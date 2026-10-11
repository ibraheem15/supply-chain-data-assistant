from langchain_core.messages import SystemMessage
from langgraph.graph import StateGraph, MessagesState, START
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import MemorySaver
from agent.prompts import build_system_prompt


def build_graph(llm, tools):
    """
    build_graph knows nothing about Gemini, Groq or MCP. It only wires the loop for whatever model and tools it receives, and returns the constructed graph.
    """
    llm_with_tools = llm.bind_tools(tools)

    async def reasoner(state: MessagesState):
        """
        state is the current state of the conversation, containing all previous messages.
        MessageState containing the conversation history up to this point in "messages".

        """
        #TODO
        # prompt caching 

        messages = [SystemMessage(content=build_system_prompt())] + state["messages"]
        
        # We add the system prompt here every time instead of storing it in the state, otherwise the memory would save a new copy of it on every turn.
        
        reply = await llm_with_tools.ainvoke(messages)
        
        return {"messages": [reply]}

    tool_node = ToolNode(tools)
    builder = StateGraph(MessagesState)
    builder.add_node("reasoner", reasoner)
    builder.add_node("tools", tool_node)
    builder.add_edge(START, "reasoner")
    builder.add_conditional_edges("reasoner", tools_condition)
    # tools_condition is not the tools node. It's a router function that runs after the
    # reasoner and checks the LLM's reply: if it asks for a tool it returns "tools",
    # otherwise END. It never calls a tool itself; the "tools" node (ToolNode) does that.

    builder.add_edge("tools", "reasoner") # always runs: after tools, go to reasoner
    
    # We didn't add end node because tools_condition is the end-or-continue switch. END is a built-in marker meaning "the graph is finished", and it's what tools_condition returns

    return builder.compile(checkpointer=MemorySaver())
    # MemorySaver is what lets the agent remember the earlier turns of a conversation. Without it, every question would start from an empty state.
    # It's a checkpointer: it saves the state after every step and loads it back at the start of the next call, under an id you choose (thread_id)
    # You pass the id when you call the graph. So thread_id is the conversation id: same id means the same conversation, and a new id starts fresh

