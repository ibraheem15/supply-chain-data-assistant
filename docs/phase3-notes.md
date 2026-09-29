# Phase 3 Notes: SOP Ingestion Pipeline

**Status: Phase 3 is in progress, not complete.** This covers one piece of it — the SOP
ingestion pipeline (chunking, embeddings, Pinecone vector store). The RAG query tool,
the text2sql tool, the `resources/` application layer, and the agent orchestration are
not built yet. See **What's Next** below.

## What We Built

An incremental pipeline that parses SOP documents by format, chunks them (heading-aware
for Markdown), embeds them, and syncs the result into a Pinecone vector index — only
touching files that are new, changed, or deleted since the last run.

```
data/policy/*.{md,txt,pdf,csv,xlsx} -> parse (format dispatch) -> chunk -> embed -> Pinecone (incremental)
```

## File Structure

```
data/
└── policy/
    └── Cold_Chain_Incident_SOP_v2.md
envs/
├── vectorDB.env / vectorDB.example.env    # PINECONE_*, EMBEDDING_PROVIDER
└── LLM.env / LLM.example.env              # GEMINI_*
src/
└── data/
    ├── ingest_supply_chain_dataset.py     # Phase 1
    └── ingest_sop_pinecone.py             # Phase 3 (this file)
.cache/
└── sop_ingest_cache.json                  # generated; gitignored
```

## Topic Sections

### Polymorphic parsing by file type

`PARSERS` is a dict mapping file suffix to a parser function (`parse_markdown`,
`parse_text`, `parse_pdf`, `parse_csv`, `parse_xlsx`), each returning
`list[langchain_core.documents.Document]`. Adding a new format later means writing one
function with that same shape, not touching `plan_sync()` or anything downstream. A
file that fails to parse is logged and skipped — one bad PDF doesn't kill the run.

### Heading-aware Markdown chunking, then a size-based safety net

`.md` files go through `MarkdownHeaderTextSplitter` first, so a chunk boundary never
falls between a heading (e.g. "3. Risk Classification Triggers") and the rule it
governs — each resulting section carries `Header 1`/`Header 2`/`Header 3` as real
metadata, not just text baked into the chunk. Every format then passes through
`RecursiveCharacterTextSplitter` (400 chars, 60 overlap) as a safety net for any
section that's still too long — `split_documents()` preserves each chunk's metadata
across that second pass. `.csv`/`.xlsx` skip this second pass entirely: each row is
already one short, complete chunk.

### Local embeddings by default, Gemini as an explicit opt-in

`EMBEDDING_PROVIDER=local` (the default) runs `sentence-transformers/all-MiniLM-L6-v2`
on-machine — nothing leaves the machine. `EMBEDDING_PROVIDER=gemini` is the opt-in
cloud path, added because `envs/LLM.env` holds Gemini credentials, not OpenAI's. The
SOP sample data is marked "Internal Operations Only," so keeping local as the default
(not just an option) matters — it mirrors the read-only-DB-role posture Phase 2 already
committed to.

### Pinecone self-healing provisioning

`ensure_pinecone_index()` creates the index if it doesn't exist. If it exists with the
wrong vector dimension — e.g. left over from switching `local` (384-dim) to `gemini`
(768-dim) — it deletes and recreates the index rather than failing with an opaque
dimension-mismatch error from Pinecone itself.

### Incremental sync via a local hash cache, not a SQL table

`phase-3-plan.md` designed this as a manifest table in SQL Server. The actual
implementation is simpler: a local JSON file (`.cache/sop_ingest_cache.json`) mapping
each source file to its content hash and the exact chunk IDs it produced. On each run,
`plan_sync()` buckets every file into new / changed / unchanged / deleted against that
cache — changed files get their old chunk IDs deleted before the new ones are added, so
nothing orphaned lingers in the index. This is a deliberate simplification: fine for a
single operator running this by hand, but it's per-machine state, not shared or
durable — worth revisiting if this ever becomes a scheduled job or has more than one
operator.

**Caveat worth knowing:** the cache only tracks *source file* content, not the
ingestion code itself. Changing the chunking logic (as happened once already, adding
the heading-aware splitter) doesn't change any file's hash, so the cache will report
"unchanged" and skip re-processing even though the old vectors in Pinecone were built
with the previous chunking. Clearing the cache and the index is the manual fix when
that happens.

## Python Concepts — Quick Reference

| Concept | Where used | What it's for |
| --- | --- | --- |
| `dict[str, Callable[[Path], list[Document]]]` | `PARSERS` | polymorphic dispatch by file suffix — the Python way to avoid an if/elif chain per format |
| Generator function | `_batched()` | yields fixed-size slices without building every batch in memory at once |
| Dict merge (`|`) | `_base_metadata(path) | {"page": ...}` | combines the shared metadata fields with format-specific ones into one new dict |
| Late/local imports | `from pypdf import PdfReader` inside `parse_pdf()`, etc. | keeps heavy, format-specific dependencies (pypdf, openpyxl, sentence-transformers) from loading unless that code path actually runs |
| Atomic file write | `save_cache()`: write to `.tmp`, then `Path.replace()` | `replace()` is atomic on POSIX — a crash mid-write can't leave a half-written, corrupted cache file |

## FDE / DDD Concepts

- **Interface as Contract** — `PARSERS`, same pattern as Phase 1's `IngestionError`
  contract, applied to pluggable format support instead of error handling.
- **Anti-Corruption Layer** — `_base_metadata()` is the one place a raw file path
  becomes the canonical `source`/`file_type` vocabulary every chunk carries.
- **Self-healing infrastructure** — `ensure_pinecone_index()` treats a dimension
  mismatch as a recoverable, expected state rather than a fatal error.
- **Manifest pattern, simplified** — same new/changed/deleted logic designed in
  `phase-3-plan.md`, implemented against a local file instead of a database table. See
  [`domain-model.md`](domain-model.md) for this documented as a deliberate deviation.

## Run

```bash
uv sync
make sop-ingest-dry-run
make sop-ingest
```

## What's Next

Still open in Phase 3, per [`domain-model.md`](domain-model.md)'s "Planned, Not Yet
Implemented" section:

- **RAG query tool** — the other half of the unstructured path: embed a question,
  search Pinecone, return matched chunks with source metadata to an agent.
- **text2sql tool** — the structured path, via MCP, against a governed SQL Server view.
- **Application layer (`resources/`)** — wraps both tools behind one interface.
- **Agent orchestration** — the LangGraph loop deciding which tool a question needs.

See [`research.md`](research.md) and [`technical-design-doc.html`](technical-design-doc.html)
for that design.
