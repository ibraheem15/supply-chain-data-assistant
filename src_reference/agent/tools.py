"""Open one persistent MCP session to the Phase 3c server and expose its tools to LangChain."""

from __future__ import annotations

import os
import sys

# AsyncGenerator is only a type hint ("an async function that yields values").
# asynccontextmanager turns a generator function into something usable with `async with`.
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

# BaseTool is LangChain's tool abstraction: a name, a description the LLM reads, an input schema,
# and a function to run. The Tools node executes these when the LLM asks for one.
# MCP (Model Context Protocol) is a standard for exposing tools from a separate server process.
# MultiServerMCPClient launches/talks to such servers; load_mcp_tools converts MCP tool
# definitions into LangChain BaseTool objects.
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from resources.settings import ROOT

# Logical name for our MCP server inside the client (a key, like a service name in docker-compose).
SERVER_NAME = "supply_chain"

# Hugging Face's cache folder for the local embedding model (data/ingest_sop_pinecone.py defines the model
# name, "sentence-transformers/all-MiniLM-L6-v2"; a test checks the two agree). It is spelled out here
# on purpose: importing the ingestion module would load `transformers` (huge) into the UI process, which
# slows start-up and makes Streamlit's file watcher walk every lazy transformers submodule.
LOCAL_MODEL_CACHE_FOLDER = "models--sentence-transformers--all-MiniLM-L6-v2"


# Describe HOW to launch the MCP server: a child process talking over stdio (stdin/stdout pipes,
# like a subprocess you pipe to). Output: a dict keyed by server name, as the client expects.
# True when the local embedding model is already downloaded to the Hugging Face cache.
# Then the MCP server can run offline: no huggingface.co requests, no "unauthenticated" warning, and a
# faster start. `make warm-models` downloads it once.
def local_model_is_cached() -> bool:
    from huggingface_hub.constants import HF_HUB_CACHE

    return any((Path(HF_HUB_CACHE) / LOCAL_MODEL_CACHE_FOLDER / "snapshots").glob("*/config.json"))


# Environment variables for the MCP server subprocess. Only what it needs is passed on.
def server_env() -> dict[str, str]:
    env = {
        "PYTHONPATH": str(ROOT / "src"),
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
    }
    if os.environ.get("HF_HOME"):
        env["HF_HOME"] = os.environ["HF_HOME"]
    if local_model_is_cached():
        env["HF_HUB_OFFLINE"] = "1"
    return env


def server_config() -> dict:
    return {
        SERVER_NAME: {
            "transport": "stdio",
            # The project's own interpreter: no `uv run` start-up cost, same environment.
            # sys.executable = absolute path of the running Python (the venv's python).
            "command": sys.executable,
            # `-m resources.mcp_server` runs that module as a program (~ java -cp ... MainClass).
            "args": ["-m", "resources.mcp_server"],
            # The child gets a minimal, explicit environment (not a copy of ours), so secrets
            # in our env are not leaked to it.
            "env": server_env(),
        }
    }


# Start the MCP server, load its tools, hand them to the caller, and shut the server down after.
# Output (via yield): a list of LangChain tools. Used once by cli.chat() around the whole chat.
# @asynccontextmanager ~ try-with-resources for async code: everything before `yield` is "open",
# the caller's `async with` body runs at the yield, and cleanup runs when the body exits.
# `async def` declares a coroutine (~ a method returning CompletableFuture); callers must `await` it.
@asynccontextmanager
async def mcp_tools() -> AsyncGenerator[list[BaseTool]]:
    """One server process for the whole chat; closed when the block exits.

    By default each tool call would start a new server, reloading the embedding model on
    every SOP search. A single session starts it once.
    """
    client = MultiServerMCPClient(server_config())
    # `async with` opens the session (spawns the server process) without blocking the event loop,
    # and guarantees it is closed on exit, even on error.
    async with client.session(SERVER_NAME) as session:
        # `await` pauses here (non-blocking) until the tool list arrives; `yield` then hands it to the
        # caller and suspends this function, keeping the session open until the caller's block ends.
        yield await load_mcp_tools(session)
