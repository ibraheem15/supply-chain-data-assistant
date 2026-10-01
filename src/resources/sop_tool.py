"""search_compliance_sop: semantic search over the SOP chunks indexed in Pinecone (Phase 3a)."""

from __future__ import annotations

import logging
from typing import Any

from resources.settings import LLM_ENV_FILE, VECTORDB_ENV_FILE, load_settings

LOGGER = logging.getLogger("tools.sop")

MAX_TOP_K = 10
HEADER_KEYS = ("Header 1", "Header 2", "Header 3")
INGEST_HINT = "Run `make sop-ingest` to (re)build the SOP index."

_vector_store: Any = None


class SopIndexUnavailable(Exception):
    """The SOP index or its embedding model cannot be used."""


def _get_vector_store() -> Any:
    global _vector_store
    if _vector_store is not None:
        return _vector_store

    from langchain_pinecone import PineconeVectorStore
    from pinecone import Pinecone

    # Same builder the ingestion script uses, so query and index embeddings always match.
    from data.ingest_sop_pinecone import IngestError, build_embeddings

    settings = load_settings(VECTORDB_ENV_FILE, LLM_ENV_FILE, prefixes=("PINECONE_", "GEMINI_", "EMBEDDING_"))
    settings.setdefault("EMBEDDING_PROVIDER", "local")
    api_key = settings.get("PINECONE_API_KEY")
    if not api_key:
        raise SopIndexUnavailable(f"PINECONE_API_KEY is missing. Set it in {VECTORDB_ENV_FILE}.")
    try:
        embeddings, dimension = build_embeddings(settings["EMBEDDING_PROVIDER"], settings)
    except IngestError as exc:
        raise SopIndexUnavailable(str(exc)) from exc

    pc = Pinecone(api_key=api_key)
    index_name = settings.get("PINECONE_INDEX_NAME", "supply-chain-sops")
    if index_name not in pc.list_indexes().names():
        raise SopIndexUnavailable(f"Pinecone index {index_name!r} does not exist. {INGEST_HINT}")
    index_dimension = pc.describe_index(index_name).dimension
    if index_dimension != dimension:
        raise SopIndexUnavailable(
            f"Index dimension {index_dimension} does not match the embedding model ({dimension}). "
            f"{INGEST_HINT}"
        )
    _vector_store = PineconeVectorStore(index=pc.Index(index_name), embedding=embeddings)
    return _vector_store


def search_compliance_sop(query: str, top_k: int = 4) -> dict[str, Any]:
    """Return the SOP passages most similar to the query, with source and heading for citation."""
    if not query or not query.strip():
        return {"status": "error", "error": "query is empty. Describe the procedure or threshold needed."}
    if not isinstance(top_k, int) or not 1 <= top_k <= MAX_TOP_K:
        return {"status": "error", "error": f"top_k must be an integer from 1 to {MAX_TOP_K}."}

    try:
        matches = _get_vector_store().similarity_search_with_score(query, k=top_k)
    except SopIndexUnavailable as exc:
        return {"status": "unavailable", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - every failure must become tool output
        LOGGER.warning("search_compliance_sop failed: %s", exc)
        return {"status": "unavailable", "error": f"SOP search failed ({type(exc).__name__}). {INGEST_HINT}"}

    results = [
        {
            "text": document.page_content,
            "source": document.metadata.get("source", "unknown"),
            "headings": " > ".join(document.metadata[key] for key in HEADER_KEYS if key in document.metadata),
            "score": round(float(score), 4),
        }
        for document, score in matches
    ]
    return {"status": "ok", "query": query, "results": results}
