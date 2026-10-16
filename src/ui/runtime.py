"""The agent runtime for the UI: one background thread owning the event loop, the MCP session and the graph.

Why a thread: Streamlit re-runs the page script on every click, but the agent is async and keeps ONE
persistent MCP session open. So the session and the graph live here, in a long-lived thread with its
own event loop, and the page submits turns to it and reads events from a queue.

One user at a time: a lock lets a single turn run at once. A second browser tab gets `Busy`.
"""

import asyncio
import queue
import sys
import threading
import uuid

from agent.audit import AuditReader, ping
from agent.graph import build_graph
from agent.llm import build_llm, describe_llm
from agent.tools import open_adapter
from agent.turn import TurnEvent, run_turn


class Busy(Exception):
    """Another turn is already running."""


class NotReady(Exception):
    """The agent has not finished starting (or failed to start)."""


class AgentRuntime:
    """Starts the agent in the background and runs turns on request."""

    def __init__(self) -> None:
        # Status, read by the page. Plain attributes: simple values written by one thread.
        self.state = "starting"  # starting | warming | ready | error
        self.error: str | None = None
        self.tool_count = 0
        self.sop_warm = False
        self.audit_error: str | None = None  # None means the audit log can be written
        self.provider = ""
        self.model = ""
        self._graph = None
        self._turn_lock = threading.Lock()
        self._stop: asyncio.Event | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="agent-runtime", daemon=True)

    def start(self) -> None:
        self._thread.start()

    # Body of the background thread: run the event loop until stop() is called.
    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self) -> None:
        self._stop = asyncio.Event()
        try:
            self.provider, self.model = describe_llm()
            llm = build_llm()
            self.audit_error = await asyncio.to_thread(ping)
            # The same pattern as cli.py: the whole session lives inside the async with.
            async with open_adapter() as adapter:
                tools = await adapter.list_tools()
                self._graph = build_graph(llm, tools)
                self.tool_count = len(tools)
                self.state = "warming"
                await self._warm_up(tools)
                self.state = "ready"
                await self._stop.wait()  # keep the MCP session open until stop() is called
        except Exception as e:  # start-up failures are shown on the page, never thrown at it
            self.state, self.error = "error", f"{type(e).__name__}: {e}"

    # Load the SOP embedding model now, so the user's first SOP question does not wait for it.
    async def _warm_up(self, tools: list) -> None:
        for tool in tools:
            if tool.name == "search_compliance_sop":
                try:
                    await tool.ainvoke({"query": "warm-up"})
                    self.sop_warm = True
                except Exception as e:  # warm-up is best effort
                    print(f"SOP warm-up failed: {e}", file=sys.stderr)

    def stop(self) -> None:
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)

    # A fresh chat id. Memory is keyed by it, so a new id is an empty conversation.
    @staticmethod
    def new_session_id() -> str:
        return str(uuid.uuid4())

    # Start one turn. Returns a queue of TurnEvent that ends with None. Raises NotReady or Busy.
    def submit(self, question: str, session_id: str) -> "queue.Queue[TurnEvent | None]":
        if self.state != "ready":
            raise NotReady(self.error or "The agent is still starting.")
        if not self._turn_lock.acquire(blocking=False):
            raise Busy("Another turn is still running.")
        events: queue.Queue = queue.Queue()
        asyncio.run_coroutine_threadsafe(self._turn(events, question, session_id), self._loop)
        return events

    async def _turn(self, events: queue.Queue, question: str, session_id: str) -> None:
        try:
            async for event in run_turn(
                self._graph, question, session_id, self.provider, self.model
            ):
                events.put(event)
        finally:
            events.put(None)  # tells the page the turn is over
            self._turn_lock.release()


def create_runtime() -> AgentRuntime:
    return AgentRuntime()


def create_reader() -> AuditReader:
    return AuditReader()
