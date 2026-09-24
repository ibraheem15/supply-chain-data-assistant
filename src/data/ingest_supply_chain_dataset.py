"""Load the raw supply-chain CSV into the local SQL Server database."""

from __future__ import annotations

import argparse
import csv
import logging
import math
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV = ROOT / "data" / "raw" / "dynamic_supply_chain_logistics_dataset.csv"
DEFAULT_ENV_FILE = ROOT / ".env.legacy_sqlserver"
DATABASE_TABLE = "dbo.supply_chain_logistics"
STAGING_TABLE = "dbo.supply_chain_logistics_staging"
RISK_LEVELS = {"Low Risk", "Moderate Risk", "High Risk"}

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

LOGGER = logging.getLogger("supply_chain_ingest")


class IngestionError(Exception):
    """An expected, actionable ingestion failure."""


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    database: str
    username: str
    password: str
    driver: str


def read_env_file(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE entries without logging their values."""
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", maxsplit=1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if not key:
            raise IngestionError(f"Invalid environment entry on line {line_number} of {path}.")
        values[key] = value
    return values


def _setting(name: str, file_values: dict[str, str], default: str | None = None) -> str | None:
    return os.environ.get(name, file_values.get(name, default))


def _choose_driver(requested: str | None) -> str:
    try:
        import pyodbc
    except ImportError as exc:
        raise IngestionError(
            "Could not import pyodbc. If the pyodbc package itself is missing, run `uv sync`. "
            "If pyodbc is installed but this still fails (e.g. "
            '"libodbc.so.2: cannot open shared object file"), the host is missing the '
            "unixODBC driver manager system library — install it via your OS package manager "
            f"(underlying error: {exc})."
        ) from exc

    available = pyodbc.drivers()
    if requested:
        if requested not in available:
            raise IngestionError(
                f"ODBC driver {requested!r} is not installed. Available drivers: "
                f"{', '.join(available) or '(none)'}."
            )
        return requested

    for candidate in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
        if candidate in available:
            return candidate
    raise IngestionError(
        "Microsoft ODBC Driver 18 or 17 for SQL Server is required. "
        "Install a Microsoft SQL Server ODBC driver and try again."
    )


def load_config(args: argparse.Namespace, env_file: Path) -> DatabaseConfig:
    file_values = read_env_file(env_file)
    host = args.host or _setting("MSSQL_HOST", file_values, "127.0.0.1")
    port_value = args.port if args.port is not None else _setting("MSSQL_PORT", file_values, "1433")
    port_text = str(port_value)
    database = args.database or _setting("MSSQL_DATABASE", file_values, "SupplyChainDb")
    username = args.username or _setting("MSSQL_USER", file_values, "sa")
    password = _setting("MSSQL_SA_PASSWORD", file_values)
    requested_driver = args.driver or _setting("MSSQL_ODBC_DRIVER", file_values)

    if host is None or host.lower() not in {"localhost", "127.0.0.1"}:
        raise IngestionError(
            "This Phase 1 loader only connects to the local Docker SQL Server. "
            "Set MSSQL_HOST to localhost or 127.0.0.1."
        )
    try:
        port = int(port_text)
    except (TypeError, ValueError) as exc:
        raise IngestionError("MSSQL_PORT must be an integer from 1 to 65535.") from exc
    if not 1 <= port <= 65535:
        raise IngestionError("MSSQL_PORT must be an integer from 1 to 65535.")
    if not database or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", database):
        raise IngestionError("MSSQL_DATABASE must be a valid SQL Server database identifier.")
    if not username:
        raise IngestionError("MSSQL_USER must not be empty.")
    if not password:
        raise IngestionError(
            f"MSSQL_SA_PASSWORD is missing. Set it in {env_file} or the process environment."
        )

    return DatabaseConfig(
        host=host,
        port=port,
        database=database,
        username=username,
        password=password,
        driver=_choose_driver(requested_driver),
    )


def _row_error(record_number: int, column: str, reason: str) -> IngestionError:
    return IngestionError(f"CSV record {record_number} has {reason} in column {column}.")


def parse_csv(path: Path) -> list[tuple[Any, ...]]:
    if not path.is_file():
        raise IngestionError(f"CSV file does not exist: {path}")

    records: list[tuple[Any, ...]] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.reader(source, skipinitialspace=True)
            try:
                headers = [value.strip().lower() for value in next(reader)]
            except StopIteration as exc:
                raise IngestionError(f"CSV file is empty: {path}") from exc

            if len(headers) != len(set(headers)):
                raise IngestionError("CSV contains duplicate column names after header normalization.")
            if set(headers) != set(COLUMNS):
                missing = sorted(set(COLUMNS) - set(headers))
                unexpected = sorted(set(headers) - set(COLUMNS))
                raise IngestionError(
                    f"CSV columns do not match the expected schema. Missing: {missing}; "
                    f"unexpected: {unexpected}."
                )
            positions = {name: index for index, name in enumerate(headers)}

            for record_number, values in enumerate(reader, start=2):
                if len(values) != len(headers):
                    raise IngestionError(
                        f"CSV record {record_number} has {len(values)} fields; expected {len(headers)}."
                    )
                normalized = [value.strip() for value in values]
                row: list[Any] = []
                for column in COLUMNS:
                    raw_value = normalized[positions[column]]
                    if not raw_value:
                        raise _row_error(record_number, column, "an empty value")
                    if column == "timestamp":
                        try:
                            value: Any = datetime.strptime(raw_value, "%Y-%m-%d %H:%M:%S")
                        except ValueError as exc:
                            raise _row_error(record_number, column, "an invalid timestamp") from exc
                    elif column == "risk_classification":
                        if raw_value not in RISK_LEVELS:
                            raise _row_error(record_number, column, "an unsupported value")
                        value = raw_value
                    else:
                        try:
                            value = float(raw_value)
                        except ValueError as exc:
                            raise _row_error(record_number, column, "a non-numeric value") from exc
                        if not math.isfinite(value):
                            raise _row_error(record_number, column, "a non-finite value")
                    row.append(value)
                records.append(tuple(row))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise IngestionError(f"Could not read CSV file {path}: {exc}") from exc

    if not records:
        raise IngestionError(f"CSV contains a header but no data rows: {path}")
    return records


def _quote_odbc_value(value: str) -> str:
    return "{" + value.replace("}", "}}") + "}"


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


def _quoted_database_name(database: str) -> str:
    # load_config validates the identifier before this helper is called.
    return f"[{database}]"


def ensure_database(config: DatabaseConfig) -> None:
    import pyodbc

    connection = pyodbc.connect(_connection_string(config, "master"), autocommit=True, timeout=10)
    try:
        cursor = connection.cursor()
        exists = cursor.execute("SELECT DB_ID(?)", config.database).fetchone()[0]
        if exists is None:
            cursor.execute(f"CREATE DATABASE {_quoted_database_name(config.database)}")
            LOGGER.info("Created database %s.", config.database)
    finally:
        connection.close()


def _table_ddl(table_name: str) -> str:
    definitions = []
    for column in COLUMNS:
        sql_type = SQL_TYPES.get(column, "FLOAT")
        legacy_name = LEGACY_COLUMN_NAMES[column]
        definitions.append(f"[{legacy_name}] {sql_type} NOT NULL")
    return f"CREATE TABLE {table_name} (\n    " + ",\n    ".join(definitions) + "\n)"


def ensure_tables(connection: Any) -> None:
    cursor = connection.cursor()
    for table_name in (DATABASE_TABLE, STAGING_TABLE):
        cursor.execute(
            f"IF OBJECT_ID(N'{table_name}', N'U') IS NULL\nBEGIN\n    {_table_ddl(table_name)};\nEND"
        )
    connection.commit()


def publish_records(config: DatabaseConfig, records: list[tuple[Any, ...]]) -> None:
    import pyodbc

    ensure_database(config)
    connection = pyodbc.connect(_connection_string(config, config.database), autocommit=False, timeout=10)
    column_list = ", ".join(f"[{LEGACY_COLUMN_NAMES[column]}]" for column in COLUMNS)
    placeholders = ", ".join("?" for _ in COLUMNS)
    insert_stage_sql = f"INSERT INTO {STAGING_TABLE} ({column_list}) VALUES ({placeholders})"
    try:
        ensure_tables(connection)
        cursor = connection.cursor()
        cursor.execute(f"TRUNCATE TABLE {STAGING_TABLE}")
        cursor.fast_executemany = True
        batch_size = 1000
        for offset in range(0, len(records), batch_size):
            batch = records[offset : offset + batch_size]
            cursor.executemany(insert_stage_sql, batch)
            staged_so_far = min(offset + len(batch), len(records))
            LOGGER.info("Staged %s of %s rows.", staged_so_far, len(records))

        staged_count = cursor.execute(f"SELECT COUNT_BIG(*) FROM {STAGING_TABLE}").fetchone()[0]
        if staged_count != len(records):
            raise IngestionError(
                f"Staging row count mismatch: expected {len(records)}, found {staged_count}."
            )

        cursor.execute(f"DELETE FROM {DATABASE_TABLE}")
        cursor.execute(
            f"INSERT INTO {DATABASE_TABLE} ({column_list}) SELECT {column_list} FROM {STAGING_TABLE}"
        )
        published_count = cursor.execute(f"SELECT COUNT_BIG(*) FROM {DATABASE_TABLE}").fetchone()[0]
        if published_count != len(records):
            raise IngestionError(
                f"Published row count mismatch: expected {len(records)}, found {published_count}."
            )

        cursor.execute(f"TRUNCATE TABLE {STAGING_TABLE}")
        connection.commit()
        LOGGER.info("Published %s rows to %s.%s.", published_count, config.database, DATABASE_TABLE)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate and load the raw supply-chain CSV into local Docker SQL Server."
    )
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="Path to the source CSV.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help="Local environment file containing MSSQL_SA_PASSWORD.",
    )
    parser.add_argument("--host", help="Local SQL Server host (localhost or 127.0.0.1 only).")
    parser.add_argument("--port", type=int, help="Published local SQL Server port.")
    parser.add_argument("--database", help="Target database name (created if absent).")
    parser.add_argument("--username", help="SQL Server login; defaults to sa.")
    parser.add_argument("--driver", help="Installed Microsoft SQL Server ODBC driver.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and count the CSV without connecting to SQL Server.",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args()
    config: DatabaseConfig | None = None
    try:
        records = parse_csv(args.csv)
        LOGGER.info("Validated %s records from %s.", len(records), args.csv)
        if args.dry_run:
            LOGGER.info("Dry run complete; SQL Server was not contacted.")
            return 0

        config = load_config(args, args.env_file)
        publish_records(config, records)
        return 0
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


if __name__ == "__main__":
    sys.exit(main())
