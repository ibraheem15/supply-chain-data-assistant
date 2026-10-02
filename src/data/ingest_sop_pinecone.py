"""Ingest SOP documents from data/policy/ into a Pinecone vector index.

Incremental: a content hash per source file (cached locally) decides whether a file
is new, changed, unchanged, or deleted since the last run, so reruns only touch what
actually changed instead of re-embedding everything every time.
"""

# Makes all type hints lazy strings (not evaluated at import time), so newer hint syntax such as
# `X | None` and `list[str]` is safe to use. It must be the first statement after the docstring.
from __future__ import annotations

# --- Imports. Python's import ~ Java's import, but it also runs the module's top-level code. ---
# Standard library (ships with Python, like java.util / java.nio):
# argparse = CLI flag parser, csv = CSV reader, hashlib = SHA digests, json = JSON encode/decode.
import argparse
import csv
import hashlib
import json
import logging
import sys
import time
# Callable = "a function type" for hints (~ java.util.function.Function); Iterator = lazy sequence type.
from collections.abc import Callable, Iterator
# Path = object-oriented file path (~ java.nio.file.Path); much nicer than string concatenation.
from pathlib import Path
# Any = "no type constraint" (~ Object); used where the shape is too loose to describe.
from typing import Any

# Third-party packages (installed via uv/pip, like Maven dependencies).
# LangChain is a toolkit for LLM apps. Document = text (page_content) plus a metadata dict.
from langchain_core.documents import Document
# Embeddings = interface for "text -> vector of numbers" models (an embedding is that vector).
from langchain_core.embeddings import Embeddings
# Splitters cut long text into smaller pieces (chunks) that fit an embedding model well.
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

# ----- Module-level constants (UPPER_CASE by convention; Python has no `final`, it's just a promise). -----
# __file__ is this script's path. .resolve() makes it absolute; .parents[2] walks up two directories
# (src/data/this.py -> src/data -> src -> project root). Path `/` joins path segments.
ROOT = Path(__file__).resolve().parents[2]
POLICY_DIR = ROOT / "data" / "policy"
# Local cache of file hashes from the last run (like a lock/state file for incremental sync).
CACHE_FILE = ROOT / ".cache" / "sop_ingest_cache.json"
# Env files hold secrets/config (API keys, index name) outside source code.
VECTORDB_ENV_FILE = ROOT / "envs" / "vectorDB.env"
LLM_ENV_FILE = ROOT / "envs" / "LLM.env"

# Tabular files are already one row = one natural record, so they skip chunk splitting.
# {...} with bare values is a set (fast membership test, no duplicates).
NO_SPLIT_SUFFIXES = {".csv", ".xlsx"}
# Chunk = a small piece of a document that gets its own embedding. Sizes are in characters.
CHUNK_SIZE = 400
# Overlap = how many characters consecutive chunks share, so a sentence cut at a boundary
# still appears whole in at least one chunk.
CHUNK_OVERLAP = 60
# How many items to send to Pinecone / the embedding model per API call.
BATCH_SIZE = 100

# Markdown heading marker -> metadata key; a list of (marker, name) tuples. Text under a "##" heading
# gets {"Header 2": "<that heading>"} attached, which keeps the section context with each chunk.
MARKDOWN_HEADERS = [("#", "Header 1"), ("##", "Header 2"), ("###", "Header 3")]

# Two interchangeable embedding providers. Dimension = length of the vector the model outputs.
# The vector index must be created with exactly the same dimension or inserts will fail.
LOCAL_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
LOCAL_EMBEDDING_DIMENSION = 384
GEMINI_EMBEDDING_MODEL = "models/text-embedding-004"
GEMINI_EMBEDDING_DIMENSION = 768

# Named logger (~ SLF4J LoggerFactory.getLogger("sop_ingest")). Handlers/format are set up in main().
LOGGER = logging.getLogger("sop_ingest")


# Custom exception for failures we expect and can explain to the user (missing key, bad file, ...).
# `(Exception)` means "extends Exception". The body is just a docstring, so no `pass` is needed.
# main() catches only this type and turns it into a clean error message and exit code 1.
class IngestError(Exception):
    """An expected, actionable ingestion failure."""


# ==========================================
# 1. PATH RESOLUTION & SETUP
# ==========================================


# Tiny .env parser: reads a KEY=VALUE file into a dict (a Python dict ~ Java HashMap<String, String>).
# In: path to an env file. Out: dict of settings ({} if the file is absent). Pipeline step: setup/config.
# Type hints (`path: Path`, `-> dict[str, str]`) are documentation + tooling only; not enforced.
def read_env_file(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE entries without logging their values."""
    values: dict[str, str] = {}
    # Early return keeps the happy path unindented ("guard clause").
    if not path.exists():
        return values
    # `for x in iterable` ~ Java's enhanced for. splitlines() gives the file's lines as a list.
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        # Skip blank lines, comments, and lines without "=". `not x` on an empty string is True.
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        # Split on the first "=" only (values may contain "="). Tuple unpacking assigns both names at once.
        key, value = stripped.split("=", maxsplit=1)
        # Remove surrounding whitespace, then surrounding quote characters ('"' or "'") from the value.
        values[key.strip()] = value.strip().strip("'\"")
    return values


# Builds the final settings dict used by the rest of the script. Precedence (lowest to highest):
# vectorDB.env < LLM.env < real environment variables (like Spring's env-over-file precedence).
def load_settings() -> dict[str, str]:
    """Merge vectorDB.env and LLM.env; the process environment wins over both."""
    # `{**a, **b}` unpacks two dicts into one new dict; on duplicate keys the later (b) wins.
    merged = {**read_env_file(VECTORDB_ENV_FILE), **read_env_file(LLM_ENV_FILE)}
    # A "local import" inside a function: legal Python, runs on first call. Used here for a stdlib module.
    import os

    # Dict comprehension: `{k: v for k, v in ... if cond}` builds a dict by looping and filtering.
    # Keep an OS env var only if its key is already known, or starts with one of the listed prefixes
    # (str.startswith accepts a tuple of prefixes). update() then overwrites matching keys in `merged`.
    merged.update({k: v for k, v in os.environ.items() if k in merged or k.startswith(("PINECONE_", "GEMINI_", "EMBEDDING_"))})
    return merged


# Leading underscore = "private by convention" (Python has no `private` keyword).
# Fetches a mandatory setting or fails fast with an actionable message. Returns the non-empty value.
def _require(settings: dict[str, str], key: str) -> str:
    # dict.get returns None instead of throwing when the key is absent (unlike indexing with [key]).
    value = settings.get(key)
    # Catches both None and "" (empty string); both are "falsy" in Python.
    if not value:
        # f"...{key}..." is an f-string: expressions in braces are interpolated into the text.
        raise IngestError(f"{key} is missing. Set it in vectorDB.env, LLM.env, or the process environment.")
    return value


# ==========================================
# 2. PINECONE PROVISIONING
# ==========================================


# Makes sure a Pinecone vector index (a database that stores embeddings and finds the nearest ones to a
# query vector) exists with the right dimension, then waits until it is ready to accept data.
# In: Pinecone client `pc`, index name, vector dimension, cloud + region. Out: nothing (-> None).
# Pipeline step: provisioning, like an idempotent "create table if not exists" / Terraform apply.
def ensure_pinecone_index(pc: Any, name: str, dimension: int, cloud: str, region: str) -> None:
    # Imported here, not at the top, so --dry-run works without loading the (slow) Pinecone SDK.
    from pinecone import ServerlessSpec

    # Dict comprehension: build {index_name: index_info} so we can look up an index by name in O(1).
    existing = {index["name"]: index for index in pc.list_indexes()}
    current = existing.get(name)

    # A dimension mismatch means the embedding model changed; old vectors would be incompatible.
    if current is not None and current["dimension"] != dimension:
        # %r / %s are lazy logging placeholders (like SLF4J's {}): %r shows repr(), i.e. with quotes.
        LOGGER.warning(
            "Index %r has dimension %s but the configured embedding model produces %s; recreating.",
            name, current["dimension"], dimension,
        )
        pc.delete_index(name)
        current = None

    if current is None:
        # Keyword arguments (name=value) are common in Python. "cosine" = compare vectors by angle,
        # the usual similarity measure for text embeddings. Serverless = no pods to size or manage.
        pc.create_index(
            name=name,
            dimension=dimension,
            metric="cosine",
            spec=ServerlessSpec(cloud=cloud, region=region),
        )

    # Poll-until-ready loop (like waiting on a Kubernetes rollout) with a 60 second timeout.
    # monotonic() is a clock that never jumps backwards, so it is safe for measuring durations.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if pc.describe_index(name).status["ready"]:
            return
        time.sleep(1)
    # Only reached if the loop timed out without returning.
    raise IngestError(f"Pinecone index {name!r} did not become ready within 60s.")


# Factory: picks the embedding model (the thing that turns text into a vector) by provider name.
# In: "local" or "gemini" plus the settings dict. Out: a tuple (embedding model, its vector dimension).
# Pipeline step: embed. "local" runs on this machine (free); "gemini" calls Google's API (needs a key).
# `tuple[Embeddings, int]` is a fixed-size pair; Python functions can return several values this way.
def build_embeddings(provider: str, settings: dict[str, str]) -> tuple[Embeddings, int]:
    if provider == "local":
        # Local import: only the chosen provider's heavy library is loaded, and its package is only
        # required when that provider is actually used.
        from langchain_huggingface import HuggingFaceEmbeddings

        return HuggingFaceEmbeddings(model_name=LOCAL_EMBEDDING_MODEL), LOCAL_EMBEDDING_DIMENSION
    if provider == "gemini":
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        api_key = _require(settings, "GEMINI_API_KEY")
        # Parentheses around the returned tuple only allow it to span several lines.
        return (
            GoogleGenerativeAIEmbeddings(model=GEMINI_EMBEDDING_MODEL, google_api_key=api_key),
            GEMINI_EMBEDDING_DIMENSION,
        )
    raise IngestError(f"Unknown EMBEDDING_PROVIDER {provider!r}; expected 'local' or 'gemini'.")


# ==========================================
# 4. ROBUST POLYMORPHIC PARSER
# ==========================================


# Metadata every chunk carries: where it came from (path relative to project root) and its file type.
# Stored next to the vector in Pinecone so search hits can cite their source. In: file path. Out: dict.
# `.suffix` is ".md"; .lower() normalizes case; .lstrip(".") drops the leading dot -> "md".
def _base_metadata(path: Path) -> dict[str, Any]:
    return {"source": str(path.relative_to(ROOT)), "file_type": path.suffix.lower().lstrip(".")}


# Parser for .md files. All parse_* functions share one contract: Path in, list[Document] out.
# Splits the file by headings so each section becomes a Document tagged with its heading names.
# strip_headers=False keeps the heading text inside the content so the embedding "sees" it too.
def parse_markdown(path: Path) -> list[Document]:
    text = path.read_text(encoding="utf-8")
    splitter = MarkdownHeaderTextSplitter(headers_to_split_on=MARKDOWN_HEADERS, strip_headers=False)
    base = _base_metadata(path)
    sections = splitter.split_text(text)
    for section in sections:
        # Dict merge: file-level metadata first, then the section's own heading metadata on top.
        section.metadata = {**base, **section.metadata}
    return sections


# Parser for .txt files: the whole file becomes a single Document (chunking happens later).
# The `[ ... ]` literal builds a one-element list.
def parse_text(path: Path) -> list[Document]:
    return [Document(page_content=path.read_text(encoding="utf-8"), metadata=_base_metadata(path))]


# Parser for .pdf files: one Document per page that has extractable text, tagged with its page number.
# Raises IngestError for scanned/image-only PDFs (no text layer), which would otherwise index nothing.
def parse_pdf(path: Path) -> list[Document]:
    # Local import: pypdf is only needed (and loaded) when a PDF is actually encountered.
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    documents = []
    # enumerate(xs, start=1) yields (index, item) pairs counting from 1, so page numbers are human-style.
    for page_number, page in enumerate(reader.pages, start=1):
        # `a or b` returns b when a is falsy (None/empty), a null-safe default: None -> "".
        text = page.extract_text() or ""
        if text.strip():
            # `dict | dict` (Python 3.9+) merges into a new dict: base metadata plus the page number.
            documents.append(Document(page_content=text, metadata=_base_metadata(path) | {"page": page_number}))
    # An empty list is falsy, so this means "no pages produced any text".
    if not documents:
        raise IngestError(f"No extractable text in PDF: {path}")
    return documents


# Parser for .csv files: each data row becomes one Document like "col: value" per line, so a row is
# a self-contained, searchable record. Raises IngestError if the file has no data rows.
def parse_csv(path: Path) -> list[Document]:
    documents = []
    # `with` = try-with-resources: the file is closed automatically when the block exits.
    # "utf-8-sig" strips the BOM that Excel adds to CSVs; newline="" is what the csv module requires.
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        # DictReader yields each row as a dict keyed by the header row's column names.
        for row_number, row in enumerate(csv.DictReader(source), start=1):
            # "\n".join(generator) glues the strings with newlines. The inner `f"..." for ... in ...`
            # is a generator expression: a lazy loop that produces one "key: value" string per column.
            content = "\n".join(f"{key}: {value}" for key, value in row.items())
            documents.append(Document(page_content=content, metadata=_base_metadata(path) | {"row": row_number}))
    if not documents:
        raise IngestError(f"CSV has no data rows: {path}")
    return documents


# Parser for .xlsx (Excel) files: like parse_csv, but loops over every sheet; first row = column names.
# Raises IngestError if no sheet has data rows.
def parse_xlsx(path: Path) -> list[Document]:
    from openpyxl import load_workbook

    # read_only streams rows instead of loading everything; data_only returns cached formula results
    # (the computed values) rather than the formula text.
    workbook = load_workbook(path, read_only=True, data_only=True)
    documents = []
    for sheet in workbook.worksheets:
        # `rows` is an iterator (lazy, consumed once), so next() below really removes the header row.
        rows = sheet.iter_rows(values_only=True)
        # next(it, default) takes the next item, or the default (None) if the sheet is empty.
        header = next(rows, None)
        if header is None:
            continue
        for row_number, row in enumerate(rows, start=1):
            # zip(header, row) pairs column names with cell values; the trailing `if` filters out empty
            # cells. Same generator-expression + join pattern as in parse_csv.
            content = "\n".join(f"{col}: {value}" for col, value in zip(header, row) if value is not None)
            # Skip fully blank rows (empty string is falsy).
            if content:
                documents.append(
                    Document(page_content=content, metadata=_base_metadata(path) | {"sheet": sheet.title, "row": row_number})
                )
    if not documents:
        raise IngestError(f"Workbook has no data rows: {path}")
    return documents


# Dispatch table (~ a Java Map<String, Function<Path, List<Document>>> / strategy map): file extension ->
# parser function. Functions are first-class values in Python, so they can be stored in a dict.
# `Callable[[Path], list[Document]]` reads "a function taking a Path and returning a list of Documents".
# Supporting a new file type = write one parse_* function and add one line here.
PARSERS: dict[str, Callable[[Path], list[Document]]] = {
    ".md": parse_markdown,
    ".txt": parse_text,
    ".pdf": parse_pdf,
    ".csv": parse_csv,
    ".xlsx": parse_xlsx,
}


# Entry point of the parse step: looks up the right parser by extension and calls it.
# In: file path. Out: list of Documents. Raises IngestError for unsupported extensions.
def parse_file(path: Path) -> list[Document]:
    parser = PARSERS.get(path.suffix.lower())
    if parser is None:
        raise IngestError(f"Unsupported file type: {path.suffix}")
    # `parser` is a function stored in a variable; calling it is just parentheses.
    return parser(path)


# Chunk step: cuts parsed Documents into pieces of at most CHUNK_SIZE characters with CHUNK_OVERLAP shared.
# The "recursive" splitter prefers natural boundaries (paragraph, then line, then sentence, then word),
# so chunks rarely cut mid-sentence. Metadata of the parent Document is copied onto every chunk.
# In: list of Documents. Out: a longer list of smaller Documents (chunks).
def split_documents(documents: list[Document]) -> list[Document]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    return splitter.split_documents(documents)


# ==========================================
# 5. INCREMENTAL PIPELINE WITH BATCHING
# ==========================================


# Generator: yields the list in slices of `size` (the last slice may be shorter), e.g. 250 items with
# size 100 -> 100, 100, 50. Used to keep each Pinecone / embedding API call at a sane size.
# `yield` makes this a generator (~ a lazy Java Iterator): it produces one batch at a time, on demand,
# and pauses between them instead of building all batches up front. Return type is Iterator[list[Any]].
def _batched(items: list[Any], size: int) -> Iterator[list[Any]]:
    # range(start, stop, step) counts 0, size, 2*size, ... until len(items).
    for offset in range(0, len(items), size):
        # Slice: items[a:b] is a copy of elements a (inclusive) to b (exclusive); an end past the
        # list's length is fine (it just stops at the end), so no bounds check is needed.
        yield items[offset : offset + size]


# Finds every supported file under data/policy/ (recursively). Out: sorted list of paths, so runs are
# deterministic. Pipeline step: discovery (the first step of every run).
def discover_files() -> list[Path]:
    # rglob("*") = recursive glob of everything; the generator expression keeps only files whose
    # extension has a parser. sorted() consumes the generator and returns a list.
    return sorted(p for p in POLICY_DIR.rglob("*") if p.suffix.lower() in PARSERS)


# The heart of the incremental sync: compares files on disk with the cache from the last run and decides
# what must be embedded/uploaded (new or changed files) and what must be removed from Pinecone (old chunks
# of changed files, and chunks of deleted files). Like a `terraform plan`: it computes, it does not apply.
# In: files found on disk, and `cache` {relative path: {"hash": ..., "chunk_ids": [...]}}.
# Out: dict with "docs" (chunks to upload), "ids" (their Pinecone IDs), "deletes" (IDs to remove), "counts".
# NOTE: it mutates `cache` in place (Python passes references); main() saves it only after a good sync.
def plan_sync(files: list[Path], cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Diff files on disk against the cache; returns pending chunks/ids/deletes and counts."""
    # `set[str]` = unordered collection of unique strings with fast "is it in there?" checks.
    seen_paths: set[str] = set()
    pending_docs: list[Document] = []
    pending_ids: list[str] = []
    pending_deletes: list[str] = []
    counts = {"new": 0, "changed": 0, "unchanged": 0, "deleted": 0}

    for path in files:
        # Relative path (as text) is the stable key for a file in the cache and in chunk IDs.
        rel = str(path.relative_to(ROOT))
        seen_paths.add(rel)
        # SHA-256 fingerprint of the file's bytes: any edit changes it (~ a checksum / image digest).
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        cached = cache.get(rel)

        # `cached and ...` short-circuits: None (never seen) is falsy, so the hash check is skipped.
        if cached and cached["hash"] == digest:
            counts["unchanged"] += 1
            # Skip to the next file: nothing to re-embed.
            continue

        # try/except ~ Java try/catch. A bad file is logged and skipped so one broken document
        # doesn't abort the whole run. `as exc` binds the caught exception object.
        try:
            documents = parse_file(path)
        except IngestError as exc:
            LOGGER.error("Skipping %s: %s", rel, exc)
            continue

        # Conditional expression (Java's ternary `c ? a : b`): tabular files are used as-is (one row =
        # one chunk); everything else is split into chunks.
        chunks = documents if path.suffix.lower() in NO_SPLIT_SUFFIXES else split_documents(documents)
        # List comprehension: builds a list by looping. IDs look like "data/policy/x.md::0", "::1", ...
        # Deterministic IDs mean re-uploading a chunk overwrites it instead of creating a duplicate.
        ids = [f"{rel}::{i}" for i in range(len(chunks))]

        # Truthy `cached` here means the file was seen before but its hash differs, i.e. it changed.
        if cached:
            # extend() appends every element of another list (add() would append the list itself).
            # The old chunks of the previous version are queued for deletion, since the new version
            # may have fewer chunks.
            pending_deletes.extend(cached["chunk_ids"])
            counts["changed"] += 1
            # %d = integer placeholder in a lazy logging call.
            LOGGER.info("Changed %s -> %d chunk(s)", rel, len(chunks))
        else:
            counts["new"] += 1
            LOGGER.info("New %s -> %d chunk(s)", rel, len(chunks))

        pending_docs.extend(chunks)
        pending_ids.extend(ids)
        # Record the new fingerprint and chunk IDs (persisted later by save_cache once the sync succeeded).
        cache[rel] = {"hash": digest, "chunk_ids": ids}

    # Anything in the cache that was not seen on disk this run was deleted. The list comprehension makes a
    # snapshot list first, because deleting from a dict while iterating over it raises an error.
    for rel in [rel for rel in cache if rel not in seen_paths]:
        pending_deletes.extend(cache[rel]["chunk_ids"])
        del cache[rel]
        counts["deleted"] += 1
        LOGGER.info("Deleted %s", rel)

    return {"docs": pending_docs, "ids": pending_ids, "deletes": pending_deletes, "counts": counts}


# ==========================================
# 6. SYNC HASH CACHE
# ==========================================


# Loads the hash cache written by the previous run. Out: the cache dict, or {} on a first run
# (empty cache = every file counts as new). json.loads parses JSON text into dicts/lists.
def load_cache(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# Writes the cache atomically: write a temp file, then rename it over the real one. Like write-then-rename
# for safe config updates: a crash mid-write leaves the old cache intact, never a half-written JSON file.
# Only called after a fully successful sync, so a failed run is simply retried next time.
def save_cache(path: Path, cache: dict[str, dict[str, Any]]) -> None:
    # Create .cache/ if missing; exist_ok avoids an error when it is already there (like `mkdir -p`).
    path.parent.mkdir(parents=True, exist_ok=True)
    # e.g. "cache.json" -> "cache.json.tmp" (with_suffix swaps the extension, so the old one is re-appended).
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    # replace() is an atomic rename that overwrites the destination on the same filesystem.
    tmp_path.replace(path)


# Defines and parses the command-line flags (argparse ~ picocli/Commons CLI). Out: a Namespace object
# whose attributes are the flags, e.g. args.dry_run. Without flags the script does a real sync.
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync data/policy/ SOP documents into a Pinecone index.")
    parser.add_argument(
        "--dry-run",
        # store_true = boolean switch: False by default, True when the flag is passed (no value needed).
        action="store_true",
        help="Report what would change without contacting Pinecone or the embedding model.",
    )
    return parser.parse_args()


# Orchestrates the whole pipeline: discover -> plan (hash diff) -> [embed + sync to Pinecone] -> save cache.
# Returns a process exit code (0 = success, 1 = expected failure) instead of calling sys.exit itself,
# which keeps it easy to call from tests.
def main() -> int:
    # Configure logging once: INFO and above, printed as "LEVEL: message".
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args()
    # Any IngestError raised below is caught at the bottom and becomes a clean message + exit code 1.
    try:
        files = discover_files()
        if not files:
            LOGGER.warning("No supported files found under %s.", POLICY_DIR)

        cache = load_cache(CACHE_FILE)
        plan = plan_sync(files, cache)
        counts = plan["counts"]
        LOGGER.info(
            "Scan complete: %d new, %d changed, %d unchanged, %d deleted.",
            counts["new"], counts["changed"], counts["unchanged"], counts["deleted"],
        )

        # Dry run stops here: the plan was computed and logged, but no network or model is touched.
        if args.dry_run:
            LOGGER.info("Dry run complete; Pinecone was not contacted.")
            return 0

        # Nothing to upload and nothing to delete -> everything is up to date.
        if not plan["docs"] and not plan["deletes"]:
            LOGGER.info("Nothing to sync.")
            return 0

        settings = load_settings()
        # get(key, default): fall back to the local embedding model when EMBEDDING_PROVIDER isn't set.
        provider = settings.get("EMBEDDING_PROVIDER", "local")
        # Tuple unpacking of the (model, dimension) pair returned by build_embeddings.
        embeddings, dimension = build_embeddings(provider, settings)

        # Late imports: the Pinecone libraries are only loaded for a real sync, so --dry-run stays fast
        # and works even without Pinecone credentials.
        from pinecone import Pinecone
        from langchain_pinecone import PineconeVectorStore

        pc = Pinecone(api_key=_require(settings, "PINECONE_API_KEY"))
        index_name = settings.get("PINECONE_INDEX_NAME", "supply-chain-sops")
        ensure_pinecone_index(
            pc, index_name, dimension,
            cloud=settings.get("PINECONE_CLOUD", "aws"),
            region=settings.get("PINECONE_REGION", "us-east-1"),
        )
        # Vector store = LangChain wrapper that embeds text with `embeddings` and stores it in Pinecone.
        vector_store = PineconeVectorStore(index=pc.Index(index_name), embedding=embeddings)

        # Apply the plan. Deletes first (stale chunks of changed/removed files), in batches of BATCH_SIZE.
        for batch in _batched(plan["deletes"], BATCH_SIZE):
            vector_store.delete(ids=batch)
        # zip() walks two sequences in lockstep: batch N of docs pairs with batch N of their IDs.
        # add_documents embeds each batch (text -> vectors) and upserts it (insert or overwrite by ID).
        for doc_batch, id_batch in zip(_batched(plan["docs"], BATCH_SIZE), _batched(plan["ids"], BATCH_SIZE)):
            vector_store.add_documents(doc_batch, ids=id_batch)

        # Reached only if everything above succeeded; an exception earlier skips this, so the next run
        # redoes the unfinished work.
        save_cache(CACHE_FILE, cache)
        LOGGER.info("Synced %d chunk(s) to Pinecone index %r.", len(plan["docs"]), index_name)
        return 0
    # Expected failures (missing key, index timeout, ...) are reported without a stack trace; exit code 1
    # lets CI / Make / cron detect the failure. Unexpected exceptions still propagate with a traceback.
    except IngestError as exc:
        LOGGER.error("%s", exc)
        return 1


# Python's `public static void main` guard: __name__ is "__main__" only when this file is run directly
# (python -m src.data.ingest_sop_pinecone), and not when another module imports it (e.g. in tests).
# sys.exit(n) ends the process with exit code n, so main()'s return value becomes the shell's `$?`.
if __name__ == "__main__":
    sys.exit(main())
