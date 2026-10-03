"""Tests for the Streamlit UI (Phase 5b), using Streamlit's headless AppTest and a fake runtime."""

import queue
import time

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from agent.turn import TurnEvent
from ui import runtime as rt

APP = "../src/ui/app.py"  # relative to this test file


# Stands in for AgentRuntime: same attributes and `submit`, but no thread, no LLM, no MCP.
class FakeRuntime:
    def __init__(self, events=None, state="ready", error=None, audit_error=None, busy=False):
        self.events = events or []
        self.state = state
        self.error = error
        self.audit = object()
        self.audit_error = audit_error
        self.provider, self.model = "gemini", "fake-model"
        self.tool_count = 3
        self.sop_warm = True
        self.busy = busy
        self.submitted = []

    def start(self):
        pass  # the real runtime starts its background thread here

    counter = 0

    def new_session_id(self):
        FakeRuntime.counter += 1
        return f"session-{FakeRuntime.counter}"

    def submit(self, question, session_id):
        if self.busy:
            raise rt.Busy("busy")
        self.submitted.append((question, session_id))
        q = queue.Queue()
        for event in self.events:
            q.put(event)
        q.put(None)
        return q


class FakeReader:
    def __init__(self, rows=None, fail=None):
        self.rows = rows or []
        self.fail = fail
        self.last_filters = None

    def sessions(self, limit=50):
        if self.fail:
            raise RuntimeError(self.fail)
        return [{"SessionId": "abcdef12-0000", "StartedUtc": "2026-10-03 12:00:00", "Events": 4}]

    def search(self, **filters):
        self.last_filters = filters
        return self.rows


ROW = {
    "LogId": 7,
    "LoggedAtUtc": "2026-10-03 12:00:01",
    "SessionId": "abcdef12-0000",
    "TurnId": "t",
    "StepNo": 2,
    "Node": "reasoner",
    "EventType": "tool_call",
    "ToolName": "query_telemetry_data",
    "ToolCallId": "c1",
    "Status": None,
    "InputData": '{"sql": "SELECT 1"}',
    "OutputData": None,
    "ErrorMessage": None,
    "Provider": "gemini",
    "Model": "m",
    "DurationMs": 12,
    "PromptTokens": None,
    "CompletionTokens": None,
}

HAPPY = [
    TurnEvent("tool_call", "query_telemetry_data", "c1", {"sql": "SELECT 1"}, duration_ms=5),
    TurnEvent("tool_result", "query_telemetry_data", "c1", '{"status":"ok","rows":[[1]]}', "ok", 30),
    TurnEvent("answer", data="**Answer:** one row."),
]


def make_app(monkeypatch, runtime=None, reader=None):
    st.cache_resource.clear()  # get_runtime / get_reader are cached per process
    runtime = runtime or FakeRuntime(HAPPY)
    monkeypatch.setattr(rt, "create_runtime", lambda: runtime)
    monkeypatch.setattr(rt, "create_reader", lambda: reader or FakeReader())
    app = AppTest.from_file(APP, default_timeout=30)
    app.run()
    return app, runtime


def texts(elements):
    return " | ".join(str(e.value) for e in elements)


def test_sidebar_shows_status(monkeypatch):
    app, _ = make_app(monkeypatch)
    assert not app.exception
    sidebar = texts(app.sidebar.markdown) + texts(app.sidebar.caption)
    assert "Agent: ready" in sidebar and "Tools: 3" in sidebar and "Audit log: on" in sidebar
    assert "gemini · fake-model" in sidebar


def test_a_question_renders_the_tool_card_and_the_answer(monkeypatch):
    app, runtime = make_app(monkeypatch)
    app.chat_input[0].set_value("how many readings?").run()
    assert not app.exception
    assert runtime.submitted[0][0] == "how many readings?"
    assert "one row" in texts(app.markdown)
    assert any("query_telemetry_data" in e.label for e in app.expander)  # the stored tool card
    assert [m.name for m in app.chat_message] == ["user", "assistant"]


def test_audit_failure_shows_a_warning_and_the_answer_still_appears(monkeypatch):
    events = [TurnEvent("audit_warning", data="Audit log unavailable: db down"), *HAPPY[-1:]]
    app, _ = make_app(monkeypatch, FakeRuntime(events))
    app.chat_input[0].set_value("hi").run()
    assert "Audit log unavailable" in texts(app.warning)
    assert "one row" in texts(app.markdown)


def test_an_error_event_is_shown_as_an_error(monkeypatch):
    app, _ = make_app(monkeypatch, FakeRuntime([TurnEvent("error", data="quota reached")]))
    app.chat_input[0].set_value("hi").run()
    assert "quota reached" in texts(app.error)


def test_a_busy_runtime_refuses_the_turn_with_a_message(monkeypatch):
    app, _ = make_app(monkeypatch, FakeRuntime(busy=True))
    app.chat_input[0].set_value("hi").run()
    assert "still running" in texts(app.warning)


def test_new_chat_clears_the_chat_and_changes_the_session_id(monkeypatch):
    app, _ = make_app(monkeypatch)
    app.chat_input[0].set_value("hi").run()
    before = app.session_state.session_id
    assert len(app.session_state.messages) == 2
    next(b for b in app.sidebar.button if "New chat" in b.label).click().run()
    assert app.session_state.messages == []
    assert app.session_state.session_id != before


def test_a_failed_start_is_explained(monkeypatch):
    app, _ = make_app(monkeypatch, FakeRuntime(state="error", error="GEMINI_API_KEY is missing."))
    assert "could not start" in texts(app.error) and "GEMINI_API_KEY" in texts(app.error)


def test_audit_tab_lists_entries_and_passes_filters(monkeypatch):
    reader = FakeReader(rows=[ROW])
    app, _ = make_app(monkeypatch, reader=reader)
    assert not app.exception
    assert len(app.dataframe) == 1 and app.dataframe[0].value.iloc[0]["LogId"] == 7
    assert app.dataframe[0].value.iloc[0]["Preview"] == '{"sql": "SELECT 1"}'  # input shown when no output
    assert "SELECT 1" in texts(app.code)  # the opened entry's full input
    app.text_input[0].set_value("select").run()
    assert reader.last_filters["text"] == "select"


def test_audit_tab_explains_an_unreadable_log(monkeypatch):
    app, _ = make_app(monkeypatch, reader=FakeReader(fail="login failed"))
    assert "make audit-db-setup" in texts(app.info)


# ---------------------------------------------------------------- the real runtime (no Streamlit)
class FakeGraph:
    """Emits one answer slowly, so a second turn arrives while the first is running."""

    async def astream(self, payload, config, stream_mode):
        import asyncio

        from langchain_core.messages import AIMessage

        await asyncio.sleep(0.3)
        yield {"reasoner": {"messages": [AIMessage(content="slow answer")]}}


class NoTools:
    async def __aenter__(self):
        return []

    async def __aexit__(self, *exc):
        return False


def wait_until(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_runtime_runs_one_turn_at_a_time(monkeypatch):
    monkeypatch.setattr(rt, "build_graph", lambda llm, tools, saver: FakeGraph())
    runtime = rt.AgentRuntime(llm=object(), tools_factory=NoTools, audit_enabled=False)
    monkeypatch.setattr(rt, "describe_llm", lambda llm: ("gemini", "m"))
    runtime.start()
    assert wait_until(lambda: runtime.state == "ready")

    first = runtime.submit("one", "s1")
    with pytest.raises(rt.Busy):  # a second tab while the first turn is running
        runtime.submit("two", "s2")
    events = []
    while (event := first.get(timeout=5)) is not None:
        events.append(event)
    assert [e.kind for e in events] == ["answer"] and events[0].data == "slow answer"
    assert wait_until(lambda: not runtime._turn_lock.locked())
    runtime.submit("three", "s1")  # the lock is free again
    runtime.stop()


def test_runtime_refuses_turns_before_it_is_ready():
    runtime = rt.AgentRuntime(llm=object(), tools_factory=NoTools, audit_enabled=False)
    with pytest.raises(rt.NotReady):
        runtime.submit("too early", "s1")


def test_runtime_reports_a_startup_failure_instead_of_raising():
    class Broken:
        async def __aenter__(self):
            raise RuntimeError("mcp server exited")

        async def __aexit__(self, *exc):
            return False

    runtime = rt.AgentRuntime(llm=object(), tools_factory=Broken, audit_enabled=False)
    runtime.start()
    assert wait_until(lambda: runtime.state == "error")
    assert "mcp server exited" in runtime.error


# Live: the real runtime (MCP server, LLM, audit log) behind the real page, driven headlessly.
# Needs the SQL Server container, `make audit-db-setup`, `make sop-ingest` and an LLM key; it spends a
# little LLM quota. Skipped by default; run with `uv run pytest -m integration tests/test_ui.py`.
@pytest.mark.integration
def test_live_page_answers_a_real_question_with_a_tool_card():
    st.cache_resource.clear()
    app = AppTest.from_file(APP, default_timeout=180)
    app.run()
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        sidebar = texts(app.sidebar.markdown)
        if "Agent: ready" in sidebar and "SOP model: warm" in sidebar:
            break
        time.sleep(1)
        app.run()
    else:
        pytest.fail("the agent did not become ready: " + texts(app.sidebar.markdown))
    app.chat_input[0].set_value("What is the temperature breach threshold for fresh perishables?").run()
    if any(marker in texts(app.error).lower() for marker in ("quota", "overloaded")):
        pytest.skip("LLM temporarily unavailable: " + texts(app.error))
    assert not app.exception
    assert any("search_compliance_sop" in e.label for e in app.expander)
    assert "4.0" in texts(app.markdown)
    st.cache_resource.clear()


# Regression: importing the ingestion module in the UI process loaded `transformers`; Streamlit's file
# watcher then walked every lazy submodule (a torchvision error for each) and start-up took long.
def test_agent_tools_does_not_pull_transformers_into_the_process():
    import subprocess
    import sys

    code = (
        "import agent.tools as t, sys; t.local_model_is_cached(); "
        "print('transformers' in sys.modules or 'torch' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env={"PYTHONPATH": "src", "PATH": ""}
    )
    assert result.stdout.strip().endswith("False"), result.stdout + result.stderr


def test_cache_folder_matches_the_ingestion_model_name():
    import re
    from pathlib import Path

    from agent.tools import LOCAL_MODEL_CACHE_FOLDER

    # Read the constant from the source text: importing the ingestion module would load `transformers`.
    source = (Path(__file__).parent.parent / "src" / "data" / "ingest_sop_pinecone.py").read_text()
    model = re.search(r'^LOCAL_EMBEDDING_MODEL = "(.+)"', source, re.MULTILINE).group(1)
    assert LOCAL_MODEL_CACHE_FOLDER == "models--" + model.replace("/", "--")


# Load .streamlit/config.toml as a dict.
def streamlit_config():
    import tomllib
    from pathlib import Path

    return tomllib.loads((Path(__file__).parent.parent / ".streamlit" / "config.toml").read_text())


def test_streamlit_config_turns_the_file_watcher_off():
    assert streamlit_config()["server"]["fileWatcherType"] == "none"


# WCAG relative luminance and contrast ratio of two #RRGGBB colours (4.5 is the minimum for body text).
def contrast(foreground, background):
    def luminance(color):
        channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_both_themes_are_defined_and_one_is_actually_dark():
    theme = streamlit_config()["theme"]
    light, dark = theme["light"], theme["dark"]
    assert contrast(light["backgroundColor"], "#000000") > 10  # the light theme background is bright
    assert contrast(dark["backgroundColor"], "#000000") < 2  # the dark theme background is near black
    assert light["backgroundColor"] != dark["backgroundColor"] and light["textColor"] != dark["textColor"]


@pytest.mark.parametrize("name", ["light", "dark"])
def test_each_theme_is_readable(name):
    theme = streamlit_config()["theme"][name]
    for background in ("backgroundColor", "secondaryBackgroundColor", "codeBackgroundColor"):
        assert contrast(theme["textColor"], theme[background]) >= 4.5, f"{name}: text on {background}"
    # Links and the accent colour are text-like on the main background, so they need contrast too.
    assert contrast(theme["linkColor"], theme["backgroundColor"]) >= 4.5, f"{name}: links"
    assert contrast(theme["primaryColor"], theme["backgroundColor"]) >= 3.0, f"{name}: primary colour"


# "minimal" hides the whole menu, and the theme switch lives in that menu.
def test_the_toolbar_keeps_the_menu_so_the_theme_can_be_switched():
    assert streamlit_config()["client"]["toolbarMode"] == "viewer"


def test_the_page_tells_the_user_where_to_switch_theme(monkeypatch):
    app, _ = make_app(monkeypatch)
    assert "Theme:" in texts(app.sidebar.caption)


def test_ui_uses_no_deprecated_streamlit_container_width():
    from pathlib import Path

    assert "use_container_width" not in (Path(__file__).parent.parent / "src" / "ui" / "app.py").read_text()
