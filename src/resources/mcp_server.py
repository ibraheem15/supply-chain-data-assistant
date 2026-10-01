"""MCP server exposing the three agent tools over stdio.

Run with: make mcp-server. Tool descriptions are part of the contract: they tell the LLM
when to use each tool and what its limits are.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

from resources.schema_context import build_schema_context
from resources.sop_tool import search_compliance_sop as _search_compliance_sop
from resources.sql_tool import MAX_ROWS
from resources.sql_tool import query_telemetry_data as _query_telemetry_data
from resources.weather_tool import fetch_weather_conditions as _fetch_weather_conditions

mcp = MCPServer("supply-chain-tools")


# Built with an f-string, which cannot be a docstring, so it is passed as `description=`.
QUERY_TELEMETRY_DESCRIPTION = f"""Query hourly supply-chain readings (2021 to 2024) with one T-SQL SELECT.

Use for questions about past temperature, location, congestion, demand, delays or risk.
At most {MAX_ROWS} rows are returned (`truncated` tells you if more existed), so prefer
aggregates (COUNT, AVG, GROUP BY) over listing rows. If status is "error", read the message,
fix the SQL and call again.

{build_schema_context()}
"""


@mcp.tool(description=QUERY_TELEMETRY_DESCRIPTION)
def query_telemetry_data(sql: str) -> dict[str, Any]:
    return _query_telemetry_data(sql)


@mcp.tool()
def fetch_weather_conditions(latitude: float, longitude: float) -> dict[str, Any]:
    """Get CURRENT weather (temperature, wind, precipitation) at one latitude/longitude.

    Use for live conditions only. It cannot answer what the weather was at a past reading's
    timestamp. `weather_disruption_level` (low/moderate/high) is a derived heuristic estimate.
    If status is "unavailable", tell the user the weather data is missing; never guess it.
    """
    return _fetch_weather_conditions(latitude, longitude)


@mcp.tool()
def search_compliance_sop(query: str, top_k: int = 4) -> dict[str, Any]:
    """Search the company's cold-chain and logistics SOPs for procedures, thresholds and rules.

    Use for what the procedure is (breach thresholds, mitigation steps, rerouting, escalation).
    Cite `source` and `headings` from the results in your answer. If no result answers the
    question, say no procedure was found; never invent one.
    """
    return _search_compliance_sop(query, top_k)


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s: %(message)s")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
