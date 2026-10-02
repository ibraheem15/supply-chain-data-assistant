"""Description of the agent-facing view, handed to the LLM so it can write correct T-SQL."""

# Lazy type hints (see settings.py): allows modern annotation syntax everywhere in this file.
from __future__ import annotations

# Reuse the column list from the ingestion script (single source of truth, like importing a shared
# constants class in Java). COLUMNS is the list of business names; LEGACY_COLUMN_NAMES maps each business
# name to the older name that SOP documents use.
from data.ingest_supply_chain_dataset import COLUMNS, LEGACY_COLUMN_NAMES

# The only database object the agent may query: view `VW_SUPPLY_CHAIN_READINGS` in schema `ai`.
VIEW_NAME = "ai.VW_SUPPLY_CHAIN_READINGS"

# COLUMN_DESCRIPTIONS: a dict literal (like a Java Map.of) from column name to a short human-readable
# unit/meaning. These strings are pasted into the LLM prompt so it can write correct SQL.
# Units and meanings come from data/README.md. Keys must match COLUMNS exactly.
COLUMN_DESCRIPTIONS = {
    "timestamp": "hourly reading time",
    "vehicle_gps_latitude": "latitude, ~30-50",
    "vehicle_gps_longitude": "longitude, ~-120 to -70",
    "fuel_consumption_rate": "litres/hour",
    "eta_variation_hours": "estimated minus actual arrival, hours",
    "traffic_congestion_level": "0-10",
    "warehouse_inventory_level": "units",
    "loading_unloading_time": "hours",
    "handling_equipment_availability": "documented 0/1, data has fractions",
    "order_fulfillment_status": "on time, documented 0/1, data has fractions",
    "weather_condition_severity": "0-1",
    "port_congestion_level": "0-10",
    "shipping_costs": "USD",
    "supplier_reliability_score": "0-1",
    "lead_time_days": "days",
    "historical_demand": "units",
    "iot_temperature": "sensor temperature, Celsius",
    "cargo_condition_status": "documented 0 poor/1 good, may be fractional",
    "route_risk_level": "0-10",
    "customs_clearance_time": "clearance time",
    "driver_behavior_score": "0-1",
    "fatigue_monitoring_score": "0-1",
    "disruption_likelihood_score": "0-1",
    "delay_probability": "0-1",
    "risk_classification": "'Low Risk' | 'Moderate Risk' | 'High Risk'",
    "delivery_time_deviation": "hours",
}

# Import-time sanity check, like a startup assertion. set(dict) builds a set of its keys, and sets compare
# ignoring order. If a column is added or removed in one place but not the other, importing this module
# fails immediately with the message after the comma, instead of misleading the LLM later.
assert set(COLUMN_DESCRIPTIONS) == set(COLUMNS), "COLUMN_DESCRIPTIONS is out of sync with COLUMNS"


# Build the plain-text schema description sent to the LLM (it is embedded in the SQL tool description
# in mcp_server.py). Input: none. Output: one multi-line string.
def build_schema_context() -> str:
    """Kept short on purpose: this text is sent to the LLM with every request."""
    # Header lines: dialect rules (T-SQL uses TOP, not LIMIT), data range, and the legacy-name warning.
    lines = [
        f"View: {VIEW_NAME} (the only queryable object). T-SQL: use TOP n, never LIMIT; one SELECT.",
        "No shipment id, vehicle id or cargo type. Rows are hourly, 2021-01-01 to 2024-08-29.",
        "Query ONLY the business column names below. Names in [SOP: ...] appear in SOP documents and",
        "are NOT columns (using one is an error). Columns:",
    ]
    # `+=` on a list extends it. The right side is a list comprehension: one f-string per column,
    # like a Java stream map(...).toList(). Each line looks like `name [SOP: legacy_name]: description`.
    lines += [f"{c} [SOP: {LEGACY_COLUMN_NAMES[c]}]: {COLUMN_DESCRIPTIONS[c]}" for c in COLUMNS]
    # str.join glues the list into one string, with a newline between items.
    return "\n".join(lines)
