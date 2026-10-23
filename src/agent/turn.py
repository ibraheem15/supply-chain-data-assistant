import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from agent.audit import record
from agent.prompts import DEFAULT_ROLE


# What a front end (the CLI or the UI) receives while a turn runs.
#   kind: "tool_call" | "tool_result" | "answer" | "error" | "audit_warning"
#   text: the readable text (tool arguments as JSON, tool result, answer, error, warning)
#   data: the tool arguments as a dict (tool_call only)
#   tool_call_id: links a tool_result to its tool_call
@dataclass
class TurnEvent:
    kind: str
    text: str = ""
    tool_name: str | None = None
    status: str | None = None
    tool_call_id: str | None = None
    data: Any = None
    duration_ms: int | None = None


def tool_status(text: str) -> str | None:
    """
    The tools return JSON like {"status": "ok", ...}. Returns that status, or None if the text is not JSON.
    """
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value.get("status") if isinstance(value, dict) else None


# An async generator: `async def` with `yield`. The caller reads it with `async for` and receives each event
# while the agent is still working, instead of waiting for the final answer.
async def run_turn(
    graph,
    question: str,
    session_id: str,
    provider: str = "",
    model: str = "",
    role: str = DEFAULT_ROLE,
):
    """
    Runs one question through the graph as the given role (department assistant), writes one audit row
    per step (each row records the role) and yields a TurnEvent per step.
    It never raises: a failure becomes an "error" event. If the audit write fails, one "audit_warning"
    event is yielded for the turn and the turn carries on.
    """
    turn_id = str(uuid.uuid4())  # one id for this question and everything it triggers
    step = 0
    last = time.monotonic()
    warned = False
    pending: list[TurnEvent] = []  # events waiting to be yielded (an inner function cannot yield for us)
    # thread_id keys the memory; role picks the system prompt inside the graph's reasoner node.
    config = {"configurable": {"thread_id": session_id, "role": role}}

    async def log(node: str, event_type: str, **fields) -> int:
        # Writes one audit row and returns its DurationMs: the time since the previous event.
        nonlocal step, last, warned
        now = time.monotonic()
        step += 1
        duration = int((now - last) * 1000)
        event = {
            "SessionId": session_id,
            "TurnId": turn_id,
            "StepNo": step,
            "Node": node,
            "EventType": event_type,
            "Provider": provider or None,
            "Model": model or None,
            "Role": role,
            "DurationMs": duration,
            **fields,
        }
        last = now
        stored = await record(event)
        if not stored and not warned:
            warned = True
            pending.append(
                TurnEvent(
                    "audit_warning",
                    "Audit log unavailable: this turn is not being recorded.",
                )
            )
        return duration

    try:
        await log("user", "user_prompt", InputData=question)
        while pending:
            yield pending.pop(0)

        # stream_mode="updates": one dictionary per finished node, e.g. {"tools": {"messages": [...]}}
        async for update in graph.astream(
            {"messages": [("user", question)]}, config, stream_mode="updates"
        ):
            for node_name, output in update.items():
                for message in output["messages"]:
                    if isinstance(message, AIMessage):
                        usage = message.usage_metadata or {}
                        tokens = {
                            "PromptTokens": usage.get("input_tokens"),
                            "CompletionTokens": usage.get("output_tokens"),
                        }
                        if message.tool_calls:
                            # The model asked for one or more tools.
                            for call in message.tool_calls:
                                arguments = json.dumps(call["args"], ensure_ascii=False)
                                duration = await log(
                                    node_name,
                                    "tool_call",
                                    ToolName=call["name"],
                                    ToolCallId=call["id"],
                                    InputData=arguments,
                                    **tokens,
                                )
                                tokens = {}  # record the model's token usage once per model reply
                                pending.append(
                                    TurnEvent(
                                        "tool_call",
                                        arguments,
                                        call["name"],
                                        tool_call_id=call["id"],
                                        data=call["args"],
                                        duration_ms=duration,
                                    )
                                )
                        else:
                            # No tool requested: this is the final answer.
                            text = message.text or "(The model returned no answer.)"
                            duration = await log(node_name, "final_answer", OutputData=text, **tokens)
                            pending.append(TurnEvent("answer", text, duration_ms=duration))
                    elif isinstance(message, ToolMessage):
                        text = message.text
                        status = tool_status(text)
                        duration = await log(
                            node_name,
                            "tool_result",
                            ToolName=message.name,
                            ToolCallId=message.tool_call_id,
                            Status=status,
                            OutputData=text,
                        )
                        pending.append(
                            TurnEvent(
                                "tool_result",
                                text,
                                message.name,
                                status,
                                tool_call_id=message.tool_call_id,
                                duration_ms=duration,
                            )
                        )
                    while pending:
                        yield pending.pop(0)
    except Exception as e:
        # Anything that goes wrong (quota, network, too many steps) is logged and reported, not raised.
        reason = f"{type(e).__name__}: {e}"
        await log("graph", "error", ErrorMessage=reason)
        pending.append(TurnEvent("error", reason))

    while pending:
        yield pending.pop(0)
