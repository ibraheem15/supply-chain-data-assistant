"""Audit log access: an insert-only writer and a read-only reader (Phase 5a).

Two different database logins on purpose, so that "append-only" is enforced by SQL Server itself:
  * USR_FDE_AUDIT_W can only INSERT into audit.AgentAuditLog.
  * USR_FDE_AUDIT_R can only SELECT from it.
Neither function here ever raises into the chat: a broken audit log must not take the agent down.
It returns an error message instead, and the caller shows a warning ("notify and carry on").
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import date, datetime
from typing import Any

from resources.db import open_connection
from resources.settings import (
    AGENT_ENV_FILE,
    AUDIT_ENV_FILE,
    LLM_ENV_FILE,
    VECTORDB_ENV_FILE,
    load_settings,
    read_env_file,
)

LOGGER = logging.getLogger("agent.audit")

TABLE = "[audit].[AgentAuditLog]"
# Environment variable names that hold secrets contain one of these words.
SECRET_NAME_PARTS = ("KEY", "PASSWORD", "TOKEN", "SECRET")
MIN_SECRET_LENGTH = 6  # shorter values would redact ordinary words


# One row of the audit table. frozen=True makes it immutable (like a Java record), and the field names
# are exactly the column names in snake_case, in insert order.
@dataclass(frozen=True)
class AuditEvent:
    session_id: str
    turn_id: str
    step_no: int
    node: str  # user | reasoner | tools | graph
    event_type: str  # user_prompt | tool_call | tool_result | final_answer | error
    tool_name: str | None = None
    tool_call_id: str | None = None
    status: str | None = None  # ok | error | unavailable (tool results)
    input_data: str | None = None
    output_data: str | None = None
    error_message: str | None = None
    provider: str | None = None
    model: str | None = None
    duration_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


# Table column names in the same order as AuditEvent's fields: "session_id" -> "SessionId".
# `"".join(part.title() for part in name.split("_"))` upper-cases the first letter of each word.
# Token fields keep their natural names: prompt_tokens -> PromptTokens.
COLUMNS = tuple("".join(part.title() for part in f.name.split("_")) for f in fields(AuditEvent))


# All secret values currently configured (API keys, passwords), read from the env files and the real
# environment. Used to scrub them out of anything written to the log.
def collect_secrets() -> list[str]:
    values: dict[str, str] = {}
    for env_file in (LLM_ENV_FILE, AGENT_ENV_FILE, AUDIT_ENV_FILE, VECTORDB_ENV_FILE):
        values.update(read_env_file(env_file))
    values.update(os.environ)
    secrets = {
        value
        for name, value in values.items()
        if any(part in name.upper() for part in SECRET_NAME_PARTS) and len(value) >= MIN_SECRET_LENGTH
    }
    # Longest first, so a secret that contains another secret is replaced whole.
    return sorted(secrets, key=len, reverse=True)


# Replace every known secret in `text` with ***. None passes through unchanged.
def redact(text: str | None, secrets: list[str]) -> str | None:
    if text is None:
        return None
    for secret in secrets:
        text = text.replace(secret, "***")
    return text


# ----------------------------------------------------------------------------------------------
# Writer
# ----------------------------------------------------------------------------------------------
class AuditLogger:
    """Writes audit events as USR_FDE_AUDIT_W. Every method returns an error message, never raises."""

    def __init__(
        self,
        settings: dict[str, str] | None = None,
        connect: Callable[..., Any] | None = None,
        secrets: list[str] | None = None,
    ) -> None:
        # Both arguments can be injected so tests need neither a database nor real secrets.
        self.settings = (
            settings if settings is not None else load_settings(AUDIT_ENV_FILE, prefixes=("MSSQL_",))
        )
        self._connect = connect or open_connection
        self._secrets = secrets if secrets is not None else collect_secrets()
        self.last_error: str | None = None

    # Open a connection as the writer login. Raises if the password is not configured.
    def _open(self) -> Any:
        password = self.settings.get("MSSQL_AUDIT_WRITER_PASSWORD")
        if not password:
            raise RuntimeError(f"MSSQL_AUDIT_WRITER_PASSWORD is missing. Set it in {AUDIT_ENV_FILE}.")
        user = self.settings.get("MSSQL_AUDIT_WRITER_USER", "USR_FDE_AUDIT_W")
        return self._connect(self.settings, user, password)

    # Insert one event (blocking). Returns None on success, or a redacted error message.
    def _insert(self, event: AuditEvent) -> str | None:
        connection = None
        try:
            connection = self._open()
            values = [getattr(event, f.name) for f in fields(AuditEvent)]
            # Free text goes through redact() first; everything else (numbers, ids) is stored as is.
            values = [redact(v, self._secrets) if isinstance(v, str) else v for v in values]
            placeholders = ", ".join("?" for _ in values)
            connection.cursor().execute(
                f"INSERT INTO {TABLE} ({', '.join(COLUMNS)}) VALUES ({placeholders})", values
            )
            self.last_error = None
            return None
        except Exception as exc:  # noqa: BLE001 - the audit log must never break the chat
            self.last_error = redact(str(exc), self._secrets) or "unknown error"
            LOGGER.warning("Audit write failed: %s", self.last_error)
            return self.last_error
        finally:
            if connection is not None:
                connection.close()

    # Insert one event without blocking the event loop (the database call runs in a worker thread).
    async def record(self, event: AuditEvent) -> str | None:
        return await asyncio.to_thread(self._insert, event)

    # A cheap connectivity check for status displays: open and close a connection.
    def ping(self) -> str | None:
        connection = None
        try:
            connection = self._open()
            return None
        except Exception as exc:  # noqa: BLE001
            return redact(str(exc), self._secrets) or "unknown error"
        finally:
            if connection is not None:
                connection.close()


# ----------------------------------------------------------------------------------------------
# Reader
# ----------------------------------------------------------------------------------------------
# Escape the characters that mean something inside a SQL LIKE pattern, so user text is matched literally.
def like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("[", "\\[")
    return f"%{escaped}%"


class AuditReader:
    """Reads audit rows as USR_FDE_AUDIT_R (SELECT only). Used by the UI's audit viewer."""

    def __init__(self, settings: dict[str, str] | None = None, connect: Callable[..., Any] | None = None):
        self.settings = (
            settings if settings is not None else load_settings(AUDIT_ENV_FILE, prefixes=("MSSQL_",))
        )
        self._connect = connect or open_connection

    def _open(self) -> Any:
        password = self.settings.get("MSSQL_AUDIT_READER_PASSWORD")
        if not password:
            raise RuntimeError(f"MSSQL_AUDIT_READER_PASSWORD is missing. Set it in {AUDIT_ENV_FILE}.")
        user = self.settings.get("MSSQL_AUDIT_READER_USER", "USR_FDE_AUDIT_R")
        return self._connect(self.settings, user, password)

    # Run a SELECT and return the rows as a list of dicts (column name -> value, datetimes as ISO text).
    def _query(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        connection = self._open()
        try:
            cursor = connection.cursor()
            cursor.execute(sql, params)
            names = [column[0] for column in cursor.description]
            rows = cursor.fetchall()
        finally:
            connection.close()
        return [
            {
                name: (value.isoformat(sep=" ") if isinstance(value, datetime | date) else value)
                for name, value in zip(names, row, strict=True)
            }
            for row in rows
        ]

    # Recent chats: one row per session with its start time and event count. Newest first.
    def sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._query(
            f"SELECT TOP (?) [SessionId], MIN([LoggedAtUtc]) AS [StartedUtc], COUNT_BIG(*) AS [Events] "
            f"FROM {TABLE} GROUP BY [SessionId] ORDER BY MIN([LoggedAtUtc]) DESC",
            [limit],
        )

    # Filtered events, newest first. Every filter is optional; all values are bound parameters
    # (never pasted into the SQL), so a filter value cannot change the query.
    def search(
        self,
        *,
        session_id: str | None = None,
        tool_name: str | None = None,
        status: str | None = None,
        event_type: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        text: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("SessionId", session_id),
            ("ToolName", tool_name),
            ("Status", status),
            ("EventType", event_type),
        ):
            if value:
                clauses.append(f"[{column}] = ?")
                params.append(value)
        if since:
            clauses.append("[LoggedAtUtc] >= ?")
            params.append(since)
        if until:
            clauses.append("[LoggedAtUtc] < ?")
            params.append(until)
        if text:
            pattern = like_pattern(text)
            clauses.append(
                "([InputData] LIKE ? ESCAPE '\\' OR [OutputData] LIKE ? ESCAPE '\\' "
                "OR [ErrorMessage] LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern, pattern, pattern])
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        return self._query(f"SELECT TOP (?) * FROM {TABLE} {where}ORDER BY [LogId] DESC", [limit, *params])

    # Connectivity check for status displays. None means the reader can connect.
    def ping(self) -> str | None:
        try:
            self._open().close()
            return None
        except Exception as exc:  # noqa: BLE001
            return str(exc)
