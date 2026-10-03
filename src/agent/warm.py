"""Download the local SOP embedding model once: python -m agent.warm (make warm-models).

After this the MCP server runs the model offline (see agent.tools.server_env), so a chat no longer
contacts huggingface.co and the first SOP search no longer waits on a download.
"""

from __future__ import annotations

import sys
import time

from data.ingest_sop_pinecone import build_embeddings
from resources.settings import LLM_ENV_FILE, VECTORDB_ENV_FILE, load_settings


def main() -> int:
    settings = load_settings(VECTORDB_ENV_FILE, LLM_ENV_FILE, prefixes=("PINECONE_", "GEMINI_", "EMBEDDING_"))
    provider = settings.get("EMBEDDING_PROVIDER", "local")
    if provider != "local":
        print(f"EMBEDDING_PROVIDER is {provider!r}: there is no local model to download.")
        return 0
    started = time.monotonic()
    # Creating the embeddings object downloads the model if it is not cached, then loads it.
    embeddings, dimension = build_embeddings(provider, settings)
    embeddings.embed_query("warm-up")  # forces a real forward pass, proving the model works
    print(f"Local embedding model ready ({dimension} dimensions, {time.monotonic() - started:.1f}s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
