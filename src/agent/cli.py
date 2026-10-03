"""Terminal chat with the agent: python -m agent.cli [--verbose] [--provider groq] [--no-audit]."""

from __future__ import annotations

import argparse  # command-line flag parser (~ picocli / commons-cli)
import asyncio  # Python's event loop for async code (~ an executor running CompletableFutures)
import logging
import sys
import uuid
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from agent.audit import AuditLogger
from agent.graph import build_graph
from agent.llm import PROVIDERS, LlmConfigError, build_llm, describe_llm
from agent.tools import mcp_tools
from agent.turn import run_turn

LOGGER = logging.getLogger("agent.cli")


# Run one question and return the final answer text. Used by the chat loop and by tests.
# Input: the graph, the question, the session id, and optional audit/provider details. With verbose=True
# each tool call and result is printed to stderr (the ReAct loop made visible). Errors come back as
# a readable message (run_turn never raises); an audit failure prints a warning and the turn carries on.
async def ask(
    graph: Any,
    question: str,
    thread_id: str,
    verbose: bool = False,
    audit: AuditLogger | None = None,
    provider: str = "",
    model: str = "",
) -> str:
    answer = ""
    # `async for` consumes the generator run_turn yields, one event at a time, as it happens.
    async for event in run_turn(graph, question, thread_id, audit=audit, provider=provider, model=model):
        if event.kind == "tool_call" and verbose:
            print(f"  [tool call] {event.tool_name}({event.data})", file=sys.stderr)
        elif event.kind == "tool_result" and verbose:
            print(f"  [tool result] {event.tool_name}: {str(event.data)[:300]}", file=sys.stderr)
        elif event.kind == "audit_warning":
            print(f"  [warning] {event.data}", file=sys.stderr)
        elif event.kind in {"answer", "error"}:
            answer = str(event.data)
    return answer


# The chat loop. Returns the process exit code (0 ok, 1 configuration error).
async def chat(verbose: bool, provider: str | None = None, audit_enabled: bool = True) -> int:
    try:
        llm = build_llm(provider=provider)
    except LlmConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    provider_name, model_name = describe_llm(llm)

    audit = AuditLogger() if audit_enabled else None
    if audit is not None:
        # Notify and carry on: a missing or unreachable audit log never stops the chat.
        problem = audit.ping()
        if problem:
            print(
                f"Warning: audit log unavailable, this chat will not be recorded. {problem}", file=sys.stderr
            )
            audit = None

    async with mcp_tools() as tools:
        graph = build_graph(llm, tools, MemorySaver())
        thread_id = str(uuid.uuid4())
        audit_note = "audit on" if audit is not None else "audit off"
        print(f"Connected. {len(tools)} tools loaded ({provider_name}/{model_name}, {audit_note}).")
        print("Type 'exit' to quit.")
        while True:
            try:
                # input() blocks, so run it in a worker thread to keep the event loop free.
                question = (await asyncio.to_thread(input, "\nyou> ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if question.lower() in {"exit", "quit"}:
                break
            if not question:
                continue
            answer = await ask(graph, question, thread_id, verbose, audit, provider_name, model_name)
            print(f"\nagent> {answer}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Chat with the cold-chain logistics agent.")
    parser.add_argument("--verbose", action="store_true", help="Show each tool call and result.")
    parser.add_argument(
        "--provider", choices=PROVIDERS, help="LLM provider; overrides LLM_PROVIDER in envs/LLM.env."
    )
    parser.add_argument("--no-audit", action="store_true", help="Do not write to the audit log.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    return asyncio.run(chat(args.verbose, args.provider, not args.no_audit))


if __name__ == "__main__":
    sys.exit(main())
