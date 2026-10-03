"""Load the raw supply-chain CSV into the local SQL Server database."""

# Python idiom: this makes type hints lazy strings, so `X | None` and forward references work
# uniformly. Think of it as a compiler flag; you can mostly ignore it.
from __future__ import annotations

# Imports (Java: `import`). All of these are standard library; no pip packages are needed here.
# argparse = CLI flag parser, csv = CSV reader, logging = log4j/slf4j, math = Math.*,
# os = env vars, re = regex, sys = exit codes, Path = java.nio.file.Path.
import argparse
import csv
import logging
import math
import os
import re
import sys
# `from X import Y` pulls one name into scope (like a Java static import of a single class).
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# Module-level constants (Java: `static final`). UPPER_CASE is the convention; Python does not enforce it.
# __file__ is this script's path; .resolve() makes it absolute; .parents[2] walks up two directories
# (src/data/file.py -> src/data -> src -> project root). `/` on a Path joins path segments.
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV = ROOT / "data" / "raw" / "dynamic_supply_chain_logistics_dataset.csv"
DEFAULT_ENV_FILE = ROOT / "envs" / "legacy_sqlserver.env"
DATABASE_TABLE = "dbo.supply_chain_logistics"
STAGING_TABLE = "dbo.supply_chain_logistics_staging"
# A set literal {..}: unordered, unique values, O(1) membership tests (`x in RISK_LEVELS`).
RISK_LEVELS = {"Low Risk", "Moderate Risk", "High Risk"}

# A tuple `( ... )`: an immutable ordered list. This is the canonical list of CSV columns and the
# order in which values are inserted into SQL. Trailing comma after the last item is allowed.
COLUMNS = (
    "timestamp",
    "vehicle_gps_latitude",
    "vehicle_gps_longitude",
    "fuel_consumption_rate",
    "eta_variation_hours",
    "traffic_congestion_level",
    "warehouse_inventory_level",
    "loading_unloading_time",
    "handling_equipment_availability",
    "order_fulfillment_status",
    "weather_condition_severity",
    "port_congestion_level",
    "shipping_costs",
    "supplier_reliability_score",
    "lead_time_days",
    "historical_demand",
    "iot_temperature",
    "cargo_condition_status",
    "route_risk_level",
    "customs_clearance_time",
    "driver_behavior_score",
    "fatigue_monitoring_score",
    "disruption_likelihood_score",
    "delay_probability",
    "risk_classification",
    "delivery_time_deviation",
)

# dict literal (Java Map). Columns not listed here default to FLOAT (see _table_ddl).
SQL_TYPES = {
    "timestamp": "DATETIME2(0)",
    "risk_classification": "NVARCHAR(30)",
}

# The legacy SQL Server schema uses terse, historically-accumulated column names.
# Our domain code speaks COLUMNS everywhere; this mapping is the one place that
# translates to the legacy names, used only when generating SQL identifiers.
LEGACY_COLUMN_NAMES = {
    "timestamp": "TM_UTC",
    "vehicle_gps_latitude": "VEH_LAT",
    "vehicle_gps_longitude": "VEH_LON",
    "fuel_consumption_rate": "FUEL_RT_LPH",
    "eta_variation_hours": "ETA_VAR_HR",
    "traffic_congestion_level": "TRF_CONG_LVL",
    "warehouse_inventory_level": "WH_INV_LVL",
    "loading_unloading_time": "LD_UNLD_TM",
    "handling_equipment_availability": "HDL_EQP_AVAIL",
    "order_fulfillment_status": "ORD_FULFIL_ST",
    "weather_condition_severity": "WX_SEV",
    "port_congestion_level": "PORT_CONG_LVL",
    "shipping_costs": "SHIP_COST_USD",
    "supplier_reliability_score": "SUPL_REL_SCR",
    "lead_time_days": "LEAD_TM_DY",
    "historical_demand": "HIST_DMD",
    "iot_temperature": "IOT_TEMP_C",
    "cargo_condition_status": "CGO_COND_ST",
    "route_risk_level": "RT_RISK_LVL",
    "customs_clearance_time": "CUST_CLR_TM",
    "driver_behavior_score": "DRV_BHVR_SCR",
    "fatigue_monitoring_score": "FTG_MON_SCR",
    "disruption_likelihood_score": "DISR_LKHD_SCR",
    "delay_probability": "DELAY_PROB",
    "risk_classification": "RISK_CLASS",
    "delivery_time_deviation": "DEL_TM_DEV",
}

# Named logger (like LoggerFactory.getLogger("name")); configured later in main().
LOGGER = logging.getLogger("supply_chain_ingest")


# A custom exception type (Java: `class X extends Exception`). Used for failures we expect and can
# explain to the user, as opposed to unexpected bugs. The docstring alone is a valid class body.
class IngestionError(Exception):
    """An expected, actionable ingestion failure."""


# @dataclass auto-generates __init__, __repr__ and __eq__ from the field annotations (like a Java record).
# frozen=True makes instances immutable (no setters), so it behaves like a value object.
# Holds the validated connection settings that flow from load_config() to publish_records().
@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    database: str
    username: str
    password: str
    driver: str


# Parses a .env style file into a dict. In: path. Out: {KEY: value} (empty dict if the file is missing).
# Roughly what a dotenv library or Java Properties.load() does. `-> dict[str, str]` is a return type hint.
def read_env_file(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE entries without logging their values."""
    # Annotated local variable: `name: type = value`. Hints are documentation; Python does not enforce them.
    values: dict[str, str] = {}
    if not path.exists():
        return values

    # enumerate(iterable, start=1) yields (index, item) pairs; start=1 gives human-friendly line numbers.
    # `for a, b in ...` is tuple unpacking. splitlines() splits on newlines without trailing newline chars.
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        # Skip blanks, comments and malformed lines. An empty string is falsy, so `not stripped` means empty.
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        # split("=", maxsplit=1) splits only on the first '=', so values may contain '='. The two results
        # are unpacked into key and value (tuple unpacking).
        key, value = stripped.split("=", maxsplit=1)
        key = key.strip()
        value = value.strip()
        # Strip matching surrounding quotes: value[0] is the first char, value[-1] the last (negative index
        # counts from the end), and `in {"'", '"'}` tests membership in a set. value[1:-1] is a slice that
        # drops the first and last characters.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if not key:
            # f-string: the f prefix lets {expr} be evaluated and embedded in the text.
            raise IngestionError(f"Invalid environment entry on line {line_number} of {path}.")
        values[key] = value
    return values


# Resolve one setting with precedence: real process env var > env file value > default.
# `str | None` is a union type: the result is either a string or None (like Java's @Nullable).
# `default: str | None = None` is a parameter with a default value (Java would need an overload).
# dict.get(key, fallback) returns the fallback instead of raising when the key is absent.
# The leading underscore in the name is a convention for 'private to this module'.
def _setting(name: str, file_values: dict[str, str], default: str | None = None) -> str | None:
    return os.environ.get(name, file_values.get(name, default))


# Picks the ODBC driver (the native library pyodbc uses to talk to SQL Server).
# In: the driver name the user asked for, or None. Out: the name of a driver that is installed.
def _choose_driver(requested: str | None) -> str:
    # try/except ImportError: pyodbc is imported lazily here so --dry-run works without it installed.
    # `except ImportError as exc` is like `catch (ImportError exc)` in Java.
    try:
        import pyodbc
    except ImportError as exc:
        # Adjacent string literals are concatenated at compile time, so these lines form one message.
        # `raise ... from exc` chains the original error as the cause (Java: `new X(msg, cause)`).
        raise IngestionError(
            "Could not import pyodbc. If the pyodbc package itself is missing, run `uv sync`. "
            "If pyodbc is installed but this still fails (e.g. "
            '"libodbc.so.2: cannot open shared object file"), the host is missing the '
            "unixODBC driver manager system library — install it via your OS package manager "
            f"(underlying error: {exc})."
        ) from exc

    # Lists the ODBC drivers installed on this machine.
    available = pyodbc.drivers()
    # `if requested:` is a truthiness test: None and empty string are both false.
    if requested:
        if requested not in available:
            raise IngestionError(
                f"ODBC driver {requested!r} is not installed. Available drivers: "
                # ', '.join(seq) glues strings with a separator; `or '(none)'` is a fallback when empty.
                f"{', '.join(available) or '(none)'}."
            )
        return requested

    # Try drivers in order of preference (newest first); return the first one that is available.
    for candidate in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
        if candidate in available:
            return candidate
    raise IngestionError(
        "Microsoft ODBC Driver 18 or 17 for SQL Server is required. "
        "Install a Microsoft SQL Server ODBC driver and try again."
    )


# Builds a validated DatabaseConfig. Precedence per field: CLI flag > env var > env file > default.
# In: parsed CLI args (argparse.Namespace) and the env file path. Out: DatabaseConfig.
# Raises IngestionError if anything is invalid. Called by main() just before publishing.
def load_config(args: argparse.Namespace, env_file: Path) -> DatabaseConfig:
    file_values = read_env_file(env_file)
    # `a or b` returns the first truthy operand, so a missing CLI flag (None) falls through to the setting.
    host = args.host or _setting("MSSQL_HOST", file_values, "127.0.0.1")
    # Conditional expression (Java ternary): `X if cond else Y`. Here 0 is a legal CLI value so we test
    # `is not None` rather than truthiness.
    port_value = args.port if args.port is not None else _setting("MSSQL_PORT", file_values, "1433")
    port_text = str(port_value)
    database = args.database or _setting("MSSQL_DATABASE", file_values, "SupplyChainDb")
    username = args.username or _setting("MSSQL_USER", file_values, "sa")
    password = _setting("MSSQL_SA_PASSWORD", file_values)
    requested_driver = args.driver or _setting("MSSQL_ODBC_DRIVER", file_values)

    # Safety guard: this loader refuses to talk to any non-local server.
    if host is None or host.lower() not in {"localhost", "127.0.0.1"}:
        raise IngestionError(
            "This Phase 1 loader only connects to the local Docker SQL Server. "
            "Set MSSQL_HOST to localhost or 127.0.0.1."
        )
    # int() raises ValueError on bad text (or TypeError on None); we convert both into a friendly error.
    try:
        port = int(port_text)
    except (TypeError, ValueError) as exc:
        raise IngestionError("MSSQL_PORT must be an integer from 1 to 65535.") from exc
    # Chained comparison: `1 <= port <= 65535` means 1 <= port and port <= 65535.
    if not 1 <= port <= 65535:
        raise IngestionError("MSSQL_PORT must be an integer from 1 to 65535.")
    # re.fullmatch requires the WHOLE string to match. Pattern: one letter, then 0-127 letters/digits/
    # underscores. This whitelist protects the f-string SQL below from injection via the database name.
    if not database or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", database):
        raise IngestionError("MSSQL_DATABASE must be a valid SQL Server database identifier.")
    if not username:
        raise IngestionError("MSSQL_USER must not be empty.")
    if not password:
        raise IngestionError(
            f"MSSQL_SA_PASSWORD is missing. Set it in {env_file} or the process environment."
        )

    # Keyword arguments (name=value) construct the frozen dataclass.
    return DatabaseConfig(
        host=host,
        port=port,
        database=database,
        username=username,
        password=password,
        driver=_choose_driver(requested_driver),
    )


# Small factory that builds (not raises) a consistent error message for a bad CSV cell.
# The caller does `raise _row_error(...)`.
def _row_error(record_number: int, column: str, reason: str) -> IngestionError:
    return IngestionError(f"CSV record {record_number} has {reason} in column {column}.")


# Reads and strictly validates the CSV. In: file path. Out: a list of tuples, one per data row, with
# values already converted (datetime / float / str) in COLUMNS order. Raises IngestionError on
# the first bad row. `tuple[Any, ...]` means a tuple of any length whose items can be any type.
def parse_csv(path: Path) -> list[tuple[Any, ...]]:
    if not path.is_file():
        raise IngestionError(f"CSV file does not exist: {path}")

    records: list[tuple[Any, ...]] = []
    try:
        # `with` is a context manager (Java try-with-resources): the file is closed automatically when the
        # block ends, even on error. utf-8-sig strips a BOM if present; newline="" is required by csv.
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            # csv.reader is an iterator that yields each row as a list of strings.
            reader = csv.reader(source, skipinitialspace=True)
            try:
                # List comprehension: [expr for x in xs] builds a list. next(reader) pulls the header row.
                # Headers are trimmed and lower-cased so matching is forgiving.
                headers = [value.strip().lower() for value in next(reader)]
            # next() raises StopIteration when the iterator is exhausted, i.e. the file is empty.
            except StopIteration as exc:
                raise IngestionError(f"CSV file is empty: {path}") from exc

            # set(headers) removes duplicates, so a length difference means a repeated column name.
            if len(headers) != len(set(headers)):
                raise IngestionError("CSV contains duplicate column names after header normalization.")
            # Set equality ignores order: the CSV must contain exactly the expected columns.
            if set(headers) != set(COLUMNS):
                # Set difference `A - B` = items in A not in B. sorted() returns a new sorted list.
                missing = sorted(set(COLUMNS) - set(headers))
                unexpected = sorted(set(headers) - set(COLUMNS))
                raise IngestionError(
                    f"CSV columns do not match the expected schema. Missing: {missing}; "
                    f"unexpected: {unexpected}."
                )
            # Dict comprehension: {key: value for ...} maps column name -> its index in a CSV row.
            positions = {name: index for index, name in enumerate(headers)}

            # Start at 2 because the header was record 1; this yields the CSV's real line number for messages.
            for record_number, values in enumerate(reader, start=2):
                if len(values) != len(headers):
                    raise IngestionError(
                        f"CSV record {record_number} has {len(values)} fields; expected {len(headers)}."
                    )
                # Another list comprehension: trim whitespace on every field.
                normalized = [value.strip() for value in values]
                row: list[Any] = []
                for column in COLUMNS:
                    # Look up this column's cell by name via positions, so CSV column order does not matter.
                    raw_value = normalized[positions[column]]
                    # Empty string is falsy, so this rejects blank cells.
                    if not raw_value:
                        raise _row_error(record_number, column, "an empty value")
                    if column == "timestamp":
                        try:
                            # `value: Any = ...` declares the type once; strptime parses text via format codes
                            # (%Y year, %m month, %d day, %H:%M:%S time).
                            value: Any = datetime.strptime(raw_value, "%Y-%m-%d %H:%M:%S")
                        except ValueError as exc:
                            raise _row_error(record_number, column, "an invalid timestamp") from exc
                    elif column == "risk_classification":
                        if raw_value not in RISK_LEVELS:
                            raise _row_error(record_number, column, "an unsupported value")
                        value = raw_value
                    else:
                        try:
                            # Every remaining column is numeric. float() raises ValueError on bad text.
                            value = float(raw_value)
                        except ValueError as exc:
                            raise _row_error(record_number, column, "a non-numeric value") from exc
                        # Rejects NaN and +/-infinity, which SQL Server FLOAT columns cannot store.
                        if not math.isfinite(value):
                            raise _row_error(record_number, column, "a non-finite value")
                    row.append(value)
                # tuple(row) freezes the list into an immutable tuple, the shape pyodbc's executemany expects.
                records.append(tuple(row))
    # `except (A, B, C)` catches any of several exception types in one clause.
    except (OSError, UnicodeError, csv.Error) as exc:
        raise IngestionError(f"Could not read CSV file {path}: {exc}") from exc

    if not records:
        raise IngestionError(f"CSV contains a header but no data rows: {path}")
    return records


# Escapes a value for an ODBC connection string: wrap in {braces}, and double any closing brace.
# This lets passwords containing ';' or '=' work. Used by _connection_string.
def _quote_odbc_value(value: str) -> str:
    return "{" + value.replace("}", "}}") + "}"


# Builds the semicolon-separated ODBC connection string (like a JDBC URL). `database` is a parameter
# so we can connect to 'master' first to create the target database. Encrypt=yes with
# TrustServerCertificate=yes is fine for a local dev container with a self-signed cert.
def _connection_string(config: DatabaseConfig, database: str) -> str:
    server = f"tcp:{config.host},{config.port}"
    return (
        f"DRIVER={_quote_odbc_value(config.driver)};"
        f"SERVER={_quote_odbc_value(server)};"
        f"DATABASE={_quote_odbc_value(database)};"
        f"UID={_quote_odbc_value(config.username)};"
        f"PWD={_quote_odbc_value(config.password)};"
        "Encrypt=yes;TrustServerCertificate=yes;"
    )


# Wraps a database name in [brackets], SQL Server's identifier quoting.
def _quoted_database_name(database: str) -> str:
    # load_config validates the identifier before this helper is called.
    return f"[{database}]"


# Creates the target database if it does not exist yet. In: DatabaseConfig. Out: nothing (None).
# Runs before the main load, connected to the always-present 'master' database.
def ensure_database(config: DatabaseConfig) -> None:
    # Lazy import, same reason as in _choose_driver.
    import pyodbc

    # autocommit=True because CREATE DATABASE cannot run inside a transaction.
    connection = pyodbc.connect(_connection_string(config, "master"), autocommit=True, timeout=10)
    try:
        cursor = connection.cursor()
        # DB_ID(?) returns NULL if the database is missing. `?` is a bound parameter (JDBC-style),
        # so the value is not concatenated into SQL. fetchone() returns a row; [0] takes its first column.
        exists = cursor.execute("SELECT DB_ID(?)", config.database).fetchone()[0]
        if exists is None:
            # DDL cannot take bound parameters, hence the f-string; the name was whitelisted in load_config.
            cursor.execute(f"CREATE DATABASE {_quoted_database_name(config.database)}")
            # Logging uses lazy %s formatting: the logger only builds the message if the level is enabled.
            LOGGER.info("Created database %s.", config.database)
    # `finally` always runs (Java finally): the connection is closed on success and on error.
    finally:
        connection.close()


# Generates the CREATE TABLE statement text for a table name. Every column is NOT NULL, typed FLOAT
# unless overridden in SQL_TYPES, and named with its legacy name. Pure function: no DB access.
def _table_ddl(table_name: str) -> str:
    definitions = []
    for column in COLUMNS:
        sql_type = SQL_TYPES.get(column, "FLOAT")
        legacy_name = LEGACY_COLUMN_NAMES[column]
        # Square brackets quote the identifier in T-SQL.
        definitions.append(f"[{legacy_name}] {sql_type} NOT NULL")
    # "sep".join(list) concatenates with a separator. Result: CREATE TABLE t ( [A] FLOAT NOT NULL, ... ).
    return f"CREATE TABLE {table_name} (\n    " + ",\n    ".join(definitions) + "\n)"


# Creates the live and staging tables if they are missing (idempotent). `connection: Any` means any type.
def ensure_tables(connection: Any) -> None:
    cursor = connection.cursor()
    # Iterating over a tuple of two constants: the real table and its staging twin.
    for table_name in (DATABASE_TABLE, STAGING_TABLE):
        # T-SQL: if OBJECT_ID(name, 'U') (user table) is NULL then run the CREATE TABLE inside BEGIN..END.
        cursor.execute(
            f"IF OBJECT_ID(N'{table_name}', N'U') IS NULL\nBEGIN\n    {_table_ddl(table_name)};\nEND"
        )
    # Commit the transaction (autocommit is off for this connection).
    connection.commit()


# The core load step. In: config and validated records. Out: nothing. Uses a staging-table pattern:
# load into staging, verify counts, then swap into the live table, all in ONE transaction, so a
# failure rolls back and leaves the live table untouched. Like a blue/green deploy for rows.
def publish_records(config: DatabaseConfig, records: list[tuple[Any, ...]]) -> None:
    import pyodbc

    ensure_database(config)
    connection = pyodbc.connect(_connection_string(config, config.database), autocommit=False, timeout=10)
    # Generator-expression inside join: builds '[VEH_LAT], [VEH_LON], ...' without an intermediate list.
    column_list = ", ".join(f"[{LEGACY_COLUMN_NAMES[column]}]" for column in COLUMNS)
    # `_` is the conventional name for a throwaway loop variable. Yields one '?' per column.
    placeholders = ", ".join("?" for _ in COLUMNS)
    # Parameterised INSERT: each ? is filled per row by executemany (SQL injection safe for values).
    insert_stage_sql = f"INSERT INTO {STAGING_TABLE} ({column_list}) VALUES ({placeholders})"
    # try/except/finally: any failure rolls back, then `raise` (bare) rethrows the same exception.
    try:
        ensure_tables(connection)
        cursor = connection.cursor()
        # TRUNCATE empties the staging table quickly (leftover rows from an earlier failed run).
        cursor.execute(f"TRUNCATE TABLE {STAGING_TABLE}")
        # pyodbc option: send many rows per round trip instead of one by one.
        cursor.fast_executemany = True
        batch_size = 1000
        # range(start, stop, step) iterates 0, 1000, 2000, ... : the start offset of each batch.
        for offset in range(0, len(records), batch_size):
            # Slice records[a:b] copies items a up to (not including) b; slicing past the end is safe.
            batch = records[offset : offset + batch_size]
            # Insert the whole batch with a single call.
            cursor.executemany(insert_stage_sql, batch)
            # min() guards against over-counting on the final, shorter batch.
            staged_so_far = min(offset + len(batch), len(records))
            LOGGER.info("Staged %s of %s rows.", staged_so_far, len(records))

        # COUNT_BIG(*) counts rows (returns bigint). Sanity check that every row reached staging.
        staged_count = cursor.execute(f"SELECT COUNT_BIG(*) FROM {STAGING_TABLE}").fetchone()[0]
        if staged_count != len(records):
            raise IngestionError(
                f"Staging row count mismatch: expected {len(records)}, found {staged_count}."
            )

        # Replace the live data: delete all current rows, then copy the staged rows with INSERT ... SELECT.
        # Still inside the open transaction, so readers never see a half-loaded table after commit.
        cursor.execute(f"DELETE FROM {DATABASE_TABLE}")
        cursor.execute(
            f"INSERT INTO {DATABASE_TABLE} ({column_list}) SELECT {column_list} FROM {STAGING_TABLE}"
        )
        # Second verification: the live table must now hold exactly the rows we validated.
        published_count = cursor.execute(f"SELECT COUNT_BIG(*) FROM {DATABASE_TABLE}").fetchone()[0]
        if published_count != len(records):
            raise IngestionError(
                f"Published row count mismatch: expected {len(records)}, found {published_count}."
            )

        # Clean staging, then make everything permanent atomically.
        cursor.execute(f"TRUNCATE TABLE {STAGING_TABLE}")
        connection.commit()
        LOGGER.info("Published %s rows to %s.%s.", published_count, config.database, DATABASE_TABLE)
    # `except Exception:` catches any error; the handler undoes the transaction and re-raises.
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


# Defines the command line interface. Out: Namespace whose attributes (args.csv, args.dry_run, ...)
# hold the flag values. argparse also gives you --help for free.
def parse_args() -> argparse.Namespace:
    # ArgumentParser = the flag parser; description shows in --help.
    parser = argparse.ArgumentParser(
        description="Validate and load the raw supply-chain CSV into local Docker SQL Server."
    )
    # add_argument declares one flag. type=Path converts the text to a Path; default is used when omitted.
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="Path to the source CSV.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help="Local environment file containing MSSQL_SA_PASSWORD.",
    )
    # Flags without a default are None when omitted, which load_config() uses to fall through to env values.
    parser.add_argument("--host", help="Local SQL Server host (localhost or 127.0.0.1 only).")
    parser.add_argument("--port", type=int, help="Published local SQL Server port.")
    parser.add_argument("--database", help="Target database name (created if absent).")
    parser.add_argument("--username", help="SQL Server login; defaults to sa.")
    parser.add_argument("--driver", help="Installed Microsoft SQL Server ODBC driver.")
    # action="store_true" makes a boolean switch: absent = False, --dry-run = True.
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and count the CSV without connecting to SQL Server.",
    )
    return parser.parse_args()


# Entry point. Out: process exit code (0 = success, non-zero = failure), the shell/CI convention.
# Pipeline: parse CLI -> validate CSV -> (unless --dry-run) build config -> publish to SQL Server.
def main() -> int:
    # Configure log output once: INFO and above, formatted like 'INFO: message'.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args()
    # Declared up front (None) so the generic error handler below can safely check it.
    config: DatabaseConfig | None = None
    try:
        records = parse_csv(args.csv)
        LOGGER.info("Validated %s records from %s.", len(records), args.csv)
        # Dry run: stop after CSV validation, never touching the database.
        if args.dry_run:
            LOGGER.info("Dry run complete; SQL Server was not contacted.")
            return 0

        config = load_config(args, args.env_file)
        publish_records(config, records)
        return 0
    # Order matters: the specific expected-error handler comes first, the catch-all after it.
    # Returning 1 instead of letting a stack trace escape gives clean CI/shell behaviour.
    except IngestionError as exc:
        LOGGER.error("%s", exc)
        return 1
    except Exception as exc:
        # ODBC error text can echo back the connection string, which would include the
        # password; redact the known password value rather than hiding the whole message.
        message = str(exc)
        if config is not None:
            message = message.replace(config.password, "***")
        LOGGER.error("Ingestion failed: %s", message)
        return 1


# Runs main() only when executed as a script (`python -m ...` or `python file.py`), not when imported.
# sys.exit(code) sets the process exit status to main()'s return value (Java: System.exit).
if __name__ == "__main__":
    sys.exit(main())
