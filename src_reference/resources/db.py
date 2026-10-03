"""One place that knows how to open a SQL Server connection, shared by the SQL tool and the audit log."""

from __future__ import annotations

from typing import Any


# Find an installed Microsoft ODBC driver name (18 preferred, then 17), as a JDBC app would pick a driver.
# Input: none. Output: the driver name. Raises RuntimeError if neither is installed.
def pick_driver() -> str:
    # Lazy import: pyodbc needs a system ODBC library, so load it only when a connection is really made.
    import pyodbc

    available = pyodbc.drivers()
    for candidate in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
        if candidate in available:
            return candidate
    raise RuntimeError("No Microsoft ODBC Driver 17/18 for SQL Server is installed.")


# Open a connection as the given login.
# Input: settings (MSSQL_HOST/PORT/DATABASE, with local-Docker defaults), the login and password, and
# timeouts in seconds. Output: a pyodbc connection (autocommit by default). The caller closes it.
def open_connection(
    settings: dict[str, str],
    user: str,
    password: str,
    *,
    login_timeout: int = 10,
    query_timeout: int = 15,
    autocommit: bool = True,
) -> Any:
    import pyodbc

    # Pooled connections closed with unread rows are reused "busy" and fail the next query, so pooling is off.
    pyodbc.pooling = False
    host = settings.get("MSSQL_HOST", "localhost")
    port = settings.get("MSSQL_PORT", "1433")
    database = settings.get("MSSQL_DATABASE", "SupplyChainDb")
    # ODBC wraps values that may hold special characters in braces; `{{` `}}` are literal braces inside an
    # f-string, and a `}` inside the password is doubled (ODBC's escape rule).
    connection_string = (
        f"DRIVER={{{pick_driver()}}};SERVER=tcp:{host},{port};DATABASE={database};"
        f"UID={user};PWD={{{password.replace('}', '}}')}}};Encrypt=yes;TrustServerCertificate=yes;"
    )
    connection = pyodbc.connect(connection_string, timeout=login_timeout, autocommit=autocommit)
    connection.timeout = query_timeout  # per-query timeout, in seconds
    return connection
