import asyncio
from mcp import Client
from mcp.client.stdio import StdioServerParameters

params = StdioServerParameters(
    command="uv",
    args=["run", "python", "-m", "resources.mcp_server"],
    env={"PYTHONPATH": "src"},
)


async def main():
    async with Client(params) as client: # THis runs commands in subprocess
        # tools = await client.list_tools()
        # print(tools)
        
        sql_tool = await client.call_tool(
            "query_telemetry_data",
            {"sql": "SELECT TOP 3 * FROM ai_2.VW_SUPPLY_CHAIN_READINGS"},
        )
        print(sql_tool)

        sop_tool = await client.call_tool(
            "search_compliance_sop",
            {"query": "What should I do when the cargo temperature is too high?"},
        )
        print(sop_tool)

        weather_tool = await client.call_tool(
            "fetch_weather_conditions", {"latitude": 49.8, "longitude": -113.1}
        )
        print(weather_tool)


if __name__ == "__main__":
    asyncio.run(main())
