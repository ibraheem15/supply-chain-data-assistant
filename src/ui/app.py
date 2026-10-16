"""Streamlit UI: one chat with live tool-call cards, and an audit-log viewer.

Run with: make ui   (Streamlit re-runs this whole script on every click; the agent itself lives in
the background thread started by ui.runtime, so nothing here holds agent state.)
"""

from __future__ import annotations

import time
from datetime import datetime
from datetime import time as dt_time
from typing import Any

import pandas as pd
import streamlit as st

from ui import runtime as rt

st.set_page_config(page_title="Cold-Chain Assistant", page_icon=":material/ac_unit:", layout="wide")

PREVIEW_CHARS = 1500  # how much of a tool result a card shows; the audit log keeps all of it
TOOL_ICONS = {
    "query_telemetry_data": ":material/database:",
    "fetch_weather_conditions": ":material/cloud:",
    "search_compliance_sop": ":material/menu_book:",
}
STATUS_ICONS = {"ok": ":green[●]", "error": ":orange[●]", "unavailable": ":red[●]"}


# `st.cache_resource` keeps ONE object per server process, shared by every re-run of this script.
@st.cache_resource(show_spinner=False)
def get_runtime() -> rt.AgentRuntime:
    runtime = rt.create_runtime()
    runtime.start()
    return runtime


@st.cache_resource(show_spinner=False)
def get_reader() -> Any:
    return rt.create_reader()


# ---------------------------------------------------------------- shared pieces
# Show one tool call as an expandable card: its arguments (SQL shown as SQL) and a result preview.
def render_tool_card(tool: dict[str, Any]) -> None:
    icon = TOOL_ICONS.get(tool["name"], ":material/build:")
    status = tool.get("status")
    marker = STATUS_ICONS.get(status or "", ":gray[●]")
    seconds = f" · {tool['duration_ms'] / 1000:.1f}s" if tool.get("duration_ms") is not None else ""
    with st.expander(f"{icon} {tool['name']} {marker} {status or 'running'}{seconds}"):
        args = tool.get("args") or {}
        if "sql" in args:
            st.code(args["sql"], language="sql")
        elif args:
            st.json(args)
        if tool.get("result"):
            st.caption("Result")
            st.code(tool["result"][:PREVIEW_CHARS], language="json")


def render_message(message: dict[str, Any]) -> None:
    with st.chat_message(message["role"]):
        for tool in message.get("tools", []):
            render_tool_card(tool)
        for note in message.get("notes", []):
            (st.error if note["level"] == "error" else st.warning)(note["text"])
        if message.get("content"):
            st.markdown(message["content"])


# ---------------------------------------------------------------- sidebar
def render_sidebar(runtime: rt.AgentRuntime) -> None:
    with st.sidebar:
        st.title(":material/ac_unit: Cold-Chain Assistant")
        st.caption("Ask about shipments, live weather and SOPs.")
        if st.button("New chat", width="stretch", icon=":material/add:"):
            st.session_state.messages = []
            st.session_state.session_id = runtime.new_session_id()
            st.rerun()
        st.divider()
        st.subheader("Status")
        agent_dot = {"ready": ":green[●]", "error": ":red[●]"}.get(runtime.state, ":orange[●]")
        st.markdown(f"{agent_dot} Agent: {runtime.state}")
        if runtime.provider:
            st.caption(f"{runtime.provider} · {runtime.model}")
        st.markdown(f"{':green[●]' if runtime.tool_count else ':gray[●]'} Tools: {runtime.tool_count}")
        st.markdown(
            f"{':green[●]' if runtime.sop_warm else ':orange[●]'} SOP model: "
            f"{'warm' if runtime.sop_warm else 'loading'}"
        )
        # audit_error is filled in by the start-up ping, so "on" only counts once start-up got that far.
        audit_ok = runtime.state != "starting" and runtime.audit_error is None
        st.markdown(f"{':green[●]' if audit_ok else ':red[●]'} Audit log: {'on' if audit_ok else 'off'}")
        if runtime.audit_error:
            st.caption(runtime.audit_error[:160])
        st.divider()
        st.caption(f"Chat id: {st.session_state.session_id[:8]}")
        st.caption("Theme: top-right menu, Settings, Theme (light, dark or system).")


# ---------------------------------------------------------------- chat tab
def run_turn_live(runtime: rt.AgentRuntime, question: str) -> dict[str, Any]:
    """Run one turn and render it as it happens. Returns the assistant message to keep in history."""
    message: dict[str, Any] = {"role": "assistant", "content": "", "tools": [], "notes": []}
    try:
        events = runtime.submit(question, st.session_state.session_id)
    except rt.Busy:
        message["notes"].append(
            {"level": "warning", "text": "Another turn is still running. Try again shortly."}
        )
        render_message(message)
        return message
    except rt.NotReady as exc:
        message["notes"].append({"level": "error", "text": str(exc)})
        render_message(message)
        return message

    by_call: dict[str, dict[str, Any]] = {}
    with st.chat_message("assistant"):
        progress = st.status("Thinking…", expanded=True)
        answer_slot = st.empty()
        while (event := events.get()) is not None:  # blocks until the next event; None ends the turn
            if event.kind == "tool_call":
                tool = {"name": event.tool_name, "args": event.data, "status": None, "result": None}
                by_call[event.tool_call_id] = tool
                message["tools"].append(tool)
                progress.update(label=f"Calling {event.tool_name}…")
                with progress:
                    if "sql" in (event.data or {}):
                        st.code(event.data["sql"], language="sql")
                    else:
                        st.write(f":material/build: **{event.tool_name}** `{event.data}`")
            elif event.kind == "tool_result":
                tool = by_call.get(event.tool_call_id) or {"name": event.tool_name, "args": {}}
                tool.update(status=event.status, result=event.text, duration_ms=event.duration_ms)
                with progress:
                    st.write(
                        f"{STATUS_ICONS.get(event.status or '', ':gray[●]')} {event.tool_name} → "
                        f"{event.status or 'done'}"
                    )
            elif event.kind == "audit_warning":
                note = {"level": "warning", "text": event.text}
                message["notes"].append(note)
                st.warning(note["text"])
            elif event.kind == "error":
                note = {"level": "error", "text": event.text}
                message["notes"].append(note)
                st.error(note["text"])
            elif event.kind == "answer":
                message["content"] = event.text
                answer_slot.markdown(message["content"])
        failed = any(n["level"] == "error" for n in message["notes"])
        progress.update(
            label="Something went wrong" if failed else "Done",
            state="error" if failed else "complete",
            expanded=False,
        )
    return message


def render_chat(runtime: rt.AgentRuntime) -> None:
    if runtime.state == "error":
        st.error(f"The agent could not start: {runtime.error}")
        return
    if runtime.state != "ready":
        st.info("Starting up: loading the tools and the SOP model. This takes a few seconds the first time.")
        time.sleep(1)
        st.rerun()
    for message in st.session_state.messages:
        render_message(message)
    if not st.session_state.messages:
        st.caption(
            "Try: *Find the readings nearest to latitude 33.8, longitude -118.0 and check them "
            "against the fresh-perishables SOP.*"
        )
    question = st.chat_input("Ask about shipments, weather or SOPs")
    if question:
        st.session_state.messages.append({"role": "user", "content": question})
        render_message(st.session_state.messages[-1])
        st.session_state.messages.append(run_turn_live(runtime, question))
        # Re-run once so the finished turn is drawn from history, exactly like every older turn.
        st.rerun()


# ---------------------------------------------------------------- audit tab
def render_audit() -> None:
    reader = get_reader()
    st.subheader("Audit log")
    st.caption("Every prompt, tool call, tool result and answer, recorded by the agent. Read-only view.")
    try:
        sessions = reader.sessions()
    except Exception as exc:  # noqa: BLE001 - show the problem, do not crash the page
        st.info(
            f"The audit log cannot be read: {str(exc)[:200]}. Run `make audit-db-setup` and check "
            "`envs/audit_sqlserver.env`."
        )
        return

    left, middle, right = st.columns(3)
    session_labels = {
        "": "All chats",
        **{
            s["SessionId"]: f"{s['StartedUtc'][:19]} · {s['SessionId'][:8]} · {s['Events']} events"
            for s in sessions
        },
    }
    session_id = left.selectbox("Chat", list(session_labels), format_func=session_labels.get)
    tool_name = middle.selectbox("Tool", ["", *TOOL_ICONS], format_func=lambda v: v or "Any tool")
    event_type = right.selectbox(
        "Event",
        ["", "user_prompt", "tool_call", "tool_result", "final_answer", "error"],
        format_func=lambda v: v or "Any event",
    )
    a, b, c = st.columns(3)
    status = a.selectbox(
        "Tool status", ["", "ok", "error", "unavailable"], format_func=lambda v: v or "Any status"
    )
    dates = b.date_input("Dates (UTC)", value=(), help="Leave empty for all dates.")
    text = c.text_input("Search text", placeholder="in prompts, SQL, results, errors")

    since = until = None
    if len(dates) >= 1:
        since = datetime.combine(dates[0], dt_time.min)
        until = datetime.combine(dates[-1], dt_time.max)
    try:
        rows = reader.search(
            session_id=session_id,
            tool_name=tool_name,
            status=status,
            event_type=event_type,
            since=since,
            until=until,
            text=text or None,
        )
    except Exception as exc:  # noqa: BLE001
        st.error(f"Search failed: {str(exc)[:200]}")
        return
    if not rows:
        st.info("No audit entries match these filters.")
        return

    st.caption(f"{len(rows)} entries, newest first (limit 200).")
    table = pd.DataFrame(rows)
    shown = ["LogId", "LoggedAtUtc", "SessionId", "StepNo", "EventType", "ToolName", "Status", "DurationMs"]
    view = table[[c for c in shown if c in table]].copy()
    view["SessionId"] = view["SessionId"].str[:8]
    # A one-line preview of what the event carried (answer, tool result, prompt/SQL or error), so a final
    # answer is readable in the table without opening the entry below.
    view["Preview"] = [
        " ".join(str(next((v for v in (out, inp, err) if isinstance(v, str) and v), "")).split())[:120]
        for out, inp, err in zip(table["OutputData"], table["InputData"], table["ErrorMessage"], strict=True)
    ]
    st.dataframe(view, hide_index=True, width="stretch")

    chosen = st.selectbox("Open an entry", table["LogId"].tolist())
    row = table[table["LogId"] == chosen].iloc[0]
    st.markdown(
        f"**{row['EventType']}** · {row['ToolName'] or row['Node']} · {row['LoggedAtUtc']}"
        f" · {row['Provider'] or '-'} / {row['Model'] or '-'}"
    )
    for label, column, language in (
        ("Input", "InputData", "text"),
        ("Output", "OutputData", "json"),
        ("Error", "ErrorMessage", "text"),
    ):
        value = row[column]
        if isinstance(value, str) and value:
            st.caption(label)
            st.code(value, language=language)


# ---------------------------------------------------------------- page
runtime = get_runtime()
if "messages" not in st.session_state:
    st.session_state.messages = []
if "session_id" not in st.session_state:
    st.session_state.session_id = runtime.new_session_id()

render_sidebar(runtime)
chat_tab, audit_tab = st.tabs([":material/chat: Chat", ":material/fact_check: Audit log"])
with chat_tab:
    render_chat(runtime)
with audit_tab:
    render_audit()
