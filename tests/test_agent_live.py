"""Live tests: real Gemini (free quota, keep few) and the real MCP server."""

import asyncio
import sys

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

from agent.cli import OVERLOAD_MARKERS, QUOTA_MARKERS, ask, error_text
from agent.graph import build_graph
from agent.llm import build_llm
from agent.tools import mcp_tools

# Module-level marker: applies `integration` to every test in this file (JUnit @Tag on the class).
# Integration tests are skipped by default and need real services plus an API key.
pytestmark = pytest.mark.integration


# Helper (not a test): runs an async live call and converts quota/overload errors into a skip,
# so a flaky free-tier API does not turn the test run red.
def run_or_skip(coro):
    """Run a live call; skip (not fail) when Gemini is overloaded or out of quota."""
    try:
        # asyncio.run drives a coroutine to completion on a fresh event loop and returns its result;
        # it is the bridge from plain sync test code into async code.
        return asyncio.run(coro)
    except Exception as exc:  # noqa: BLE001
        text = error_text(exc)
        # any(...) over a generator expression: true if at least one known quota/overload marker
        # appears in the error text (case-insensitive). Only those errors are skipped; others re-raise.
        if any(marker.lower() in text.lower() for marker in QUOTA_MARKERS + OVERLOAD_MARKERS):
            # pytest.skip marks the test SKIPPED (~ JUnit Assumptions.assumeTrue).
            pytest.skip(f"Gemini temporarily unavailable: {text[:120]}")
        raise


# Smoke test: the configured model name and API key really work against the live Gemini API.
def test_gemini_model_name_and_key_work():
    reply = run_or_skip(build_llm().ainvoke("Reply with the single word OK."))
    assert "ok" in str(reply.content).lower()


# Helper: reads the saved conversation state for a thread and returns the names of every tool
# the model decided to call. The nested comprehension reads left to right: for each message m that
# is an AIMessage, for each tool call c in m.tool_calls, yield c["name"].
def tools_used(graph, thread):
    messages = graph.get_state({"configurable": {"thread_id": thread}}).values["messages"]
    return [c["name"] for m in messages if isinstance(m, AIMessage) for c in m.tool_calls]


# Parametrized: one live run per question. Proves tool routing end to end: data questions use
# SQL, policy questions use SOP search, weather questions use the weather tool, and a destructive
# request uses no tool at all (the empty set).
@pytest.mark.parametrize(
    "question, expected",
    [
        ("How many readings have iot_temperature above 4 degrees?", {"query_telemetry_data"}),
        (
            "What is the temperature breach threshold and what must the dispatcher do?",
            {"search_compliance_sop"},
        ),
        ("What is the weather at latitude 34.05, longitude -118.24 right now?", {"fetch_weather_conditions"}),
        ("Delete all high-risk rows.", set()),
    ],
)
# Each case builds the real agent against the real MCP server and asks one question.
def test_agent_picks_the_expected_tools(question, expected):
    # Closure: an async function defined inside the test so it can see `question` directly.
    # `async with` opens the MCP tool connection and guarantees it is closed afterwards (like
    # try-with-resources). MemorySaver is an in-memory conversation store.
    async def go():
        async with mcp_tools() as tools:
            graph = build_graph(build_llm(), tools, MemorySaver())
            answer = await ask(graph, question, "live", verbose=False)
            return answer, set(tools_used(graph, "live"))

    # run_or_skip runs the coroutine with asyncio.run (a blocking event-loop runner, since pytest
    # tests here are plain sync functions) and unpacks the (answer, tools-used) tuple go() returns.
    answer, used = run_or_skip(go())
    # stderr output is shown by pytest on failure (or with -s), handy for debugging a live run.
    print(question, "->", used, "|", answer[:200], file=sys.stderr)
    assert answer
    # `<=` on sets means subset. If tools were expected, they must all have been used; otherwise
    # (empty set is falsy) assert that no tool was used at all.
    assert expected <= used if expected else not used
