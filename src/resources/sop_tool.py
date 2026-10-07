from data.ingest_sop_pinecone import LOCAL_EMBEDDING_MODEL, connect_pinecone
from resources.settings import VECTORDB_ENV_FILE, load_settings

TOP_K = 3  # # chunks are enough

from functools import lru_cache


# @lru_cache is a decorator: it remembers the function's result, so the model loads on the first call only and is reused after. That's the only new Python in this step.
@lru_cache(maxsize=1)
def get_embedding_model():
    from langchain_huggingface import HuggingFaceEmbeddings

    return HuggingFaceEmbeddings(model_name=LOCAL_EMBEDDING_MODEL)


def search_compliance_sop(question: str) -> dict:
    if not question.strip():
        return {"status": "error", "message": "Question cannot be empty"}
    try:
        vectors = get_embedding_model().embed_query(question)
        settings = load_settings(VECTORDB_ENV_FILE)
        pc = connect_pinecone()
        index = pc.Index(settings["PINECONE_INDEX_NAME"])
        response = index.query(vector=vectors, top_k=TOP_K, include_metadata=True)
        results = [
            {
                "source": match.metadata["source"],
                "text": match.metadata["text"],
                "score": round(match.score, 3),
            }
            for match in response.matches
        ]
        return {"status": "ok", "results": results}
    except Exception as e:
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    print(
        search_compliance_sop(
            "What should I do when the cargo temperature is too high?"
        )
    )
