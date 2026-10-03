"""The agent runtime for the UI: one background thread owning the event loop, the MCP session and the graph.

Why a thread: Streamlit re-runs the page script on every click, but the agent is async and keeps ONE
persistent MCP session open. So the session and the graph live here, in a long-lived thread with its
own event loop, and the page submits turns to it and reads events from a queue.

One user at a time: a lock lets a single turn run at once. A second browser tab gets `Busy`.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import uuid
from collections.abc import Callable
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from agent.audit import AuditLogger, AuditReader
from agent.graph import build_graph
from agent.llm import LlmConfigError, build_llm, describe_llm
from agent.tools import mcp_tools
from agent.turn import TurnEvent, error_text, friendly_error, run_turn

LOGGER = logging.getLogger("ui.runtime")


class Busy(Exception):
    """Another turn is already running."""


class NotReady(Exception):
    """The agent has not finished starting (or failed to start)."""


class AgentRuntime:
    """Starts the agent in the background and runs turns on request."""

    def __init__(
        self,
        llm: Any = None,
        tools_factory: Callable[[], Any] = mcp_tools,
        audit: AuditLogger | None = None,
        audit_enabled: bool = True,
    ) -> None:
        self._llm = llm
        self._tools_factory = tools_factory
        self._audit_enabled = audit_enabled
        self.audit = audit
        # Status, read by the page. Plain attributes: simple values written by one thread.
        self.state = "starting"  # starting | warming | ready | error
        self.error: str | None = None
        self.tool_count = 0
        self.sop_warm = False
        self.audit_error: str | None = None
        self.provider = ""
        self.model = ""
        self._graph: Any = None
        self._turn_lock = threading.Lock()
        self._stop: asyncio.Event | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="agent-runtime", daemon=True)

    def start(self) -> None:
        self._thread.start()

    # Body of the background thread: run the event loop until `stop()`.
    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self) -> None:
        self._stop = asyncio.Event()
        try:
            llm = self._llm or build_llm()
            self.provider, self.model = describe_llm(llm)
            if self._audit_enabled:
                self.audit = self.audit or AuditLogger()
                self.audit_error = await asyncio.to_thread(self.audit.ping)
            async with self._tools_factory() as tools:
                self._graph = build_graph(llm, tools, MemorySaver())
                self.tool_count = len(tools)
                self.state = "warming"
                await self._warm_up(tools)
                self.state = "ready"
                await self._stop.wait()  # keep the MCP session open until stop() is called
        except LlmConfigError as exc:
            self.state, self.error = "error", str(exc)
        except Exception as exc:  # noqa: BLE001 - startup failures are shown, never thrown at the page
            LOGGER.warning("Agent start-up failed: %s", error_text(exc))
            self.state, self.error = "error", friendly_error(exc)

    # Load the SOP embedding model now, so the user's first SOP question does not wait ~40 seconds.
    async def _warm_up(self, tools: list[Any]) -> None:
        for tool in tools:
            if tool.name == "search_compliance_sop":
                try:
                    await tool.ainvoke({"query": "warm-up", "top_k": 1})
                    self.sop_warm = True
                except Exception as exc:  # noqa: BLE001 - warm-up is best effort
                    LOGGER.warning("SOP warm-up failed: %s", error_text(exc))

    def stop(self) -> None:
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)

    # A fresh chat id. Memory is keyed by it, so a new id is an empty conversation.
    @staticmethod
    def new_session_id() -> str:
        return str(uuid.uuid4())

    # Start one turn. Returns a queue of TurnEvent that ends with None. Raises NotReady or Busy.
    def submit(self, question: str, session_id: str) -> queue.Queue[TurnEvent | None]:
        if self.state != "ready":
            raise NotReady(self.error or "The agent is still starting.")
        if not self._turn_lock.acquire(blocking=False):
            raise Busy("Another turn is still running.")
        events: queue.Queue[TurnEvent | None] = queue.Queue()
        asyncio.run_coroutine_threadsafe(self._turn(events, question, session_id), self._loop)
        return events

    async def _turn(self, events: queue.Queue, question: str, session_id: str) -> None:
        try:
            async for event in run_turn(
                self._graph,
                question,
                session_id,
                audit=self.audit if self.audit_error is None else None,
                provider=self.provider,
                model=self.model,
            ):
                events.put(event)
        finally:
            events.put(None)  # tells the page the turn is over
            self._turn_lock.release()


def create_runtime() -> AgentRuntime:
    return AgentRuntime()


def create_reader() -> AuditReader:
    return AuditReader()
