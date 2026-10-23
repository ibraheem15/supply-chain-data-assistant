import argparse
import asyncio
import uuid
from agent.llm import build_llm, describe_llm
from agent.graph import build_graph
from agent.prompts import DEFAULT_ROLE, role_titles
from agent.tools import open_adapter
from agent.turn import run_turn


def parse_args() -> argparse.Namespace:
    roles = role_titles()
    parser = argparse.ArgumentParser(description="Chat with the supply-chain agent.")
    parser.add_argument(
        "--role",
        choices=list(roles),
        default=DEFAULT_ROLE,
        help="which department assistant to talk to: "
        + "; ".join(f"{role_id} = {title}" for role_id, title in roles.items()),
    )
    return parser.parse_args()


async def main():
    role = parse_args().role
    provider, model = describe_llm()
    print(f"Assistant: {role_titles()[role]} ({role}). Type exit to leave.")
    async with open_adapter() as adapter:
        # The async with is the same as in the smoke test. The chat loop lives inside it, so the MCP server stays alive for the whole conversation. When the block ends, the server stops

        tools = await adapter.list_tools()
        graph = build_graph(build_llm(), tools)
        session_id = str(uuid.uuid4())  # one conversation per run; also the audit log's SessionId

        while True:
            question = input("You: ").strip()
            if question.lower() in {"exit", "quit"}:
                break
            if not question:
                continue
            # run_turn runs the question, writes the audit rows and yields one event per step.
            async for event in run_turn(graph, question, session_id, provider, model, role):
                if event.kind == "tool_call":
                    print(f"  [tool] {event.tool_name} {event.text}")
                elif event.kind == "tool_result":
                    print(f"  [result] {event.tool_name}: {event.status}")
                elif event.kind == "answer":
                    print("Agent:", event.text)
                elif event.kind == "error":
                    print("Error:", event.text)
                elif event.kind == "audit_warning":
                    print("Warning:", event.text)


if __name__ == "__main__":
    asyncio.run(main())
