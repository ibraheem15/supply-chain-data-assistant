"""search_compliance_sop: semantic search over the SOP chunks indexed in Pinecone (Phase 3a)."""

# Lazy type hints so `X | None` and `list[str]` work in annotations (see settings.py).
from __future__ import annotations

import logging
from typing import Any

# Absolute import from our own package (`src` is on the Python path), like importing a Java class by
# package.
from resources.settings import LLM_ENV_FILE, VECTORDB_ENV_FILE, load_settings

# Named logger for this tool (like a per-class SLF4J logger).
LOGGER = logging.getLogger("tools.sop")

# Constants: upper bound on how many passages the agent may request, the metadata keys that hold the
# Markdown heading path of a chunk, and the standard recovery hint appended to error messages.
MAX_TOP_K = 10
HEADER_KEYS = ("Header 1", "Header 2", "Header 3")
INGEST_HINT = "Run `make sop-ingest` to (re)build the SOP index."

# Module-level cache of the Pinecone connection, created lazily on first use. Analogy: a lazily
# initialised singleton. The leading underscore means "private by convention" (Python has no `private`).
_vector_store: Any = None


# Custom exception (like `class X extends Exception` in Java), raised when the index or embedding model
# cannot be used. search_compliance_sop catches it and turns it into status "unavailable". The class
# body is just a docstring; no extra code is needed.
class SopIndexUnavailable(Exception):
    """The SOP index or its embedding model cannot be used."""


# Return the shared Pinecone vector store, building it on the first call and reusing it afterwards.
# Inputs: none (settings come from envs/*.env and the environment). Output: a PineconeVectorStore.
# Raises SopIndexUnavailable with an actionable message if anything is missing or mismatched.
def _get_vector_store() -> Any:
    # `global` is required to ASSIGN to the module-level variable; without it Python would create a new
    # local
    # variable. Reading a global needs no declaration.
    global _vector_store
    if _vector_store is not None:
        return _vector_store

    # Lazy imports inside the function: these heavy libraries load only when the SOP tool is first used, so
    # importing this module (and starting the MCP server) stays fast and works without them configured.
    from langchain_pinecone import PineconeVectorStore
    from pinecone import Pinecone

    # Same builder the ingestion script uses, so query and index embeddings always match.
    from data.ingest_sop_pinecone import IngestError, build_embeddings

    # Merge the two env files; environment variables with these prefixes are also picked up. Prefixes is a
    # tuple, so a one-element tuple would need a trailing comma.
    settings = load_settings(VECTORDB_ENV_FILE, LLM_ENV_FILE, prefixes=("PINECONE_", "GEMINI_", "EMBEDDING_"))
    # dict.setdefault: set the key to "local" only if it is absent (a default that does not overwrite).
    settings.setdefault("EMBEDDING_PROVIDER", "local")
    # dict.get returns None when the key is missing (instead of raising KeyError like settings[...]).
    api_key = settings.get("PINECONE_API_KEY")
    # `not` on None or an empty string is True: covers both "missing" and "blank".
    if not api_key:
        raise SopIndexUnavailable(f"PINECONE_API_KEY is missing. Set it in {VECTORDB_ENV_FILE}.")
    try:
        # The function returns a 2-tuple; unpacking assigns each part to its own variable.
        embeddings, dimension = build_embeddings(settings["EMBEDDING_PROVIDER"], settings)
    except IngestError as exc:
        # `raise ... from exc` converts the error to our exception type while keeping the original as its
        # cause
        # (like `new X(msg, cause)` in Java), so the stack trace shows both.
        raise SopIndexUnavailable(str(exc)) from exc

    # Create the Pinecone client (a network handle to the hosted vector database).
    pc = Pinecone(api_key=api_key)
    # dict.get with a second argument returns that default when the key is missing.
    index_name = settings.get("PINECONE_INDEX_NAME", "supply-chain-sops")
    # Fail early with a clear message if the index was never built (ingestion has not run).
    if index_name not in pc.list_indexes().names():
        raise SopIndexUnavailable(f"Pinecone index {index_name!r} does not exist. {INGEST_HINT}")
    # A vector index has a fixed vector size. If the embedding model produces a different size than the one
    # used at ingestion, searches would be meaningless or fail, so check it.
    index_dimension = pc.describe_index(index_name).dimension
    if index_dimension != dimension:
        # Two adjacent string literals (here, f-strings) are joined into one, so a long message can span
        # lines.
        raise SopIndexUnavailable(
            f"Index dimension {index_dimension} does not match the embedding model ({dimension}). "
            f"{INGEST_HINT}"
        )
    # Cache the store in the module-level global so later calls skip all the setup above.
    _vector_store = PineconeVectorStore(index=pc.Index(index_name), embedding=embeddings)
    return _vector_store


# TOOL 3 of 3: semantic (meaning-based) search over the SOP documents stored in Pinecone.
# Inputs: a natural-language query and top_k, how many passages to return (default 3, max MAX_TOP_K).
# Output: {"status": "ok", "query", "results": [{text, source, headings, score}]}, or status "error"
# (bad input) / "unavailable" (index or embedding problem). Never raises to the agent.
def search_compliance_sop(query: str, top_k: int = 3) -> dict[str, Any]:
    """Return the SOP passages most similar to the query, with source and heading for citation."""
    # Reject None, "" and whitespace-only strings. strip() removes leading/trailing whitespace.
    if not query or not query.strip():
        return {"status": "error", "error": "query is empty. Describe the procedure or threshold needed."}
    # Must be an int within 1..MAX_TOP_K. `not 1 <= x <= y` negates a chained comparison.
    if not isinstance(top_k, int) or not 1 <= top_k <= MAX_TOP_K:
        return {"status": "error", "error": f"top_k must be an integer from 1 to {MAX_TOP_K}."}

    try:
        # Embeds the query and returns a list of (Document, score) tuples, the closest matches first.
        matches = _get_vector_store().similarity_search_with_score(query, k=top_k)
    # Expected failure: setup problem we described ourselves. str(exc) gives its message.
    except SopIndexUnavailable as exc:
        return {"status": "unavailable", "error": str(exc)}
    # Catch-all (like `catch (Exception e)`): any other failure must still become a dict, not a crash. The
    # `noqa` marker tells the linter this broad catch is deliberate. Only the exception type name is
    # returned,
    # so internal details do not leak to the agent.
    except Exception as exc:  # noqa: BLE001 - every failure must become tool output
        LOGGER.warning("search_compliance_sop failed: %s", exc)
        return {"status": "unavailable", "error": f"SOP search failed ({type(exc).__name__}). {INGEST_HINT}"}

    # List comprehension: builds the results list in one expression, like a Java stream map(...).toList().
    # `for document, score in matches` unpacks each (Document, score) tuple.
    results = [
        {
            "text": document.page_content,
            "source": document.metadata.get("source", "unknown"),
            # Build a breadcrumb like "Title > Section > Subsection": a generator expression (a
            # comprehension without
            # brackets) picks the heading values that exist, and " > ".join glues them together.
            "headings": " > ".join(document.metadata[key] for key in HEADER_KEYS if key in document.metadata),
            # Convert to a plain float (the library may return a numpy type that JSON cannot encode), then
            # round.
            "score": round(float(score), 4),
        }
        for document, score in matches
    ]
    # Echo the query back with the results so the agent can cite what was searched.
    return {"status": "ok", "query": query, "results": results}
