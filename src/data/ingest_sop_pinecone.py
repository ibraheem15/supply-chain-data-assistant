# This will enable Semantic Search
# SOP files → split into chunks → embed each chunk (text → vector) → store in Pinecone
#  ↓
#  question → embed → find the nearest chunk vectors → return those chunks

from pathlib import Path

from langchain_core.documents import Document


from dotenv import load_dotenv
import os

# * Caching
import hashlib
import json

# * Pinecone configuration
from pinecone import Pinecone, ServerlessSpec

ROOT = Path(__file__).resolve().parents[2]

POLICY_DIR = ROOT / "data" / "policy"
VECTORDB_ENV_FILE = ROOT / "envs" / "vectorDB.env"
LLM_ENV_FILE = ROOT / "envs" / "LLM.env"

# * Chunking configuration
# Chunk size tells how large each text chunk should be before embedding
# Chunk overlap tells how much each chunk should overlap with the previous one to maintain context
CHUNK_SIZE = 400
CHUNK_OVERLAP = 60

HEADERS = [("#", "Header 1"), ("##", "Header 2"), ("###", "Header 3")]

# * Embedding model configuration
LOCAL_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
LOCAL_EMBEDDING_DIMENSION = 384

# Ensure the cache directory exists
CACHE_FILE = ROOT / ".cache" / "sop_ingest_cache.json"
CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)


class IngestError(Exception):
    """
    An expected, actionable ingestion failure.
    """


def read_sop_file(file_path: Path) -> str:
    """
    Reads an SOP file and returns its content as a string.

    Args:
        file_path (Path): The path to the SOP file.

    Returns:
        str: The content of the SOP file as a string.
    """
    if not file_path.exists():
        raise IngestError(f"File not found: {file_path}")

    return file_path.read_text(encoding="utf-8")


def split_markdown(text: str) -> list[Document]:
    """
    Splits the given markdown text into sections based on headers.

    Args:
        text (str): The markdown text to be split.

    Returns:
        list[Document]: A list of Document objects representing the sections.
    """
    from langchain_text_splitters import (
        MarkdownHeaderTextSplitter,
    )

    markdown_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=HEADERS)
    return markdown_splitter.split_text(text)


def split_sop_file(file_path: Path) -> list[Document]:
    """
    Reads an SOP file and splits its content into chunks using the custom character splitter.

    Args:
        file_path (Path): The path to the SOP file.

    Returns:
        list[Document]: A list of Document objects representing the text chunks from the SOP file.
    """
    from langchain_text_splitters import (
        RecursiveCharacterTextSplitter,
    )

    file_content = read_sop_file(file_path)
    sections = split_markdown(file_content)

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP
    )
    return text_splitter.split_documents(sections)


def embed_chunks(provider: str, chunks: list[Document]) -> list[list[float]]:
    """
    Embeds the given list of Document chunks into vectors using the local embedding model.

    Args:
        chunks (list[Document]): A list of Document objects to be embedded.

    Returns:
        list[list[float]]: A list of vectors representing the embedded chunks.
    """
    if provider == "local":
        from langchain_huggingface import HuggingFaceEmbeddings

        embedding_model = HuggingFaceEmbeddings(model_name=LOCAL_EMBEDDING_MODEL)

        texts = [chunk.page_content for chunk in chunks]
        return embedding_model.embed_documents(texts)

    if provider == "gemini":
        # TODO
        return []  # Placeholder until Gemini embedding implementation is added

    raise IngestError(
        f"Unknown EMBEDDING_PROVIDER {provider!r}; expected 'local' or 'gemini'."
    )


def connect_pinecone() -> Pinecone:
    load_dotenv(VECTORDB_ENV_FILE)  # Load environment variables from .env file
    api_key = os.getenv("PINECONE_API_KEY")
    if not api_key:
        raise IngestError("Pinecone API key not set in environment variables.")
    return Pinecone(api_key=api_key)


def create_serverless_index(pc: Pinecone) -> str:
    """
    Creates a serverless Pinecone index if it does not already exist.
    """
    load_dotenv(VECTORDB_ENV_FILE)  # Load environment variables from .env file
    index_name = os.getenv("PINECONE_INDEX_NAME")

    if not index_name:
        raise IngestError("Pinecone index name not set in environment variables.")

    if not pc.has_index(index_name):
        pc.create_index(
            name=index_name,
            dimension=LOCAL_EMBEDDING_DIMENSION,
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1"),
        )
    return index_name


def upload_chunks(pc: Pinecone, chunks: list[Document], source: str) -> None:
    """
    Uploads the given list of Document chunks to the Pinecone index.
    """
    index_name = create_serverless_index(pc)

    vectors = embed_chunks(os.getenv("EMBEDDING_PROVIDER", "local"), chunks)
    records = [
        {
            "id": f"{source}::{i}",
            "values": vector,
            "metadata": {
                **chunk.metadata,
                "text": chunk.page_content,
                "source": source,
            },
        }
        for i, (chunk, vector) in enumerate(zip(chunks, vectors))
    ]
    pc.Index(index_name).upsert(records)


def file_hash(file_path: str) -> str:
    """
    Computes the SHA-256 hash of the given file.
    """
    text = read_sop_file(file_path)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_cache() -> dict[str, str]:
    if not CACHE_FILE.exists():
        return {}
    return json.loads(CACHE_FILE.read_text(encoding="utf-8"))


def save_cache(cache: dict[str, str]) -> None:
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def find_sop_files() -> list[Path]:
    """
    Finds all SOP files in the POLICY_DIR directory.
    """
    if not POLICY_DIR.exists():
        raise IngestError(f"Policy directory {POLICY_DIR} does not exist.")
    return sorted(list(POLICY_DIR.glob("*.md")))

def sync_sop_files() -> None:
    """
    Synchronizes SOP files with the Pinecone index.
    """
    
    pc = connect_pinecone()
    cache = load_cache()
    for file_path in find_sop_files():
        file_key = str(file_path)
        current_hash = file_hash(file_path)
        if cache.get(file_key) != current_hash:
            chunks = split_sop_file(file_path)
            upload_chunks(pc, chunks, source=file_path.stem)
            cache[file_key] = current_hash
    save_cache(cache)
    
sync_sop_files()
