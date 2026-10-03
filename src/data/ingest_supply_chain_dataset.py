from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import os
from dotenv import load_dotenv
import csv


import pyodbc

# Project root folder
ROOT = Path(__file__).resolve().parents[2]

# Constants
DEFAULT_CSV = ROOT / "data" / "raw" / "dynamic_supply_chain_logistics_dataset.csv"
DEFAULT_ENV_FILE = ROOT / "envs" / "legacy_sqlserver.env"
DATABASE_TABLE = "dbo.supply_chain_logistics"
# A set literal {..}: unordered, unique values, O(1) membership tests (`x in RISK_LEVELS`).
RISK_LEVELS = {
    "Low Risk",
    "Moderate Risk",
    "High Risk",
}  # Used set here because it holds only unique values and allows for O(1) membership tests

COLUMNS = (  # we used tuple "()" instead of list "[]" because the column names are fixed and should not be modified, tuple is immutable
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

# LEGACY columns that were used in the previous version of the dataset
# We will add legacy values of keys because we are  predicting that ususally the systems in enterprise are old and have legacy column names.
LEGACY_COLUMN_NAMES = {  # Using dict to map current column names to their legacy counterparts which is useful for backward compatibility
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

SQL_TYPES = {
    "timestamp": "DATETIME2(0)",
    "risk_classification": "NVARCHAR(30)",
}


class IngestionError(Exception):
    """Custom exception for errors during the ingestion of the supply chain dataset."""


# Using frozen=True to make instances of this dataclass immutable. This means
# that once an instance is created, its attributes cannot be modified.
# THis will help ensure the integrity of the database configuration throughout the application's lifecycle.
@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    database: str
    username: str
    password: str
    driver: str


def read_env_file(file_path: str) -> DatabaseConfig:
    """Reads the environment file and returns a DatabaseConfig instance."""

    load_dotenv(file_path)

    if not os.path.exists(file_path):
        raise IngestionError(f"Environment file not found: {file_path}")
    if (
        not os.getenv("DB_HOST")
        or not os.getenv("DB_PORT")
        or not os.getenv("DATABASE_NAME")
        or not os.getenv("DB_USER")
        or not os.getenv("DB_PASSWORD")
        or not os.getenv("DB_DRIVER")
    ):
        raise IngestionError(
            f"One or more required environment variables are missing in the file: {file_path}"
        )

    return DatabaseConfig(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT")),
        database=os.getenv("DATABASE_NAME"),
        username=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        driver=os.getenv("DB_DRIVER"),
    )


def parse_csv(path: Path) -> list[tuple]:
    """Parses the CSV file at the given path and returns a list of tuples representing the rows."""

    if not path.exists():
        raise IngestionError(f"CSV file not found: {path}")

    with path.open(newline="", encoding="utf-8") as csvfile:
        reader = csv.reader(csvfile)
        header = [h.strip() for h in next(reader)]

        # Ensure the header matches the expected columns
        if header != list(COLUMNS):
            raise IngestionError(
                f"CSV header does not match the expected columns: {header}"
            )

        rows = []
        for csv_row in reader:
            values = []
            for column, raw in zip(COLUMNS, csv_row):
                raw = raw.strip()
                if column == "timestamp":
                    values.append(datetime.strptime(raw, "%Y-%m-%d %H:%M:%S"))
                elif column == "risk_classification":
                    if raw not in RISK_LEVELS:
                        raise ValueError("not an allowed risk level")
                    values.append(raw)
                else:
                    values.append(float(raw))
            rows.append(tuple(values))
    return rows


def connection_string(cfg: DatabaseConfig) -> str:
    return (
        f"DRIVER={{{cfg.driver}}};"
        f"SERVER={cfg.host},{cfg.port};"
        f"DATABASE={cfg.database};"
        f"UID={cfg.username};PWD={cfg.password};"
        "TrustServerCertificate=yes;"
    )


def create_table_if_not_exists(cfg: DatabaseConfig) -> None:
    """Creates the supply_chain_logistics table if it does not exist."""
    conn = pyodbc.connect(connection_string(cfg))
    cursor = conn.cursor()
    definitions = []
    # This will loop over all the columns
    for column in COLUMNS:
        # this reads the dict and falls back to "FLOAT" when the column isn't listed
        sql_type = SQL_TYPES.get(column, "FLOAT")  # FLOAT unless overridden
        # get the legacy column name for the current column
        legacy_name = LEGACY_COLUMN_NAMES[column]
        # create the column definition string and append it to the list of definitions
        definitions.append(f"[{legacy_name}] {sql_type} NOT NULL")

    # join the column definitions into a single string for the CREATE TABLE statement
    columns_definition = ", ".join(definitions)

    # Create the table only if it does not exist yet; the name comes from the DATABASE_TABLE constant
    create_table_query = f"""
    IF OBJECT_ID(N'{DATABASE_TABLE}', N'U') IS NULL
    CREATE TABLE {DATABASE_TABLE} (
        {columns_definition}
    )
    """
    cursor.execute(create_table_query)
    conn.commit()
    conn.close()


def insert_data(cfg: DatabaseConfig, rows: list[tuple]) -> None:
    """Inserts the given rows into the supply_chain_logistics table."""
    # Make sure the table exists first, on its own connection, before the transaction below starts
    create_table_if_not_exists(cfg)
    conn = pyodbc.connect(connection_string(cfg))
    column_list = ", ".join(f"[{LEGACY_COLUMN_NAMES[c]}]" for c in COLUMNS)
    placeholders = ", ".join("?" for _ in COLUMNS)
    insert_sql = f"INSERT INTO {DATABASE_TABLE} ({column_list}) VALUES ({placeholders})"
    try:
        cursor = conn.cursor()
        # Delete all existing rows from the table before inserting new data
        cursor.execute(f"DELETE FROM {DATABASE_TABLE}")
        cursor.fast_executemany = True  # send many rows per round trip
        cursor.executemany(insert_sql, rows)
        conn.commit()  # make it all permanent at once
    except Exception:
        conn.rollback()  # undo the DELETE and any inserts
        raise  # let the error continue upward
    finally:
        conn.close()

def main() -> None:
    cfg = read_env_file(DEFAULT_ENV_FILE)
    rows = parse_csv(DEFAULT_CSV)  # assuming you have a function to parse the CSV into a list of tuples
    insert_data(cfg, rows)

if __name__ == "__main__":
    main()