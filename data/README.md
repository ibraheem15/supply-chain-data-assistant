# Supply Chain Logistics Dataset

## Source

Dataset page: [Logistics and Supply Chain Dataset on Kaggle](https://www.kaggle.com/datasets/datasetengineer/logistics-and-supply-chain-dataset)

## Dataset Overview

The source describes this as anonymized logistics and supply chain data collected from a logistics network in Southern California. It covers transportation, warehouse operations, route planning, and monitoring. The described data sources include GPS tracking, IoT sensors, warehouse management systems, and external data providers.

The source describes hourly records from January 2021 to January 2024 and mentions trucks, drones, and rail. The CSV itself contains GPS coordinates and operational features, but no transportation-mode field. See [Observed File Details](#observed-file-details) for checks against the actual file.

## File

| Path | Description |
| --- | --- |
| `data/raw/dynamic_supply_chain_logistics_dataset.csv` | Raw dataset as downloaded from the source |

The CSV contains 32,065 records and 26 columns. Column names in the source file include inconsistent leading or trailing spaces; data-loading code should normalize headers before referring to them.

`data/raw/*.csv` is gitignored (raw data isn't committed to version control). Download it from the [Kaggle source](#source) above and place it at `data/raw/dynamic_supply_chain_logistics_dataset.csv` before running the ingestion script.

## Loading into SQL Server

Phase 1 loads this raw file into the local Docker SQL Server with [`src/data/ingest_supply_chain_dataset.py`](../src/data/ingest_supply_chain_dataset.py). See [`docs/phase1-plan.md`](../docs/phase1-plan.md) for prerequisites and commands. The loader validates the source schema, preserves source rows, and publishes the load transactionally.

## Features

| Column | Meaning in the supplied dataset description |
| --- | --- |
| `timestamp` | Date and time the record was recorded, described as hourly resolution |
| `vehicle_gps_latitude` | Vehicle latitude coordinate |
| `vehicle_gps_longitude` | Vehicle longitude coordinate |
| `fuel_consumption_rate` | Fuel consumption rate in liters per hour |
| `eta_variation_hours` | Difference between estimated and actual arrival time, in hours |
| `traffic_congestion_level` | Route traffic congestion, on a 0–10 scale |
| `warehouse_inventory_level` | Warehouse inventory, in units |
| `loading_unloading_time` | Loading or unloading duration, in hours |
| `handling_equipment_availability` | Handling equipment availability (described as 0 = unavailable, 1 = available) |
| `order_fulfillment_status` | Whether an order was fulfilled on time (described as 0 = no, 1 = yes) |
| `weather_condition_severity` | Severity of weather conditions, on a 0–1 scale |
| `port_congestion_level` | Port congestion, on a 0–10 scale |
| `shipping_costs` | Shipping cost in USD |
| `supplier_reliability_score` | Supplier reliability score, on a 0–1 scale |
| `lead_time_days` | Supplier delivery lead time, in days |
| `historical_demand` | Historical demand, in units |
| `iot_temperature` | IoT sensor temperature in degrees Celsius |
| `cargo_condition_status` | Cargo condition (described as 0 = poor, 1 = good) |
| `route_risk_level` | Route risk, on a 0–10 scale |
| `customs_clearance_time` | Time required for customs clearance |
| `driver_behavior_score` | Driver behavior score, on a 0–1 scale |
| `fatigue_monitoring_score` | Driver fatigue score, on a 0–1 scale |

## Target Variables

The source identifies these columns as prediction targets:

| Column | Meaning in the supplied dataset description |
| --- | --- |
| `disruption_likelihood_score` | Disruption likelihood score, on a 0–1 scale |
| `delay_probability` | Shipment delay probability, on a 0–1 scale |
| `risk_classification` | Categorical risk label: `Low Risk`, `Moderate Risk`, or `High Risk` |
| `delivery_time_deviation` | Deviation from expected delivery time, in hours |

## Potential Use Cases

- Predictive modeling for disruption detection and risk assessment
- Analysis of factors associated with delivery delays, including traffic and weather
- Route and schedule analysis
- Vehicle maintenance analysis using fuel and sensor readings
- Warehouse inventory and fulfillment analysis

These are possible analytical uses of the dataset, not validated capabilities or conclusions.

## Observed File Details

Basic inspection of `data/raw/dynamic_supply_chain_logistics_dataset.csv` found:

- 32,065 data rows and 26 columns
- Timestamps from `2021-01-01 00:00:00` through `2024-08-29 00:00:00`
- Latitude values from approximately 30 to 50 and longitude values from approximately -120 to -70
- No column describing transportation mode

The observed date range extends beyond the January 2024 end date in the supplied description. The observed coordinate range is broader than Southern California alone. The description also defines several status fields as binary, while the CSV contains fractional values in at least `handling_equipment_availability` and `order_fulfillment_status`. Validate field ranges and meanings before treating these columns as binary labels or using them for modeling.
