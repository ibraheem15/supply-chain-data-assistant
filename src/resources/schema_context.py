"""Description of the agent-facing view, handed to the LLM so it can write correct T-SQL."""

from __future__ import annotations

from data.ingest_supply_chain_dataset import COLUMNS, LEGACY_COLUMN_NAMES

VIEW_NAME = "ai.VW_SUPPLY_CHAIN_READINGS"

# Units and meanings come from data/README.md. Keys must match COLUMNS exactly.
COLUMN_DESCRIPTIONS = {
    "timestamp": "Date and time of the reading (hourly, 2021-01-01 to 2024-08-29)",
    "vehicle_gps_latitude": "Vehicle latitude (about 30 to 50)",
    "vehicle_gps_longitude": "Vehicle longitude (about -120 to -70)",
    "fuel_consumption_rate": "Fuel consumption, litres per hour",
    "eta_variation_hours": "Estimated minus actual arrival time, hours",
    "traffic_congestion_level": "Route traffic congestion, 0-10",
    "warehouse_inventory_level": "Warehouse inventory, units",
    "loading_unloading_time": "Loading/unloading duration, hours",
    "handling_equipment_availability": "Handling equipment availability (documented 0/1; data has fractions)",
    "order_fulfillment_status": "Order fulfilled on time (documented as 0/1, data holds fractions)",
    "weather_condition_severity": "Weather severity, 0-1",
    "port_congestion_level": "Port congestion, 0-10",
    "shipping_costs": "Shipping cost, USD",
    "supplier_reliability_score": "Supplier reliability, 0-1",
    "lead_time_days": "Supplier delivery lead time, days",
    "historical_demand": "Historical demand, units",
    "iot_temperature": "IoT sensor temperature, degrees Celsius",
    "cargo_condition_status": "Cargo condition (documented as 0 poor / 1 good, data may hold fractions)",
    "route_risk_level": "Route risk, 0-10",
    "customs_clearance_time": "Customs clearance time",
    "driver_behavior_score": "Driver behavior, 0-1",
    "fatigue_monitoring_score": "Driver fatigue, 0-1",
    "disruption_likelihood_score": "Disruption likelihood, 0-1",
    "delay_probability": "Shipment delay probability, 0-1",
    "risk_classification": "Text label: 'Low Risk', 'Moderate Risk' or 'High Risk'",
    "delivery_time_deviation": "Deviation from expected delivery time, hours",
}

assert set(COLUMN_DESCRIPTIONS) == set(COLUMNS), "COLUMN_DESCRIPTIONS is out of sync with COLUMNS"


def build_schema_context() -> str:
    lines = [
        f"Table (a read-only view): {VIEW_NAME}. It is the only object you may query.",
        "Dialect: Microsoft SQL Server (T-SQL). Use TOP n, never LIMIT. Write exactly one SELECT.",
        "The view has no shipment id, cargo type or vehicle id. Rows are hourly readings.",
        "",
        "Columns (business name: meaning). Reference them in square brackets, e.g. [timestamp]:",
    ]
    lines += [f"- {column}: {COLUMN_DESCRIPTIONS[column]}" for column in COLUMNS]
    lines += [
        "",
        "SOP documents use legacy names. Translate them to the business names above:",
    ]
    lines += [f"- {LEGACY_COLUMN_NAMES[column]} = {column}" for column in COLUMNS]
    return "\n".join(lines)
