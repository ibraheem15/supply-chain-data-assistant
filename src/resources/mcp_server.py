from fastmcp import FastMCP
from pydantic_core import to_json
from resources.schema_context import build_schema_context
from resources.sop_tool import search_compliance_sop as _search
from resources.sql_tool import MAX_ROWS, query_telemetry_data as _query
from resources.weather_tool import fetch_weather_conditions as _weather

# We used _ as a prefix for imported functions to indicate they are internal.

mcp = FastMCP("supply-chain-tools")

SQL_DESCRIPTION = f"""Run one T-SQL SELECT on historical hourly supply-chain readings.
For past temperature, location, congestion, demand, delay, risk. At most {MAX_ROWS} rows come back (`truncated` says if more existed): prefer COUNT/AVG/GROUP BY over listing rows. On status "error", fix the SQL and retry.
{build_schema_context()}"""


@mcp.tool(description=SQL_DESCRIPTION)
def query_telemetry_data(sql: str) -> str:
    return to_json(_query(sql)).decode()


# to_json returns bytes, not a string so we need to decode it to get a proper string.


@mcp.tool(
    description="SOP: searches company SOPs for procedures, thresholds and rules, such as breaches, rerouting and escalation. Cite the source. If nothing answers the question, say so; never invent."
)
def search_compliance_sop(query: str) -> str:
    return to_json(_search(query)).decode()


@mcp.tool(
    description="Weather: current weather at one latitude/longitude, never historical. If the status is 'unavailable', say so; don't guess."
)
def fetch_weather_conditions(latitude: float, longitude: float) -> str:
    return to_json(_weather(latitude, longitude)).decode()


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
    # We have use the stdio transport to communicate with the client becuase it allows for easy interaction with subprocesses via standard input and output.
    # With stdio, each client gets its own private server process. If three agents used your tools, there would be three separate processes, each with its own SQL connection and its own copy of the embedding model in memory. With HTTP there is one server that all of them share.
