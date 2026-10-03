"""The ReAct graph: START -> Reasoner -> (Tools -> Reasoner)* -> END.

Pure wiring. The LLM and tools are arguments, so tests can pass fakes and this module reads
no environment files and starts no processes.
"""

from __future__ import annotations

# Sequence = "any ordered, read-only collection" (list or tuple); type hints only (~ List<T>).
# Any = "no type constraint" (~ Object); used where the real type is a complex LangGraph internal.
from collections.abc import Sequence
from typing import Any

# What the LangChain/LangGraph imports below are, in one line each:
#   BaseChatModel - common interface of all chat models (Gemini, Groq, ...).
#   BaseMessage   - parent of all chat messages.
#   HumanMessage  - what the user said.
#   SystemMessage - standing instructions to the model (our system prompt).
#   (AIMessage = model reply, may carry tool_calls; ToolMessage = a tool's result; see cli.py.)
#   trim_messages - helper that drops old messages so the history fits a token budget.
#   BaseTool      - a tool the LLM may call (name, description, input schema, function).
#   BaseCheckpointSaver - interface of a checkpointer: saves graph state after every step, keyed
#                   by thread_id; that is the conversation memory (~ an HTTP session store).
#   StateGraph    - builder for a graph of nodes (functions) that read/update a shared state.
#   MessagesState - ready-made state: a dict with one key, "messages", a list that new messages
#                   are APPENDED to (not overwritten).
#   START / END   - special marker nodes where a run begins and finishes.
#   ToolNode      - prebuilt node that executes the tool calls found in the last AI message.
#   tools_condition - prebuilt router: "tools" if the last AI message requested tools, else END.
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, trim_messages
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from agent.prompts import build_system_prompt

# Safety valve (like a max-iterations guard): LangGraph aborts a run after this many steps so a
# confused model cannot loop forever calling tools.
RECURSION_LIMIT = 12  # one tool round is two steps, so about five rounds per question
# Free API tiers limit tokens per minute and per day, so each request carries only a window of the
# conversation. The full history stays in graph state (memory); only what is SENT is trimmed.
MAX_HISTORY_TOKENS = 3000  # approximate, counting everything except the system prompt


# Choose which messages to send to the LLM on this call (a sliding window over the history).
# Input: the full message list from graph state, plus a token budget. Output: a shorter list:
# older messages that fit the budget followed by the current question's messages untouched.
# Used inside the Reasoner node, right before every LLM call.
def window_messages(
    messages: Sequence[BaseMessage], max_tokens: int = MAX_HISTORY_TOKENS
) -> list[BaseMessage]:
    """The current question's messages in full, plus as many recent older messages as fit.

    The current question (the last human message and every tool call and result after it) is never
    trimmed, so a tool loop cannot lose its own context. Older turns are dropped oldest first, and
    the window always starts on a human message so no tool result loses its tool call.
    """
    # list(...) copies into a real list so slicing below works for any Sequence.
    messages = list(messages)
    # Find the index of the LAST human message. Reading inside-out:
    #   enumerate(messages) yields (index, message) pairs;
    #   the generator expression `(i for ... if isinstance(m, HumanMessage))` lazily yields the
    #   indexes of human messages only (isinstance ~ Java's instanceof);
    #   max(..., default=0) picks the largest, or 0 if there are none (max of empty would raise).
    last_human = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=0)
    # Slicing: messages[:n] = items before index n; messages[n:] = from n to the end.
    # Tuple unpacking assigns both results in one line.
    older, current = messages[:last_human], messages[last_human:]
    budget = max_tokens - count_tokens_approximately(current)
    # Nothing to add: no room left, or no history. `not older` is True for an empty list.
    if budget <= 0 or not older:
        return current
    # Keep the newest older messages that fit the budget.
    #   strategy="last"  : keep the end of the list (drop oldest first).
    #   start_on="human" : the kept window must begin with a user message.
    #   end_on=(...)     : and may end on a human, tool or ai message.
    kept = trim_messages(
        older,
        max_tokens=budget,
        token_counter="approximate",
        strategy="last",
        start_on="human",
        end_on=("human", "tool", "ai"),
    )
    # `*x` unpacks a list in place: this builds one new list = kept messages then current ones.
    return [*kept, *current]


# Assemble and compile the ReAct agent graph (START -> reasoner -> tools -> reasoner ... -> END).
# Inputs: the chat model, the tool list, an optional checkpointer (None = no memory) and an
# optional prompt override. Output: a compiled graph; callers run it with .astream/.ainvoke.
# Called once per chat session by cli.chat(); tests call it with fake models and tools.
def build_graph(
    llm: BaseChatModel,
    tools: Sequence[BaseTool],
    checkpointer: BaseCheckpointSaver | None = None,
    system_prompt: str | None = None,
) -> Any:
    # bind_tools: returns a copy of the model that advertises these tools (name, description,
    # input schema) in every request, so the model can answer with a tool call instead of text.
    llm_with_tools = llm.bind_tools(list(tools))
    # Use the given prompt, or fall back to the default one (`or` picks the first truthy value).
    prompt = system_prompt or build_system_prompt()

    # The Reasoner node: calls the LLM with the (windowed) conversation and returns its reply.
    # Input: the graph state (a dict with the "messages" list). Output: a dict with the new
    # message; MessagesState APPENDS it to the history rather than replacing it.
    # It is defined INSIDE build_graph so it can use `llm_with_tools` and `prompt` from the
    # enclosing scope: a closure (~ a Java lambda capturing effectively-final locals).
    # `async def` makes it a coroutine so the network call does not block (~ CompletableFuture).
    async def reasoner(state: MessagesState) -> dict[str, list]:
        # The prompt is added per call and never stored in state, so the checkpointer
        # does not save a copy of it every turn.
        # `await` waits for the model's reply without blocking the thread; ainvoke is the async
        # form of invoke. The list sent = system prompt first, then the windowed history
        # (`*` unpacks that list into this one). The reply is an AIMessage: either final text
        # or a request to call tools (message.tool_calls).
        response = await llm_with_tools.ainvoke(
            [SystemMessage(content=prompt), *window_messages(state["messages"])]
        )
        return {"messages": [response]}

    # Declare a graph whose shared state has the MessagesState shape.
    graph = StateGraph(MessagesState)
    # Register nodes by name: "reasoner" = our function above; "tools" = prebuilt executor.
    graph.add_node("reasoner", reasoner)
    graph.add_node("tools", ToolNode(list(tools)))
    # Entry edge: every run starts at the reasoner.
    graph.add_edge(START, "reasoner")
    # Branch after the reasoner: tools_condition returns "tools" if the last message asked for
    # tools, else END. The dict maps those return values to destination nodes.
    graph.add_conditional_edges("reasoner", tools_condition, {"tools": "tools", END: END})
    # Loop back: after tools run, the reasoner sees their results and decides what to do next.
    graph.add_edge("tools", "reasoner")
    # compile() validates the wiring and returns a runnable graph (~ building the final object).
    # With a checkpointer, state is saved per thread_id so later questions remember earlier ones.
    return graph.compile(checkpointer=checkpointer)
