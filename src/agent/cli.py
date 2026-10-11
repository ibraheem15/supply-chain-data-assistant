import asyncio
import uuid
from agent.llm import build_llm
from agent.graph import build_graph
from agent.tools import open_adapter

async def main():
    async with open_adapter() as adapter:
    # The async with is the same as in the smoke test. The chat loop will live inside it, so the MCP server stays alive for the whole conversation. When the block ends, the server stops
        
        tools = await adapter.list_tools()
        graph = build_graph(build_llm(), tools)
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        
        while True:
            question = input("You: ").strip()
            if question.lower() in {"exit", "quit"}:
                break
            if not question:
                continue
            result = await graph.ainvoke({"messages": [("user", question)]}, config=config)
            print("Agent:", result["messages"][-1].text)

if __name__ == "__main__":
    asyncio.run(main())
