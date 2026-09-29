# SOP Ingestion
data/policy will contain pdf, md, txt, csv, xlsx
These files will go to chunking logic
## Chunking Logic
### MD
Markdown files will be split into chunks based on headings and paragraphs. Each chunk will retain metadata such as the source file path and heading hierarchy.

### PDF
PDF files will be split into chunks based on pages and sections. Each chunk will retain metadata such as the source file path and page number.

### TXT
Text files will be split into chunks based on paragraphs. Each chunk will retain metadata such as the source file path and paragraph index.

### CSV
CSV files will be split into chunks based on rows. Each chunk will retain metadata such as the source file path and row index.

### XLSX
Excel files will be split into chunks based on sheets and rows. Each chunk will retain metadata such as the source file path, sheet name, and row index.

### Text Splitting
Then the chunks will be passed to the text splitting which will actually break them down into smaller, semantically meaningful pieces suitable for downstream processing and indexing.

## Embedding

text chunks will be converted into embeddings using a suitable embedding model (openai or local). These embeddings will then be stored in a vector database for efficient similarity search and retrieval during downstream tasks.

### Flow

```mermaid
flowchart LR
    User([User asks a question]) --> LLM[LLM / Agent]
    LLM --> Decide{Which tool?}
    Decide -->|Tool 1| SQLTool["text2sql tool\n(structured data, SQL Server)"]
    Decide -->|Tool 2| RAGTool["RAG tool\n(unstructured SOPs)"]
    RAGTool --> Embed["question -> embedding\n(same model used at indexing time)"]
    Embed --> VectorDB[("Vector database\ne.g. Pinecone")]
    VectorDB --> Retrieved[Retrieved chunks]
    SQLTool --> Answer[Answer]
    Retrieved --> Answer
```


Note that index passed to vector database should be consistent with the embeddings generated from the text chunks to ensure accurate retrieval. (if openai is used as the embedding model then the same model should be used for generating embeddings for both indexing and querying)

# Re-Indexing, Versioning & Deletion Sync

Every ingestion run needs to know, for each file in `data/policy/`, whether it is new,
changed, deleted, or unchanged since the last run. Without this, a rerun either misses
real edits or re-embeds every file every time — wasteful, and an actual cost problem
once embeddings go through a paid API.

## The Manifest: Local Source of Truth for "What's Indexed Right Now"

A small table (reusing the local SQL Server already running — no new infrastructure)
tracks one row per source file:

| Column | Purpose |
| --- | --- |
| `source_path` | e.g. `data/policy/Cold_Chain_Incident_SOP_v2.md` |
| `content_hash` | SHA-256 of the file's bytes — the only signal used to decide "did this change" |
| `chunk_ids` | the vector-DB IDs produced from this file, so they can be deleted precisely later |
| `doc_version` / `effective_date` | captured from the file's own front matter if present, for citation only — not used for change detection |
| `last_indexed_at` | timestamp, for the audit trail |

**Content hash, not modified-time, is the change signal.** File mtime can lie — `touch`,
a fresh `git checkout`, or clock drift can all change it without changing the content.
A hash of the bytes can't.

## Sync Flow

```mermaid
flowchart TD
    Start([Scan data/policy/]) --> ForEach[For each file on disk]
    ForEach --> Hash[Compute content_hash]
    Hash --> Lookup{In manifest?}

    Lookup -->|No| New["NEW\nchunk -> embed -> upsert vectors"]
    New --> InsertManifest[Insert manifest row]

    Lookup -->|Yes, hash differs| Changed["CHANGED\ndelete old chunk_ids from vector DB"]
    Changed --> Rechunk["chunk -> embed -> upsert vectors"]
    Rechunk --> UpdateManifest["Update manifest row\n(new hash, new chunk_ids)"]

    Lookup -->|Yes, hash same| Unchanged["UNCHANGED\nskip - no embedding call"]

    InsertManifest --> Log[Append to immutable audit log]
    UpdateManifest --> Log
    Unchanged --> Log

    Log --> AfterScan{Any manifest row whose\nsource_path is missing on disk?}
    AfterScan -->|Yes| Deleted["DELETED\ndelete chunk_ids from vector DB, remove manifest row"]
    Deleted --> Log
    AfterScan -->|No| Done([Done])
```

Every branch (new/changed/deleted) writes one entry to the same immutable audit log
already decided for the query path in Phase 2 — same concept, extended from "every
question and answer" to "every ingestion action." Unchanged files are the common case
on a rerun and cost nothing: no embedding call, no vector-DB write.

## Why This, Not Simpler Alternatives

- **Re-embedding every file on every run** — wrong on two counts: wasteful/costly, and
  it still doesn't solve deletion sync (a removed file's vectors would linger in the
  vector DB forever, answerable-but-wrong).
- **mtime-based change detection** — fragile, as above; content hash is deterministic
  regardless of filesystem metadata.
- **A file-watcher/daemon for continuous sync** — over-engineering for now. A manual,
  idempotent CLI command (`make sop-ingest`, mirroring Phase 1's `make ingest`) run on
  demand is consistent with how Phase 1 already works, and is enough until there's a
  real need for continuous sync.