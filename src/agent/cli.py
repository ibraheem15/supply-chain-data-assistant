"""Terminal chat with the agent: python -m agent.cli [--verbose]."""

from __future__ import annotations

import argparse  # command-line flag parser (~ picocli / commons-cli)
import asyncio  # Python's event loop for async code (~ an executor running CompletableFutures)
import logging
import sys
import uuid
from typing import Any

# Message types: AIMessage = a model reply (may carry tool_calls); HumanMessage = the user's
# input; ToolMessage = the result a tool returned to the model.
# MemorySaver (next import) is an in-process checkpointer: conversation state lives in RAM only.
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphRecursionError

from agent.graph import RECURSION_LIMIT, build_graph
from agent.llm import PROVIDERS, LlmConfigError, build_llm
from agent.tools import mcp_tools

# Named logger (~ LoggerFactory.getLogger("agent.cli")). Used for debug detail, not user output.
LOGGER = logging.getLogger("agent.cli")
# Substrings in an error message that identify a quota/rate-limit failure.
QUOTA_MARKERS = ("429", "quota", "RESOURCE_EXHAUSTED", "rate limit")
# Substrings that identify a temporary provider overload.
OVERLOAD_MARKERS = ("503", "UNAVAILABLE", "high demand", "overloaded")


# Turn a message's `content` into plain text. Input: a string, or a list of content blocks
# (strings or dicts like {"text": ...}) as Gemini sometimes returns. Output: one string.
# Used to print the final answer and tool-result previews.
def message_text(content: Any) -> str:
    """Gemini returns either a string or a list of content blocks."""
    if isinstance(content, str):
        return content
    # List comprehension (a compact for-loop that builds a list), read as: for each block `b` in
    # content, skipping empty ones (`if b`), take b itself if it is a string, else its "text"
    # field (dict.get returns "" when the key is missing instead of raising).
    parts = [b if isinstance(b, str) else b.get("text", "") for b in content if b]
    # "".join(list) concatenates the pieces with no separator (~ String.join("", parts)).
    return "".join(parts)


# Send one user question through the agent graph and return its final text answer.
# Inputs: the compiled graph, the question, the conversation's thread_id (selects which saved
# memory to use), and verbose (print tool activity). Output: the answer string.
# This is the "run one turn" step: Reasoner/Tools loop happens inside graph.astream.
async def ask(graph: Any, question: str, thread_id: str, verbose: bool = False) -> str:
    """Run one question through the graph and return the final answer text."""
    # Run config: thread_id tells the checkpointer which conversation's memory to load/save;
    # recursion_limit caps the number of graph steps for this run.
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
    answer = ""
    # `async for` consumes an async stream, getting each item as it is produced without blocking.
    # astream with stream_mode="updates" yields one dict per node that ran: {node_name: its output}.
    # Only the NEW question is passed in; earlier messages are restored from the checkpointer.
    async for update in graph.astream(
        {"messages": [HumanMessage(content=question)]}, config, stream_mode="updates"
    ):
        # .values() ~ Map.values(): we ignore the node name and look at each node's output.
        for node_output in update.values():
            for message in node_output["messages"]:
                # isinstance ~ instanceof: the Reasoner produces AIMessage, the Tools node ToolMessage.
                if isinstance(message, AIMessage):
                    for call in message.tool_calls:
                        if verbose:
                            # f-string: {expr} is evaluated inline. file=sys.stderr sends the debug
                            # trace to stderr so stdout stays just the conversation.
                            print(f"  [tool call] {call['name']}({call['args']})", file=sys.stderr)
                    # An AI message with no tool calls is the final answer (the graph ends next).
                    if not message.tool_calls:
                        answer = message_text(message.content)
                elif isinstance(message, ToolMessage) and verbose:
                    # [:300] slices the first 300 characters so long tool output stays readable.
                    preview = message_text(message.content)[:300]
                    print(f"  [tool result] {message.name}: {preview}", file=sys.stderr)
    # Empty string is falsy, so `or` substitutes the fallback text.
    return answer or "(The model returned no answer.)"


# Extract a readable message from an exception. Python 3.11+ can bundle several errors in an
# ExceptionGroup (async task groups do this), which exposes them as `.exceptions`.
# Input: any exception. Output: one string; nested messages are joined with " | ".
def error_text(exc: BaseException) -> str:
    """Message of an exception, including any exceptions nested in an ExceptionGroup."""
    # getattr(obj, name, default): read an attribute by name, or None if it does not exist.
    nested = getattr(exc, "exceptions", None)
    if nested:
        # Generator expression inside join: recursion over each inner exception.
        return " | ".join(error_text(inner) for inner in nested)
    return str(exc)


# Convert a raw failure into a short, operator-friendly sentence (no stack trace in the chat).
# Input: the exception. Output: the message to show. Used by chat() when a turn fails.
def friendly_error(exc: Exception) -> str:
    if isinstance(exc, GraphRecursionError):
        return "That question needed too many steps to finish. Try narrowing it."
    text = error_text(exc)
    # any(generator) is True if at least one marker appears in the text (~ stream().anyMatch);
    # both sides are lower-cased so the match ignores case.
    if any(marker.lower() in text.lower() for marker in QUOTA_MARKERS):
        return "The model provider hit a quota or rate limit. Wait a bit, or switch with --provider."
    if any(marker.lower() in text.lower() for marker in OVERLOAD_MARKERS):
        return "The model provider is overloaded right now (temporary). Try again in a minute."
    # type(exc).__name__ is the exception's class name; splitlines()[0] keeps only the first line;
    # `x if cond else y` is Python's ternary operator (cond ? x : y).
    return f"The model call failed ({type(exc).__name__}): {text.splitlines()[0] if text else ''}"


# The interactive chat loop. Inputs: verbose flag and optional provider override.
# Output: a process exit code (0 = ok, 1 = configuration error).
# Flow: build LLM -> start MCP server and load tools -> build graph with memory -> loop on
# user input, calling ask() for each question until the user quits.
async def chat(verbose: bool, provider: str | None = None) -> int:
    try:
        llm = build_llm(provider=provider)
    except LlmConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    # `async with` (see tools.mcp_tools): the MCP server runs for the whole block and is stopped
    # automatically when the block ends, however it ends. `tools` is the yielded tool list.
    async with mcp_tools() as tools:
        # MemorySaver() gives the graph in-RAM conversation memory.
        graph = build_graph(llm, tools, MemorySaver())
        # One random id per chat session = one conversation thread in the checkpointer.
        thread_id = str(uuid.uuid4())
        print(f"Connected. {len(tools)} tools loaded. Type 'exit' to quit.")
        while True:
            try:
                # input() blocks the thread, which would freeze the event loop, so to_thread runs
                # it on a worker thread and `await` waits for it without blocking others.
                question = (await asyncio.to_thread(input, "\nyou> ")).strip()
            # Ctrl-D (EOF) or Ctrl-C ends the chat cleanly; the tuple catches either exception.
            except (EOFError, KeyboardInterrupt):
                print()
                break
            # {..} with commas is a set literal: fast membership test.
            if question.lower() in {"exit", "quit"}:
                break
            if not question:
                continue
            try:
                # f-strings can contain expressions, including `await`, evaluated before printing.
                print(f"\nagent> {await ask(graph, question, thread_id, verbose)}")
            # Catching broad Exception is deliberate here: one failed turn must not kill the chat.
            # The noqa marker on the next line tells the linter (ruff) to skip its "blind except" rule.
            except Exception as exc:  # noqa: BLE001 - never crash the chat on a model/tool failure
                # %s is lazy log formatting: only built if DEBUG logging is enabled.
                LOGGER.debug("Turn failed: %s", exc)  # the friendly message below is what the user sees
                print(f"\nagent> {friendly_error(exc)}")
    return 0


# Program entry point: parse command-line flags, set up logging, run the async chat loop.
# Output: the exit code for the process. Run via `python -m agent.cli [--verbose] [--provider X]`.
def main() -> int:
    # argparse builds the parser and the auto-generated --help text.
    parser = argparse.ArgumentParser(description="Chat with the cold-chain logistics agent.")
    # store_true: a boolean flag, False by default, True when --verbose is present.
    parser.add_argument("--verbose", action="store_true", help="Show each tool call and result.")
    # choices=PROVIDERS rejects any value other than "gemini"/"groq" with a usage error.
    parser.add_argument(
        "--provider", choices=PROVIDERS, help="LLM provider; overrides LLM_PROVIDER in envs/LLM.env."
    )
    # Reads sys.argv; flags become attributes (args.verbose, args.provider).
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    # asyncio.run starts an event loop, runs the coroutine to completion, and returns its result.
    # This is the bridge from normal synchronous code into async code (~ future.get()).
    return asyncio.run(chat(args.verbose, args.provider))


# Only true when the file is run directly (python -m agent.cli), not when imported:
# ~ `public static void main`. sys.exit(code) ends the process with main()'s return value.
if __name__ == "__main__":
    sys.exit(main())
