"""Tests for the audit writer, the audit reader and the shared turn loop (Phase 5a)."""

import asyncio
import json
from datetime import datetime

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver

from agent.audit import COLUMNS, AuditEvent, AuditLogger, AuditReader, like_pattern, redact
from agent.cli import ask
from agent.graph import build_graph
from agent.turn import run_turn

SETTINGS = {"MSSQL_AUDIT_WRITER_PASSWORD": "pw-writer-123", "MSSQL_AUDIT_READER_PASSWORD": "pw-reader-123"}


# A hand-written stand-in for a pyodbc connection that remembers every statement it was given.
class FakeConnection:
    def __init__(self, fail=None):
        self.executed = []
        self.closed = False
        self.fail = fail
        self.description = [("LogId",), ("LoggedAtUtc",), ("Status",)]
        self.rows = [(1, datetime(2026, 10, 3, 12, 0, 0), "ok")]

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        if self.fail:
            raise RuntimeError(self.fail)
        self.executed.append((sql, params))
        return self

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


def make_logger(connection=None, secrets=None, settings=SETTINGS, connect_error=None):
    connection = connection or FakeConnection()

    def connect(_settings, user, password):
        if connect_error:
            raise RuntimeError(connect_error)
        connection.user = user
        connection.password = password
        return connection

    return AuditLogger(settings=settings, connect=connect, secrets=secrets or []), connection


def event(**overrides):
    base = {"session_id": "s1", "turn_id": "t1", "step_no": 1, "node": "tools", "event_type": "tool_result"}
    return AuditEvent(**{**base, **overrides})


# ---------------------------------------------------------------- writer
def test_columns_follow_the_event_fields():
    assert COLUMNS[:5] == ("SessionId", "TurnId", "StepNo", "Node", "EventType")
    assert "PromptTokens" in COLUMNS and "ErrorMessage" in COLUMNS


def test_insert_uses_the_writer_login_and_bound_parameters():
    logger, connection = make_logger()
    error = asyncio.run(logger.record(event(tool_name="query_telemetry_data", input_data="SELECT 1")))
    assert error is None and connection.closed
    assert connection.user == "USR_FDE_AUDIT_W" and connection.password == "pw-writer-123"
    sql, params = connection.executed[0]
    assert sql.startswith("INSERT INTO [audit].[AgentAuditLog] (SessionId, TurnId,")
    assert "SELECT 1" not in sql  # values are bound parameters, never pasted into the statement
    assert params[0] == "s1" and "SELECT 1" in params


def test_secrets_are_redacted_before_they_are_stored():
    logger, connection = make_logger(secrets=["sk-very-secret-key", "pw-writer-123"])
    asyncio.run(
        logger.record(event(input_data="key is sk-very-secret-key", error_message="pw-writer-123 bad"))
    )
    params = connection.executed[0][1]
    assert "key is ***" in params and "*** bad" in params
    assert not any("sk-very-secret-key" in str(p) for p in params)
    assert redact(None, ["x" * 8]) is None


def test_write_failure_returns_a_message_and_never_raises():
    logger, connection = make_logger(
        connection=FakeConnection(fail="boom pw-writer-123"), secrets=["pw-writer-123"]
    )
    error = asyncio.run(logger.record(event()))
    assert error == "boom ***" and logger.last_error == "boom ***"
    assert connection.closed


def test_unreachable_database_is_reported_not_raised():
    logger, _ = make_logger(connect_error="login timeout")
    assert asyncio.run(logger.record(event())) == "login timeout"
    assert logger.ping() == "login timeout"


def test_missing_password_is_reported_with_the_env_file_name():
    logger, _ = make_logger(settings={})
    error = asyncio.run(logger.record(event()))
    assert "MSSQL_AUDIT_WRITER_PASSWORD" in error and "audit_sqlserver.env" in error


# ---------------------------------------------------------------- reader
def test_reader_uses_the_reader_login_and_filters_with_parameters():
    connection = FakeConnection()
    reader = AuditReader(
        settings=SETTINGS, connect=lambda s, u, p: (setattr(connection, "user", u), connection)[1]
    )
    rows = reader.search(
        session_id="s1", tool_name="query_telemetry_data", status="ok", text="50%_off", limit=10
    )
    sql, params = connection.executed[0]
    assert connection.user == "USR_FDE_AUDIT_R" and connection.closed
    assert "[SessionId] = ?" in sql and "[ToolName] = ?" in sql and "[Status] = ?" in sql
    assert "query_telemetry_data" not in sql and "50" not in sql  # nothing pasted into the SQL
    assert params[0] == 10 and params[1] == "s1" and params[-1] == "%50\\%\\_off%"
    assert rows == [{"LogId": 1, "LoggedAtUtc": "2026-10-03 12:00:00", "Status": "ok"}]


def test_like_pattern_escapes_wildcards():
    assert like_pattern("a%b_c[d") == "%a\\%b\\_c\\[d%"


# ---------------------------------------------------------------- turn loop
# A scripted fake LLM (same idea as in test_agent_graph.py): one tool call, then an answer.
class ScriptedModel(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self):
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            message = AIMessage(
                content="", tool_calls=[{"name": "lookup", "args": {"term": "x"}, "id": "c1"}]
            )
        else:
            message = AIMessage(
                content="final answer",
                usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


@tool
async def lookup(term: str) -> str:
    """Look something up."""
    return json.dumps({"status": "ok", "value": term})


@tool
async def broken(term: str) -> str:
    """Always reports unavailable."""
    return json.dumps({"status": "unavailable", "error": "down"})


class RecordingAudit:
    """Collects events in memory; can be told to fail."""

    def __init__(self, error=None):
        self.events = []
        self.error = error

    async def record(self, ev):
        self.events.append(ev)
        return self.error


def collect(graph, audit, question="look up x"):
    async def go():
        return [
            e async for e in run_turn(graph, question, "sess-1", audit=audit, provider="gemini", model="m")
        ]

    return asyncio.run(go())


def make_graph():
    return build_graph(ScriptedModel(), [lookup], MemorySaver())


def test_turn_yields_and_audits_every_step_in_order():
    audit = RecordingAudit()
    events = collect(make_graph(), audit)
    assert [e.kind for e in events] == ["tool_call", "tool_result", "answer"]
    assert [a.event_type for a in audit.events] == ["user_prompt", "tool_call", "tool_result", "final_answer"]
    assert [a.step_no for a in audit.events] == [1, 2, 3, 4]
    assert len({a.turn_id for a in audit.events}) == 1 and {a.session_id for a in audit.events} == {"sess-1"}
    call, result, final = audit.events[1], audit.events[2], audit.events[3]
    assert call.tool_call_id == result.tool_call_id == "c1" and call.tool_name == "lookup"
    assert json.loads(call.input_data) == {"term": "x"}
    assert result.status == "ok" and '"value": "x"' in result.output_data
    assert (final.prompt_tokens, final.completion_tokens) == (7, 3)
    assert {a.provider for a in audit.events} == {"gemini"} and all(
        a.duration_ms is not None for a in audit.events
    )


def test_audit_failure_warns_once_and_the_turn_still_completes():
    audit = RecordingAudit(error="database down")
    events = collect(make_graph(), audit)
    kinds = [e.kind for e in events]
    assert kinds.count("audit_warning") == 1  # one warning per turn, not one per event
    assert kinds[-1] == "answer" and events[-1].data == "final answer"
    assert "database down" in next(e.data for e in events if e.kind == "audit_warning")


def test_turn_without_an_audit_logger_still_works():
    events = collect(make_graph(), None)
    assert [e.kind for e in events] == ["tool_call", "tool_result", "answer"]


def test_a_failing_graph_becomes_an_error_event_and_an_audit_row():
    class Boom:
        async def astream(self, *args, **kwargs):
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
            yield  # pragma: no cover - makes this an async generator

    audit = RecordingAudit()
    events = collect(Boom(), audit)
    assert [e.kind for e in events] == ["error"] and "quota" in events[0].data
    assert audit.events[-1].event_type == "error" and "429" in audit.events[-1].error_message


def test_unavailable_tool_status_is_recorded():
    graph = build_graph(ScriptedModel(), [broken, lookup], MemorySaver())
    # The scripted model calls "lookup"; check the status parser on a direct unavailable result too.
    from agent.turn import tool_status

    assert tool_status(json.dumps({"status": "unavailable"})) == "unavailable"
    assert tool_status("not json") is None and tool_status("[1]") is None
    assert collect(graph, RecordingAudit())[-1].kind == "answer"


def test_cli_ask_returns_the_answer_and_prints_the_audit_warning(capsys):
    answer = asyncio.run(
        ask(make_graph(), "q", "sess-2", verbose=True, audit=RecordingAudit(error="db down"))
    )
    err = capsys.readouterr().err
    assert answer == "final answer"
    assert "[tool call] lookup" in err and "[warning] Audit log unavailable" in err


@pytest.mark.integration
def test_live_audit_permissions_and_a_real_turn():
    """Needs the SQL Server container and `make audit-db-setup`."""
    import uuid

    from resources.db import open_connection
    from resources.settings import AGENT_ENV_FILE, load_settings

    session = f"it-{uuid.uuid4()}"
    logger = AuditLogger()
    reader = AuditReader()
    assert logger.ping() is None and reader.ping() is None

    async def go():
        async for _ in run_turn(make_graph(), "look up x", session, audit=logger, provider="test", model="m"):
            pass

    asyncio.run(go())
    rows = reader.search(session_id=session)
    assert [r["EventType"] for r in reversed(rows)] == [
        "user_prompt",
        "tool_call",
        "tool_result",
        "final_answer",
    ]
    assert reader.search(session_id=session, text="final answer")[0]["EventType"] == "final_answer"
    assert any(s["SessionId"] == session for s in reader.sessions())

    # The writer can only insert; the reader can only select; the agent's own login sees nothing.
    settings = load_settings(prefixes=("MSSQL_",))

    def refused(user, password, sql):
        connection = open_connection({**settings}, user, password)
        try:
            connection.cursor().execute(sql)
        except Exception:  # noqa: BLE001
            return True
        finally:
            connection.close()
        return False

    audit_settings = logger.settings
    writer = ("USR_FDE_AUDIT_W", audit_settings["MSSQL_AUDIT_WRITER_PASSWORD"])
    read = ("USR_FDE_AUDIT_R", audit_settings["MSSQL_AUDIT_READER_PASSWORD"])
    agent = ("USR_FDE_RO", load_settings(AGENT_ENV_FILE)["MSSQL_AGENT_PASSWORD"])
    assert refused(*writer, "SELECT TOP 1 * FROM audit.AgentAuditLog")
    assert refused(*writer, f"UPDATE audit.AgentAuditLog SET Status='x' WHERE SessionId='{session}'")
    assert refused(*writer, f"DELETE FROM audit.AgentAuditLog WHERE SessionId='{session}'")
    assert refused(*read, f"DELETE FROM audit.AgentAuditLog WHERE SessionId='{session}'")
    assert refused(*agent, "SELECT TOP 1 * FROM audit.AgentAuditLog")


# ---------------------------------------------------------------- empty answer caused by the output limit
# Live bug: a reasoning model used its whole output limit thinking and returned no text. The audit log showed
# CompletionTokens == the cap and an empty reply. The turn must now say so, not print a bland "no answer".
class TruncatedModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "truncated"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        message = AIMessage(
            content="",
            response_metadata={"finish_reason": "length"},
            usage_metadata={"input_tokens": 100, "output_tokens": 4096, "total_tokens": 4196},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_empty_answer_at_the_output_limit_is_reported_as_an_error_and_audited():
    graph = build_graph(TruncatedModel(), [lookup], MemorySaver())
    audit = RecordingAudit()
    events = collect(graph, audit)
    assert [e.kind for e in events] == ["error"]
    assert "output limit" in events[0].data and "4096" in events[0].data
    assert audit.events[-1].event_type == "error" and audit.events[-1].completion_tokens == 4096


def test_an_empty_answer_for_another_reason_is_still_reported_plainly():
    class EmptyModel(TruncatedModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=""))])

    events = collect(build_graph(EmptyModel(), [lookup], MemorySaver()), RecordingAudit())
    assert [e.kind for e in events] == ["answer"] and "no answer" in events[0].data
