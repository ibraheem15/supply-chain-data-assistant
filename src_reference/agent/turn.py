"""Run one chat turn and yield events. The CLI and the UI both use this, so neither can skip the audit."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError

from agent.audit import AuditEvent, AuditLogger
from agent.graph import RECURSION_LIMIT

QUOTA_MARKERS = ("429", "quota", "RESOURCE_EXHAUSTED", "rate limit")
OVERLOAD_MARKERS = ("503", "UNAVAILABLE", "high demand", "overloaded")
NO_ANSWER = "(The model returned no answer.)"
# finish_reason values meaning "stopped because the output limit was reached" (Groq, Gemini).
LENGTH_FINISH_REASONS = {"length", "max_tokens"}


# What a front end (CLI or UI) receives while a turn runs.
#   kind: tool_call | tool_result | answer | error | audit_warning
#   data: tool arguments (tool_call), text (tool_result, answer, error, audit_warning)
@dataclass(frozen=True)
class TurnEvent:
    kind: str
    tool_name: str | None = None
    tool_call_id: str | None = None
    data: Any = None
    status: str | None = None
    duration_ms: int | None = None


# Gemini returns message content as either a string or a list of content blocks; reduce it to text.
def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = [b if isinstance(b, str) else b.get("text", "") for b in content if b]
    return "".join(parts)


# Message of an exception, including any exceptions nested in an ExceptionGroup.
def error_text(exc: BaseException) -> str:
    nested = getattr(exc, "exceptions", None)
    if nested:
        return " | ".join(error_text(inner) for inner in nested)
    return str(exc)


# Turn an exception into a short message a person can act on.
def friendly_error(exc: Exception) -> str:
    if isinstance(exc, GraphRecursionError):
        return "That question needed too many steps to finish. Try narrowing it."
    text = error_text(exc)
    if any(marker.lower() in text.lower() for marker in QUOTA_MARKERS):
        return "The model provider hit a quota or rate limit. Wait a bit, or switch with --provider."
    if any(marker.lower() in text.lower() for marker in OVERLOAD_MARKERS):
        return "The model provider is overloaded right now (temporary). Try again in a minute."
    return f"The model call failed ({type(exc).__name__}): {text.splitlines()[0] if text else ''}"


# The "status" field the tools put in their JSON result (ok | error | unavailable), or None.
def tool_status(text: str) -> str | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value.get("status") if isinstance(value, dict) else None


# Run one question through the graph.
# Input: the compiled graph, the question, the chat's session id, an optional AuditLogger, and the
# provider/model names to record. Output: an async generator of TurnEvent. It never raises: a failure
# becomes an "error" event. A failed audit write becomes one "audit_warning" event per turn and the
# turn carries on.
async def run_turn(
    graph: Any,
    question: str,
    session_id: str,
    *,
    audit: AuditLogger | None = None,
    provider: str = "",
    model: str = "",
) -> AsyncGenerator[TurnEvent]:
    turn_id = str(uuid.uuid4())
    step = 0
    last = time.monotonic()
    warned = False
    # Collected here, yielded by the loop below: a plain function cannot `yield` for its caller.
    pending: list[TurnEvent] = []

    # Write one audit row and note (once per turn) if it failed. Times each event since the previous one.
    async def log(node: str, event_type: str, **fields: Any) -> int:
        nonlocal step, last, warned
        now = time.monotonic()
        duration = int((now - last) * 1000)
        last = now
        step += 1
        if audit is not None:
            error = await audit.record(
                AuditEvent(
                    session_id=session_id,
                    turn_id=turn_id,
                    step_no=step,
                    node=node,
                    event_type=event_type,
                    provider=provider or None,
                    model=model or None,
                    duration_ms=duration,
                    **fields,
                )
            )
            if error and not warned:
                warned = True
                pending.append(
                    TurnEvent(
                        "audit_warning",
                        data="Audit log unavailable: this turn is not being recorded. " + error,
                    )
                )
        return duration

    config = {"configurable": {"thread_id": session_id}, "recursion_limit": RECURSION_LIMIT}
    answered = False
    try:
        await log("user", "user_prompt", input_data=question)
        for event in _drain(pending):
            yield event
        async for update in graph.astream(
            {"messages": [HumanMessage(content=question)]}, config, stream_mode="updates"
        ):
            for node_name, node_output in update.items():
                for message in node_output["messages"]:
                    if isinstance(message, AIMessage):
                        usage = message.usage_metadata or {}
                        tokens = {
                            "prompt_tokens": usage.get("input_tokens"),
                            "completion_tokens": usage.get("output_tokens"),
                        }
                        if message.tool_calls:
                            for call in message.tool_calls:
                                duration = await log(
                                    node_name,
                                    "tool_call",
                                    tool_name=call["name"],
                                    tool_call_id=call["id"],
                                    input_data=json.dumps(call["args"], ensure_ascii=False),
                                    **tokens,
                                )
                                tokens = {}  # record the model's token usage on the first call only
                                pending.append(
                                    TurnEvent(
                                        "tool_call",
                                        call["name"],
                                        call["id"],
                                        call["args"],
                                        duration_ms=duration,
                                    )
                                )
                        else:
                            text = message_text(message.content)
                            finish = str((message.response_metadata or {}).get("finish_reason", "")).lower()
                            if not text.strip() and finish in LENGTH_FINISH_REASONS:
                                # The model spent its whole output limit (reasoning counts) before answering.
                                reason = (
                                    "The model reached its output limit while thinking and gave no answer"
                                    f" (finish_reason={finish}, {tokens.get('completion_tokens')} tokens). "
                                    "Try a narrower question."
                                )
                                answered = True
                                await log(node_name, "error", error_message=reason, **tokens)
                                pending.append(TurnEvent("error", data=reason))
                                continue
                            text = text or NO_ANSWER
                            answered = True
                            duration = await log(node_name, "final_answer", output_data=text, **tokens)
                            pending.append(TurnEvent("answer", data=text, duration_ms=duration))
                    elif isinstance(message, ToolMessage):
                        text = message_text(message.content)
                        status = tool_status(text)
                        duration = await log(
                            node_name,
                            "tool_result",
                            tool_name=message.name,
                            tool_call_id=message.tool_call_id,
                            status=status,
                            output_data=text,
                        )
                        pending.append(
                            TurnEvent(
                                "tool_result",
                                message.name,
                                message.tool_call_id,
                                text,
                                status,
                                duration,
                            )
                        )
                    for event in _drain(pending):
                        yield event
        if not answered:
            await log("graph", "final_answer", output_data=NO_ANSWER)
            pending.append(TurnEvent("answer", data=NO_ANSWER))
    except Exception as exc:  # noqa: BLE001 - a turn never raises; the failure is an event
        friendly = friendly_error(exc)
        await log("graph", "error", error_message=error_text(exc))
        pending.append(TurnEvent("error", data=friendly))
    for event in _drain(pending):
        yield event


# Hand over and clear the events collected so far.
def _drain(pending: list[TurnEvent]) -> list[TurnEvent]:
    events = list(pending)
    pending.clear()
    return events
