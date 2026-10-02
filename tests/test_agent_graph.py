import asyncio
from datetime import date

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphRecursionError

from agent.cli import ask, friendly_error, message_text
from agent.graph import MAX_HISTORY_TOKENS, RECURSION_LIMIT, build_graph, window_messages
from agent.prompts import build_system_prompt


# Hand-written fake LLM (like a stub/Mockito mock of the chat model). Subclassing LangChain's
# BaseChatModel lets the real graph use it unmodified. Its `responder` function scripts each reply,
# so tests are offline, free and deterministic. Every call's input messages are recorded so tests
# can inspect exactly what the model was sent.
class ScriptedModel(BaseChatModel):
    """Fake LLM: `responder(messages)` decides each reply; every call's input is recorded."""

    # Pydantic-style field declarations (BaseChatModel is a Pydantic model): the scripted reply function
    # and the call log. The `calls: list = []` default is copied per instance by Pydantic.
    responder: object
    calls: list = []

    # Required by BaseChatModel: a name identifying the model type. @property ~ a getter.
    @property
    def _llm_type(self):
        return "scripted"

    # The graph calls bind_tools to tell a real LLM which tools exist. The fake ignores that and
    # returns itself, because the script decides tool calls on its own.
    def bind_tools(self, tools, **kwargs):
        return self

    # The single method that produces a reply. Record the input, then wrap whatever the responder
    # returns in the ChatResult/ChatGeneration envelope LangChain expects.
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=self.responder(messages))])


# @tool turns this function into a LangChain tool (name, description from the docstring, schema
# from the type hints). It is a trivial fake tool that echoes its input.
@tool
async def lookup(term: str) -> str:
    """Look something up."""
    return f"result for {term}"


# Helper: builds an AIMessage in which the model 'asks' to call a tool, like a real LLM would.
# `args or {...}` uses the default when args is None (Python's `or` returns the first truthy operand).
def tool_call(name="lookup", args=None, call_id="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {"term": "x"}, "id": call_id}])


# Helper: runs the graph for one user message on a given conversation thread. The thread_id is
# what the checkpointer uses to find saved memory; recursion_limit caps graph steps.
# asyncio.run executes the async ainvoke from sync test code.
def run(graph, text, thread="t1", limit=RECURSION_LIMIT):
    config = {"configurable": {"thread_id": thread}, "recursion_limit": limit}
    return asyncio.run(graph.ainvoke({"messages": [HumanMessage(content=text)]}, config))


# Helper: builds a scripted model and a real graph around it (the fake tool, optional memory).
# Returns both so tests can inspect model.calls. `model, graph = make(...)` is tuple unpacking.
def make(responder, checkpointer=None):
    model = ScriptedModel(responder=responder, calls=[])
    return model, build_graph(model, [lookup], checkpointer)


# Proves: when the model answers directly, the graph goes reasoner -> END with no tool step
# and exactly one model call. `lambda m: ...` is an anonymous function (Java lambda) ignoring its input.
def test_no_tool_question_goes_straight_to_end():
    model, graph = make(lambda m: AIMessage(content="plain answer"))
    result = run(graph, "hello")
    assert result["messages"][-1].content == "plain answer"
    assert not any(isinstance(m, ToolMessage) for m in result["messages"])
    assert len(model.calls) == 1


# Proves the core agent loop: model asks for a tool -> tool runs -> result goes back to the
# model -> model gives the final answer. Hence exactly two model calls and one ToolMessage.
def test_tool_call_loops_through_tools_back_to_reasoner():
    # Nested function: if the last message is a tool result, answer; otherwise request a tool.
    # `messages[-1]` is the last list element.
    def responder(messages):
        if isinstance(messages[-1], ToolMessage):
            return AIMessage(content=f"final: {messages[-1].content}")
        return tool_call()

    model, graph = make(responder)
    result = run(graph, "look up x")
    assert result["messages"][-1].content == "final: result for x"
    assert any(isinstance(m, ToolMessage) for m in result["messages"])
    assert len(model.calls) == 2


# Proves: the system prompt is injected at call time (the model sees it first) but is not saved
# in conversation state, so it is never duplicated or stale in memory.
def test_system_prompt_is_first_for_the_model_but_not_stored_in_state():
    model, graph = make(lambda m: AIMessage(content="ok"), MemorySaver())
    result = run(graph, "hi")
    assert isinstance(model.calls[0][0], SystemMessage)
    assert not any(isinstance(m, SystemMessage) for m in result["messages"])


# Proves: with a checkpointer, the same thread_id accumulates history and a different thread_id
# starts fresh. Like HTTP sessions keyed by session id.
def test_memory_follows_thread_id():
    saver = MemorySaver()

    # Responder that reports how many user messages the model was shown. The generator expression
    # `isinstance(x, HumanMessage) for x in messages` yields booleans; sum() counts the True values.
    def count_humans(messages):
        return AIMessage(content=f"{sum(isinstance(x, HumanMessage) for x in messages)} human")

    _, graph = make(count_humans, saver)
    assert run(graph, "one", "a")["messages"][-1].content == "1 human"
    assert run(graph, "two", "a")["messages"][-1].content == "2 human"
    assert run(graph, "other thread", "b")["messages"][-1].content == "1 human"


# Proves: a model that calls tools forever is stopped by the recursion limit, not allowed to
# spin and burn quota. The responder here always asks for a tool, never an answer.
def test_endless_tool_loop_hits_the_step_limit():
    _, graph = make(lambda m: tool_call())
    with pytest.raises(GraphRecursionError):
        run(graph, "loop forever", limit=4)


# Proves: ask() returns just the final text, even when the model gives content as a list of
# typed blocks (as Gemini does) rather than a plain string.
def test_ask_returns_final_answer_only():
    def responder(messages):
        if isinstance(messages[-1], ToolMessage):
            return AIMessage(content=[{"type": "text", "text": "done"}])
        return tool_call()

    _, graph = make(responder, MemorySaver())
    assert asyncio.run(ask(graph, "q", "t9")) == "done"


# Proves: raw exceptions are turned into short, user-readable messages (no stack traces).
# ExceptionGroup is Python 3.11+ aggregate-exception type (async code raises these); the helper
# must look inside the group. `message_text` flattens content blocks and plain strings into text.
def test_friendly_errors():
    assert "too many steps" in friendly_error(GraphRecursionError("x"))
    assert "quota" in friendly_error(RuntimeError("429 RESOURCE_EXHAUSTED"))
    assert "overloaded" in friendly_error(RuntimeError("503 UNAVAILABLE high demand"))
    assert "overloaded" in friendly_error(ExceptionGroup("group", [RuntimeError("503 UNAVAILABLE")]))
    assert "ValueError" in friendly_error(ValueError("boom\nsecond line"))
    assert message_text([{"type": "text", "text": "a"}, "b"]) == "ab"


# Proves: the system prompt includes every behavioural rule (tool names, error handling, no
# inventing facts, citations, data date, read-only SQL) and today's date, with no unfilled
# {today} placeholder. Prompt text is code here: dropping a rule silently would change the agent.
def test_prompt_contains_every_rule_and_today():
    prompt = build_system_prompt(date(2026, 10, 2))
    # Plain loop over a list of required phrases; the message after the comma is shown if it fails.
    for phrase in [
        "query_telemetry_data",
        "fetch_weather_conditions",
        "search_compliance_sop",
        'status "error"',
        'status "unavailable"',
        "Never invent",
        "cite the source file",
        "2024-08-29",
        "never LIMIT",
        "read-only",
        "IOT_TEMP_C",
        "2026-10-02",
    ]:
        assert phrase.lower() in prompt.lower(), phrase
    assert "{today}" not in prompt


# Helper: one fake question/answer pair of roughly `size` characters, to fill the history.
def long_turn(i, size=400):
    return [HumanMessage(content=f"q{i} " + "x" * size), AIMessage(content=f"a{i} " + "y" * size)]


# Proves: when history exceeds the token budget, the oldest turns are dropped but the window
# still starts at a user question and ends at the newest answer. Bounds cost per request.
def test_window_keeps_current_question_and_drops_oldest_turns():
    # Nested comprehension: flattens 20 turns (each a list of 2 messages) into one list of 40.
    history = [m for i in range(20) for m in long_turn(i)]
    windowed = window_messages(history, max_tokens=600)
    assert windowed[0].content.startswith("q") and isinstance(windowed[0], HumanMessage)
    assert windowed[-1].content.startswith("a19")
    assert len(windowed) < len(history)
    assert not any(m.content.startswith("q0") for m in windowed)


# Proves: the in-progress tool loop (question, tool call, tool result) is kept whole even when
# it alone exceeds the budget. Trimming it would leave the model without its own tool results.
def test_window_never_trims_the_current_tool_loop():
    current = [
        HumanMessage(content="now"),
        tool_call(),
        ToolMessage(content="r" * 8000, tool_call_id="c1"),
    ]
    # `[*a, *b]` unpacks two lists into one new list (like addAll into a new ArrayList).
    windowed = window_messages([*long_turn(0), *current], max_tokens=100)
    assert windowed == current


# Proves: for any budget, the window never begins with a ToolMessage whose tool call was cut
# off; providers reject that sequence. `range(1, 200, 7)` sweeps many budgets (start, stop, step).
def test_window_never_starts_on_an_orphan_tool_message():
    history = [
        HumanMessage(content="h"),
        tool_call(),
        ToolMessage(content="r", tool_call_id="c1"),
        AIMessage(content="done"),
        HumanMessage(content="next"),
    ]
    for budget in range(1, 200, 7):
        windowed = window_messages(history, max_tokens=budget)
        assert isinstance(windowed[0], HumanMessage)


# Proves both halves of memory design: the model is sent a bounded history (cost control) while
# the checkpointed state keeps every message (nothing lost). 30 turns -> 60 saved messages.
def test_model_receives_a_bounded_history_but_state_keeps_everything():
    saver = MemorySaver()
    model, graph = make(lambda m: AIMessage(content="ok " + "z" * 400), saver)
    for i in range(30):
        result = run(graph, f"question {i} " + "w" * 400, "long")
    # Messages the model received on the last call. Rough token estimate is characters // 4
    # (`//` is integer division); the +800 is slack for the system prompt.
    sent = model.calls[-1]
    assert sum(len(str(m.content)) for m in sent) // 4 < MAX_HISTORY_TOKENS + 800
    assert len(result["messages"]) == 60  # nothing was removed from memory
