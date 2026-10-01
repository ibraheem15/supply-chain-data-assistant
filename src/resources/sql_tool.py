"""query_telemetry_data: run one agent-written SELECT against the governed view.

The database permissions set up in Phase 3b are the real security boundary. The checks
here give the agent fast, readable feedback and cap the cost of a query.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from resources.settings import AGENT_ENV_FILE, load_settings

LOGGER = logging.getLogger("tools.sql")

ALLOWED_VIEW = "VW_SUPPLY_CHAIN_READINGS"
ALLOWED_SCHEMA = "AI"
MAX_ROWS = 200
QUERY_TIMEOUT_SECONDS = 15
LOGIN_TIMEOUT_SECONDS = 10

# ODBC SQLSTATEs: HYT00/HYT01 timeouts, 08xxx connection failures, 28000 login failure.
UNAVAILABLE_PREFIXES = ("HYT", "08", "28")


def validate_sql(sql: str) -> str | None:
    """Return an actionable error message, or None if the query is acceptable."""
    if not sql or not sql.strip():
        return "The query is empty. Provide one SELECT statement."
    try:
        statements = [s for s in sqlglot.parse(sql, read="tsql") if s is not None]
    except SqlglotError as exc:
        return f"Could not parse the query as T-SQL: {str(exc).splitlines()[0]}"
    if len(statements) != 1:
        return "Send exactly one SELECT statement, with no extra statements."
    statement = statements[0]
    if not isinstance(statement, exp.Select):
        return "Only a plain SELECT is allowed (no INSERT, UPDATE, DELETE, DROP, EXEC, UNION)."
    if statement.find(exp.Into) is not None:
        return "SELECT ... INTO is not allowed."

    cte_names = {cte.alias_or_name.upper() for cte in statement.find_all(exp.CTE)}
    for table in statement.find_all(exp.Table):
        name = table.name.upper()
        if name in cte_names and not table.db:
            continue
        if name != ALLOWED_VIEW or (table.db and table.db.upper() != ALLOWED_SCHEMA) or table.catalog:
            return f"Only the view ai.{ALLOWED_VIEW} may be queried; found {table.sql(dialect='tsql')!r}."
    return None


def _clean_odbc_message(exc: Exception) -> str:
    """Reduce a pyodbc error to the server's message, e.g. "Invalid column name 'x'. (207)"."""
    text = str(exc.args[1]) if len(getattr(exc, "args", ())) > 1 else str(exc)
    if "[SQL Server]" in text:
        text = text.split("[SQL Server]", maxsplit=1)[1]
    return text.replace("(SQLExecDirectW)", "").strip()


def _pick_driver() -> str:
    import pyodbc

    available = pyodbc.drivers()
    for candidate in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
        if candidate in available:
            return candidate
    raise RuntimeError("No Microsoft ODBC Driver 17/18 for SQL Server is installed.")


def _connect(settings: dict[str, str]) -> Any:
    import pyodbc

    # Pooled connections closed with unread rows are reused "busy" and fail the next query.
    pyodbc.pooling = False
    host = settings.get("MSSQL_HOST", "localhost")
    port = settings.get("MSSQL_PORT", "1433")
    database = settings.get("MSSQL_DATABASE", "SupplyChainDb")
    user = settings.get("MSSQL_AGENT_USER", "USR_FDE_RO")
    password = settings.get("MSSQL_AGENT_PASSWORD")
    if not password:
        raise RuntimeError(f"MSSQL_AGENT_PASSWORD is missing. Set it in {AGENT_ENV_FILE}.")
    connection_string = (
        f"DRIVER={{{_pick_driver()}}};SERVER=tcp:{host},{port};DATABASE={database};"
        f"UID={user};PWD={{{password.replace('}', '}}')}}};Encrypt=yes;TrustServerCertificate=yes;"
    )
    connection = pyodbc.connect(connection_string, timeout=LOGIN_TIMEOUT_SECONDS, autocommit=True)
    connection.timeout = QUERY_TIMEOUT_SECONDS  # per-query timeout, in seconds
    return connection


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def query_telemetry_data(sql: str) -> dict[str, Any]:
    """Run one SELECT against ai.VW_SUPPLY_CHAIN_READINGS and return rows plus the SQL used."""
    problem = validate_sql(sql)
    if problem:
        return {"status": "error", "sql": sql, "error": problem}

    settings = load_settings(AGENT_ENV_FILE, prefixes=("MSSQL_",))
    password = settings.get("MSSQL_AGENT_PASSWORD", "")

    def redact(message: str) -> str:
        return message.replace(password, "***") if password else message

    connection = None
    try:
        connection = _connect(settings)
        cursor = connection.cursor()
        cursor.execute(sql)
        columns = [column[0] for column in cursor.description]
        fetched = cursor.fetchmany(MAX_ROWS + 1)
    except Exception as exc:  # noqa: BLE001 - every failure must become tool output
        sqlstate = str(exc.args[0]) if getattr(exc, "args", None) else ""
        message = redact(_clean_odbc_message(exc))
        LOGGER.warning("query_telemetry_data failed: %s", message)
        if isinstance(exc, RuntimeError) or sqlstate.startswith(UNAVAILABLE_PREFIXES):
            return {"status": "unavailable", "sql": sql, "error": f"Database unavailable: {message}"}
        return {"status": "error", "sql": sql, "error": f"SQL Server rejected the query: {message}"}
    finally:
        if connection is not None:
            connection.close()

    truncated = len(fetched) > MAX_ROWS
    rows = [[_jsonable(value) for value in row] for row in fetched[:MAX_ROWS]]
    return {
        "status": "ok",
        "sql": sql,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }
