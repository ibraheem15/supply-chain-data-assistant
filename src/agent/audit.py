import asyncio
from functools import lru_cache

from resources.db import open_connection
from resources.settings import (
    AGENT_SQL_ENV_FILE,
    AUDIT_SQL_ENV_FILE,
    LLM_ENV_FILE,
    VECTORDB_ENV_FILE,
    load_settings,
)

# The columns an event may fill in. Column names go into the SQL text (placeholders cannot be used for
# names), so this whitelist is what keeps unexpected dictionary keys out of the SQL.
# LogId and LoggedAtUtc are missing on purpose: the database sets them itself.
COLUMNS = (
    "SessionId",
    "TurnId",
    "StepNo",
    "Node",
    "EventType",
    "ToolName",
    "ToolCallId",
    "Status",
    "InputData",
    "OutputData",
    "ErrorMessage",
    "Provider",
    "Model",
    "DurationMs",
    "PromptTokens",
    "CompletionTokens",
)

# A setting is treated as a secret when its name contains one of these words.
SECRET_WORDS = ("PASSWORD", "KEY", "TOKEN", "SECRET")
MIN_SECRET_LENGTH = 8  # very short values would blank out ordinary text by accident


# @lru_cache: the env files are read once, not on every audit row.
@lru_cache(maxsize=1)
def _secret_values() -> tuple[str, ...]:
    """
    Collects the real secret values (API keys, passwords) from the env files.
    Longest first, so a secret that contains another one is replaced as a whole.
    """
    settings = load_settings(
        AGENT_SQL_ENV_FILE, AUDIT_SQL_ENV_FILE, LLM_ENV_FILE, VECTORDB_ENV_FILE
    )
    values = {
        value
        for name, value in settings.items()
        if value
        and len(value) >= MIN_SECRET_LENGTH
        and any(word in name.upper() for word in SECRET_WORDS)
    }
    return tuple(sorted(values, key=len, reverse=True))


def redact(text: str) -> str:
    """
    Replaces every secret value found in the text with ***, so the audit log never stores a key or password.
    """
    for secret in _secret_values():
        text = text.replace(secret, "***")
    return text


def _insert(event: dict) -> None:
    """
    Inserts one audit row as the insert-only writer login. Blocking code: call it through record().
    """
    # Keep only known columns that the event actually carries; strings are redacted.
    columns = [column for column in COLUMNS if column in event]
    values = [
        redact(event[column]) if isinstance(event[column], str) else event[column]
        for column in columns
    ]

    column_list = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    # ? placeholders: the values travel separately from the SQL text, so a prompt that contains SQL is
    # stored as plain text and can never run as SQL.
    sql = f"INSERT INTO audit_2.AgentAuditLog ({column_list}) VALUES ({placeholders})"

    settings = load_settings(AGENT_SQL_ENV_FILE, AUDIT_SQL_ENV_FILE)
    connection = None
    try:
        connection = open_connection(
            settings, "MSSQL_AUDIT_WRITER_USER", "MSSQL_AUDIT_WRITER_PASSWORD"
        )
        cursor = connection.cursor()
        cursor.execute(sql, values)
        connection.commit()  # pyodbc does not auto-commit: without this the row is rolled back
    finally:
        if connection is not None:
            connection.close()


async def record(event: dict) -> bool:
    """
    Writes one audit row without blocking the chat and without ever raising.
    Returns True when the row was stored and False when it was not, so the caller can show a warning.
    """
    try:
        # asyncio.to_thread runs the blocking pyodbc call in a worker thread, so the event loop
        # (and the chat) is not frozen while the database answers.
        await asyncio.to_thread(_insert, event)
        return True
    except Exception:
        # Deliberately broad: a broken log must never take the agent down. It is not silent,
        # because the caller gets False and reports "this turn was not recorded".
        return False


def ping() -> str | None:
    """
    Checks that the writer login can connect. Returns None when it can, otherwise a short error text.
    The UI calls this at start-up to show whether the audit log is on.
    """
    try:
        settings = load_settings(AGENT_SQL_ENV_FILE, AUDIT_SQL_ENV_FILE)
        open_connection(
            settings, "MSSQL_AUDIT_WRITER_USER", "MSSQL_AUDIT_WRITER_PASSWORD"
        ).close()
        return None
    except Exception as e:
        return redact(str(e))


MAX_SEARCH_ROWS = 200


def _like_pattern(text: str) -> str:
    # In T-SQL LIKE, % _ and [ are wildcards. Wrapping each in [] makes it match literally.
    escaped = text.replace("[", "[[]").replace("%", "[%]").replace("_", "[_]")
    return f"%{escaped}%"


class AuditReader:
    """
    Reads the audit log through the select-only login. It can never write.
    Blocking code: the UI calls it directly (it is fast), not from the agent's event loop.
    """

    def _connect(self):
        settings = load_settings(AGENT_SQL_ENV_FILE, AUDIT_SQL_ENV_FILE)
        return open_connection(
            settings, "MSSQL_AUDIT_READER_USER", "MSSQL_AUDIT_READER_PASSWORD"
        )

    def _query(self, sql: str, params: list) -> list[dict]:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute(sql, params)
            names = [column[0] for column in cursor.description]
            # Dates become text, so the UI can slice and show them without caring about types.
            return [
                {
                    name: str(value) if hasattr(value, "isoformat") else value
                    for name, value in zip(names, row)
                }
                for row in cursor.fetchall()
            ]
        finally:
            connection.close()

    def sessions(self) -> list[dict]:
        """
        One row per chat: SessionId, StartedUtc, Events. Newest chat first.
        """
        return self._query(
            "SELECT TOP 100 SessionId, MIN(LoggedAtUtc) AS StartedUtc, COUNT(*) AS Events "
            "FROM audit_2.AgentAuditLog GROUP BY SessionId ORDER BY MIN(LoggedAtUtc) DESC",
            [],
        )

    def search(
        self,
        session_id: str = "",
        tool_name: str = "",
        status: str = "",
        event_type: str = "",
        since=None,
        until=None,
        text: str | None = None,
    ) -> list[dict]:
        """
        Returns up to 200 rows, newest first. Every filter is optional.
        Values always travel as ? parameters, never inside the SQL text.
        """
        conditions, params = [], []
        for column, value in (
            ("SessionId", session_id),
            ("ToolName", tool_name),
            ("Status", status),
            ("EventType", event_type),
        ):
            if value:
                conditions.append(f"{column} = ?")
                params.append(value)
        if since is not None:
            conditions.append("LoggedAtUtc >= ?")
            params.append(since)
        if until is not None:
            conditions.append("LoggedAtUtc <= ?")
            params.append(until)
        if text:
            conditions.append(
                "(InputData LIKE ? OR OutputData LIKE ? OR ErrorMessage LIKE ?)"
            )
            params.extend([_like_pattern(text)] * 3)

        where = f"WHERE {' AND '.join(conditions)} " if conditions else ""
        sql = (
            f"SELECT TOP {MAX_SEARCH_ROWS} LogId, LoggedAtUtc, SessionId, TurnId, StepNo, Node, EventType, "
            "ToolName, ToolCallId, Status, InputData, OutputData, ErrorMessage, Provider, Model, "
            "DurationMs, PromptTokens, CompletionTokens "
            f"FROM audit_2.AgentAuditLog {where}ORDER BY LogId DESC"
        )
        return self._query(sql, params)
