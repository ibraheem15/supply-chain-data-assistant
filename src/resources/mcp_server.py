"""MCP server exposing the three agent tools over stdio.

Run with: make mcp-server. Tool descriptions are part of the contract: they tell the LLM
when to use each tool and what its limits are.
"""

# Lazy type hints so modern annotation syntax is safe (see settings.py).
from __future__ import annotations

import json
import logging
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from resources.schema_context import build_schema_context
from resources.sop_tool import search_compliance_sop as _search_compliance_sop
from resources.sql_tool import MAX_ROWS
from resources.sql_tool import query_telemetry_data as _query_telemetry_data
from resources.weather_tool import fetch_weather_conditions as _fetch_weather_conditions

# Import notes: `sys` is used for stderr, because MCP over stdio uses stdout for protocol messages, so logs
# MUST go to stderr. FastMCP is a helper from the MCP Python SDK (think Spring Boot for MCP tool servers).
# `import X as Y` renames on import: the tool functions get a leading underscore because this file defines
# MCP wrappers with the SAME public names, and the alias avoids a name clash.
# The server object, named for clients. Tools are registered on it with the `@mcp.tool` decorator below.
# Module-level, so it is created once at import (like a singleton bean).
mcp = FastMCP("supply-chain-tools")


# Serialise a tool result dict to a compact JSON string. Input: the dict from a tool function.
# Output: JSON text. MCP tools return text to the LLM, and tokens cost money and context space.
def compact(result: dict[str, Any]) -> str:
    """Compact JSON text. The default pretty-printed output wastes tokens on every tool result."""
    # separators=(",", ":") removes the spaces after commas and colons (smaller output).
    # default=str is a fallback that stringifies values JSON cannot encode (e.g. a stray date) instead of
    # raising. ensure_ascii=False keeps non-ASCII characters readable instead of \\uXXXX escapes.
    return json.dumps(result, separators=(",", ":"), default=str, ensure_ascii=False)


# Constant: the text the LLM reads to decide when and how to use the SQL tool. It is part of the tool
# contract. The triple-quoted f-string spans lines and inlines MAX_ROWS and the schema text at import time.
# Built with an f-string, which cannot be a docstring, so it is passed as `description=`.
# Tool descriptions are sent with every request: keep them short.
QUERY_TELEMETRY_DESCRIPTION = f"""Run one T-SQL SELECT on historical hourly supply-chain readings.
For past temperature, location, congestion, demand, delay, risk. At most {MAX_ROWS} rows come back
(`truncated` says if more existed): prefer COUNT/AVG/GROUP BY over listing rows. On status "error",
fix the SQL and retry.

{build_schema_context()}"""


# MCP tool 1: query_telemetry_data. Input: a T-SQL SELECT string. Output: compact JSON text.
# `@mcp.tool(...)` is a decorator: a function that wraps/registers the function below it, much like a Java
# annotation (@GetMapping) registering an endpoint. The type hints on the parameters become the tool's
# input schema that the LLM sees. The body just delegates to the real implementation in sql_tool.py.
@mcp.tool(description=QUERY_TELEMETRY_DESCRIPTION)
def query_telemetry_data(sql: str) -> str:
    return compact(_query_telemetry_data(sql))


# MCP tool 2: fetch_weather_conditions. Input: latitude and longitude. Output: compact JSON text.
# Here description= is a plain string; two adjacent literals are concatenated into one.
# MCP tool 3: search_compliance_sop. Input: query text and optional top_k (default 3). Output: compact
# JSON text. The description tells the LLM to cite `source` and `headings` and never to invent answers.
@mcp.tool(
    description="CURRENT weather at one latitude/longitude (live only, never historical). "
    "weather_disruption_level is a derived heuristic. If unavailable, say so; never guess."
)
def fetch_weather_conditions(latitude: float, longitude: float) -> str:
    return compact(_fetch_weather_conditions(latitude, longitude))


@mcp.tool(
    description="Search company SOPs for procedures, thresholds and rules (breaches, rerouting, "
    "escalation). Cite `source` and `headings`. If nothing answers, say so; never invent."
)
def search_compliance_sop(query: str, top_k: int = 3) -> str:
    return compact(_search_compliance_sop(query, top_k))


# Entry point (like `public static void main`). Configures logging to stderr, then starts the server on
# stdio, so an MCP client can launch this process and talk to it over stdin/stdout.
def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s: %(message)s")
    mcp.run(transport="stdio")


# Runs main() only when this file is executed directly (e.g. `python -m resources.mcp_server`), not when
# it is imported by something else. The Python idiom for a Java-style main entry.
if __name__ == "__main__":
    main()
