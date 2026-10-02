"""query_telemetry_data: run one agent-written SELECT against the governed view.

The database permissions set up in Phase 3b are the real security boundary. The checks
here give the agent fast, readable feedback and cap the cost of a query.
"""

# Lazy type hints so `str | None` works in annotations (see settings.py).
from __future__ import annotations

# Imports: stdlib types we must convert for JSON (datetime/date/Decimal); sqlglot, a pure-Python SQL parser
# that builds an AST (syntax tree) without touching the database; `exp` holds its node classes.
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from data.ingest_supply_chain_dataset import LEGACY_COLUMN_NAMES
from resources.settings import AGENT_ENV_FILE, load_settings

# Named logger for this tool.
LOGGER = logging.getLogger("tools.sql")

# Dict comprehension that INVERTS business->legacy into UPPERCASE legacy->business, used to detect when
# the agent uses an old SOP-style column name. `.items()` yields (key, value) pairs; upper() makes the
# lookup case-insensitive, because SQL names are.
LEGACY_TO_BUSINESS = {legacy.upper(): business for business, legacy in LEGACY_COLUMN_NAMES.items()}

# Guard-rail constants: the one view and schema the agent may read, a row cap and a character cap (both
# protect the LLM's context window), and timeouts in seconds. Names are uppercase for case-insensitive
# comparison.
ALLOWED_VIEW = "VW_SUPPLY_CHAIN_READINGS"
ALLOWED_SCHEMA = "AI"
MAX_ROWS = 50  # small on purpose: results go into the LLM's context on free-tier token limits
MAX_RESULT_CHARS = 4000  # second cap, for rows that are wide
QUERY_TIMEOUT_SECONDS = 15
LOGIN_TIMEOUT_SECONDS = 10

# SQLSTATE prefixes that mean "database unreachable" rather than "your SQL is wrong". A tuple, because
# str.startswith can test several prefixes at once.
# ODBC SQLSTATEs: HYT00/HYT01 timeouts, 08xxx connection failures, 28000 login failure.
UNAVAILABLE_PREFIXES = ("HYT", "08", "28")


# First line of defence: check the agent's SQL BEFORE it reaches the database.
# Input: the SQL text. Output: an error message string if the query is rejected, or None if acceptable
# (`str | None` is a union type, like a Java nullable return). The real security boundary is the
# read-only DB user; this gives fast, specific feedback so the agent can fix its query and retry.
def validate_sql(sql: str) -> str | None:
    """Return an actionable error message, or None if the query is acceptable."""
    # Empty/None/whitespace-only input (empty strings and None are falsy).
    if not sql or not sql.strip():
        return "The query is empty. Provide one SELECT statement."
    try:
        # Parse the text as T-SQL into a list of AST roots, one per statement. The list comprehension drops
        # None
        # entries (sqlglot yields None for empty statements, e.g. a trailing semicolon).
        statements = [s for s in sqlglot.parse(sql, read="tsql") if s is not None]
    # Syntax error. str(exc).splitlines()[0] keeps only the first line of the message (the rest is noisy).
    except SqlglotError as exc:
        return f"Could not parse the query as T-SQL: {str(exc).splitlines()[0]}"
    # Blocks stacked statements such as `SELECT 1; DROP TABLE x`.
    if len(statements) != 1:
        return "Send exactly one SELECT statement, with no extra statements."
    # From here on we inspect the single statement's AST.
    statement = statements[0]
    # Only a plain SELECT node is allowed. INSERT/UPDATE/DELETE/EXEC/UNION parse as other node types.
    if not isinstance(statement, exp.Select):
        return "Only a plain SELECT is allowed (no INSERT, UPDATE, DELETE, DROP, EXEC, UNION)."
    # AST search: .find returns the first node of that type or None. `SELECT ... INTO` would create a table.
    if statement.find(exp.Into) is not None:
        return "SELECT ... INTO is not allowed."

    # find_all walks the whole syntax tree (a depth-first search, including subqueries) and yields every
    # column
    # reference, like visiting every node of a DOM tree.
    for column in statement.find_all(exp.Column):
        # dict.get returns None if the name is not a legacy one, so `if business_name:` below is a lookup
        # hit test.
        business_name = LEGACY_TO_BUSINESS.get(column.name.upper())
        if business_name:
            # Caught here, with the exact fix, instead of costing a database call and a model retry.
            return f"{column.name} is a legacy name used in SOPs, not a column. Use {business_name}."

    # Set comprehension (braces, no key:value): names of WITH-clause temporary tables (CTEs) the query
    # defined.
    # These look like tables in the AST but are not real tables, so they must be allowed.
    cte_names = {cte.alias_or_name.upper() for cte in statement.find_all(exp.CTE)}
    # Check every table reference in the query, including those in subqueries.
    for table in statement.find_all(exp.Table):
        name = table.name.upper()
        # An unqualified reference to a CTE the query itself defined is fine: skip to the next table
        # (`continue`).
        if name in cte_names and not table.db:
            continue
        # Reject anything other than ai.VW_SUPPLY_CHAIN_READINGS: wrong name, wrong schema, or a
        # database-qualified
        # name (table.catalog).
        if name != ALLOWED_VIEW or (table.db and table.db.upper() != ALLOWED_SCHEMA) or table.catalog:
            # f-string; `!r` inserts repr() (with quotes). Single quotes inside the braces avoid clashing
            # with the
            # outer double quotes. table.sql(...) renders the AST node back to T-SQL text.
            return f"Only the view ai.{ALLOWED_VIEW} may be queried; found {table.sql(dialect='tsql')!r}."
    # Falling through every check means the query is acceptable.
    return None


# Shorten a raw pyodbc exception into just the SQL Server message the agent can act on.
# Input: the exception. Output: a cleaned string. The leading underscore marks it module-private.
def _clean_odbc_message(exc: Exception) -> str:
    """Reduce a pyodbc error to the server's message, e.g. "Invalid column name 'x'. (207)"."""
    # Conditional expression (Java ternary): `A if cond else B`. pyodbc errors carry args=(sqlstate,
    # message);
    # use the message part when present. getattr(obj, name, default) reads an attribute safely.
    text = str(exc.args[1]) if len(getattr(exc, "args", ())) > 1 else str(exc)
    # `in` on strings is a substring test. Drivers prefix messages with noisy "[Microsoft][ODBC Driver]...".
    if "[SQL Server]" in text:
        # Keep only what follows the first "[SQL Server]" marker.
        text = text.split("[SQL Server]", maxsplit=1)[1]
    # Remove a driver-internal suffix and trim whitespace.
    return text.replace("(SQLExecDirectW)", "").strip()


# Find an installed Microsoft ODBC driver name (18 preferred, then 17), as a JDBC app would pick a driver.
# Input: none. Output: the driver name. Raises RuntimeError if neither is installed.
def _pick_driver() -> str:
    # Lazy import inside the function: pyodbc needs a system ODBC library, so it is only loaded when a
    # database call is really made (importing this module stays safe without it).
    import pyodbc

    available = pyodbc.drivers()
    # Iterating a tuple in preference order; the first installed one wins.
    for candidate in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
        if candidate in available:
            return candidate
    raise RuntimeError("No Microsoft ODBC Driver 17/18 for SQL Server is installed.")


# Open a connection to SQL Server as the read-only agent user.
# Input: merged settings dict (host, port, database, user, password). Output: a pyodbc connection.
# Raises RuntimeError when the password is missing; query_telemetry_data reports that as "unavailable".
def _connect(settings: dict[str, str]) -> Any:
    # (Same lazy import as in _pick_driver.)
    import pyodbc

    # Setting a module attribute: pyodbc.pooling is a global switch, like turning off a JDBC connection
    # pool.
    # Pooled connections closed with unread rows are reused "busy" and fail the next query.
    pyodbc.pooling = False
    # Each .get(key, default) reads a setting, falling back to a local-Docker default when it is unset.
    host = settings.get("MSSQL_HOST", "localhost")
    port = settings.get("MSSQL_PORT", "1433")
    database = settings.get("MSSQL_DATABASE", "SupplyChainDb")
    user = settings.get("MSSQL_AGENT_USER", "USR_FDE_RO")
    # No default for the password: it must be configured.
    password = settings.get("MSSQL_AGENT_PASSWORD")
    if not password:
        raise RuntimeError(f"MSSQL_AGENT_PASSWORD is missing. Set it in {AGENT_ENV_FILE}.")
    # ODBC connection string (like a JDBC URL), built from adjacent f-string pieces inside parentheses.
    # In an f-string, `{{` and `}}` are literal braces, and `{expr}` inserts a value. ODBC wraps values that
    # may
    # contain special characters in braces, so `{{{_pick_driver()}}}` renders as `{ODBC Driver 18 ...}`:
    # two literal braces around one expression. The password is quoted the same way, and any `}` inside it
    # is
    # doubled (`replace('}', '}}')`), which is ODBC's escape rule.
    connection_string = (
        f"DRIVER={{{_pick_driver()}}};SERVER=tcp:{host},{port};DATABASE={database};"
        f"UID={user};PWD={{{password.replace('}', '}}')}}};Encrypt=yes;TrustServerCertificate=yes;"
    )
    # timeout= is the login timeout. autocommit=True: we only read, so no transaction is left open.
    connection = pyodbc.connect(connection_string, timeout=LOGIN_TIMEOUT_SECONDS, autocommit=True)
    connection.timeout = QUERY_TIMEOUT_SECONDS  # per-query timeout, in seconds
    return connection


# Convert one database value into something JSON can encode. Dates become ISO-8601 strings, Decimals become
# floats, everything else is returned unchanged. Input: any value. Output: JSON-safe value.
def _jsonable(value: Any) -> Any:
    # isinstance with a `|` union: true if value is a datetime OR a date.
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


# TOOL 1 of 3: run one agent-written SELECT on the governed view.
# Input: SQL text. Output: dict with status "ok" (columns, rows, row_count, truncated), "error" (the
# query is wrong; agent should fix it) or "unavailable" (database problem). Never raises to the agent.
# Flow: validate_sql -> load settings -> connect and fetch -> trim to caps -> JSON-safe dict.
def query_telemetry_data(sql: str) -> dict[str, Any]:
    """Run one SELECT against ai.VW_SUPPLY_CHAIN_READINGS and return rows plus the SQL used."""
    # None means valid; an error string is truthy, so `if problem:` rejects.
    problem = validate_sql(sql)
    if problem:
        return {"status": "error", "sql": sql, "error": problem}

    # `("MSSQL_",)`: the trailing comma makes a one-element tuple (without it, it would just be a string).
    settings = load_settings(AGENT_ENV_FILE, prefixes=("MSSQL_",))
    password = settings.get("MSSQL_AGENT_PASSWORD", "")

    # Nested function (a closure: it can read `password` from the enclosing scope, like a Java lambda
    # capturing a variable). It masks the password in any text before it is returned or logged.
    def redact(message: str) -> str:
        # Ternary: only replace when a password exists (replacing an empty string would mangle the message).
        return message.replace(password, "***") if password else message

    # Declared before `try` so the `finally` block can safely check whether a connection was ever opened.
    connection = None
    try:
        connection = _connect(settings)
        cursor = connection.cursor()
        cursor.execute(sql)
        # cursor.description is a list of tuples describing result columns; element 0 is the column name.
        columns = [column[0] for column in cursor.description]
        # fetchmany(n) reads at most n rows. Asking for one MORE than the cap tells us if extra rows
        # existed,
        # without pulling the entire table.
        fetched = cursor.fetchmany(MAX_ROWS + 1)
    # Deliberately broad catch: any failure (bad SQL, timeout, missing driver) must become a dict, not a
    # crash.
    # Anything caught here is classified below as "unavailable" or "error".
    except Exception as exc:  # noqa: BLE001 - every failure must become tool output
        # pyodbc puts the 5-character SQLSTATE code in args[0]. Ternary guards against exceptions with no
        # args.
        sqlstate = str(exc.args[0]) if getattr(exc, "args", None) else ""
        # Clean, then mask the password, before logging or returning.
        message = redact(_clean_odbc_message(exc))
        LOGGER.warning("query_telemetry_data failed: %s", message)
        # RuntimeError comes from our own checks (missing password/driver); startswith with a tuple matches
        # any
        # of the prefixes (timeouts, connection or login failure). Those are outages; everything else is a
        # problem with the query itself.
        if isinstance(exc, RuntimeError) or sqlstate.startswith(UNAVAILABLE_PREFIXES):
            return {"status": "unavailable", "sql": sql, "error": f"Database unavailable: {message}"}
        return {"status": "error", "sql": sql, "error": f"SQL Server rejected the query: {message}"}
    # Always close the connection, on success or failure (Java: try-with-resources / finally { close() }).
    finally:
        if connection is not None:
            connection.close()

    # True if we received the extra sentinel row, meaning more data existed.
    truncated = len(fetched) > MAX_ROWS
    # A list of rows, each row itself a list of values (annotation is only a hint).
    rows: list[list[Any]] = []
    used_chars = 0
    # Slice [:n] takes the first n items, so the sentinel extra row is dropped.
    for row in fetched[:MAX_ROWS]:
        # List comprehension: make every value in the row JSON-safe.
        converted = [_jsonable(value) for value in row]
        used_chars += len(str(converted))
        # Second cap on total size. `and rows` guarantees at least one row is returned even if it is very
        # wide.
        if used_chars > MAX_RESULT_CHARS and rows:
            truncated = True
            break
        rows.append(converted)
    # Success payload. The "sql" key echoes the executed query so the agent and humans can see it.
    return {
        "status": "ok",
        "sql": sql,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }
