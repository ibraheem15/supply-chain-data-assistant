from fastmcp.client.transports import StdioTransport
import asyncio
from langchain.mcp import MCPAdapter

from data.domain import load_domain

# The MCP server is a separate process. A stdio child process only gets the variables we list here
# (plus a few basics like PATH), so it does NOT inherit DOMAIN from our shell. Without DOMAIN it would
# fall back to "logistics" and the agent would get logistics tools next to a retail prompt.
transport = StdioTransport(
    command="uv",
    args=["run", "python", "-m", "resources.mcp_server"],
    env={"PYTHONPATH": "src", "DOMAIN": load_domain().name},
)

def open_adapter() -> MCPAdapter:
    return MCPAdapter(transport)


async def main():
    async with MCPAdapter(transport) as adapter: # Connect to the MCP server using the specified transport.
        tools = await adapter.list_tools()
        by_name = {t.name: t for t in tools}
        print(list(by_name))
        print(
            await by_name["fetch_weather_conditions"].ainvoke(
                {"latitude": 49.8, "longitude": -113.1}
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
