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

# Phase 3b: Agent Security Layer (Read-Only User + Semantic View)

**Status: complete.** Implemented in `src/data/setup_security_and_view.sql`, run with
`make agent-db-setup`, and verified by the script's built-in self-check.

## Goal

Give the future text2sql tool a database identity that can read approved data and
nothing else. Security is enforced by SQL Server itself, so a bad prompt or a wrong
generated query cannot modify data, however it was produced (Phase 2 decision; see
`research.md`, Security).

## What gets created

| Object | Purpose |
| --- | --- |
| Login + user `USR_FDE_RO` | The agent's identity. Member of `public` only, no other roles |
| Schema `ai` (owned by `dbo`) | Home for agent-facing views, kept separate from the legacy `dbo` tables |
| View `ai.VW_SUPPLY_CHAIN_READINGS` | `dbo.supply_chain_logistics` with legacy names translated to the project's ubiquitous-language names (`IOT_TEMP_C` becomes `iot_temperature`) |

```
USR_FDE_RO --SELECT--> ai.VW_SUPPLY_CHAIN_READINGS --(ownership chain)--> dbo.supply_chain_logistics
USR_FDE_RO --any other access--> denied
```

## Schemas: `dbo` vs `ai`

`dbo` and `ai` are two **schemas** (folders) inside the one database, `SupplyChainDb`,
not two databases. Think of the database as a building and schemas as floors.

| Schema | Analogy | Holds | Used by |
| --- | --- | --- | --- |
| `dbo` | Ground floor | The real data: `supply_chain_logistics`, the staging table | The legacy system and our loader |
| `ai` | Reception desk | Only **views** (saved `SELECT` queries that store no data) | The agent |

Why separate them:

1. **Clear boundary.** Anything in `ai` is meant for the agent; anything in `dbo` is not.
2. **Safe growth.** A table added to `dbo` later is never visible to the agent by
   accident. Only something deliberately placed in `ai` and explicitly granted is.
3. **Translation.** The view renames terse legacy columns (`IOT_TEMP_C` becomes
   `iot_temperature`), so the agent never sees the legacy names.

## What `USR_FDE_RO` can do

| Action | Allowed? |
| --- | --- |
| `SELECT` from `ai.VW_SUPPLY_CHAIN_READINGS` | Yes, the only thing it can do |
| `SELECT` from `dbo.supply_chain_logistics` | No |
| Insert, update or delete, even through the view | No |
| Drop or create tables, views or schemas | No |

The view can read the table even though the user cannot, because SQL Server lets the
view's owner (`dbo`) read the table on the user's behalf. Like a bank teller who shows
you your balance without handing you the vault key.

## How it works

- **Ownership chaining.** The view and the table share an owner (`dbo`), so SQL Server
  does not check the user's permissions on the table when the view is read. The user
  needs `SELECT` on the view only.
- **Grant narrow, deny broad.** `GRANT SELECT` is on the single view, not the schema, so
  a view added later is not exposed by accident. `DENY` on `dbo` (select, write,
  execute, alter, view definition) and on `ai` (write, alter), plus `DENY CREATE ...`,
  overrides any role or grant. `DENY` does not break the view, because chained access
  skips the check.
- **The view is the second Anti-Corruption Layer.** The Python loader's
  `LEGACY_COLUMN_NAMES` translates names going in; the view translates them coming out.
  The mapping now lives in two places (Python and SQL) and must be kept in sync.
- **Secrets stay out of the script.** The password arrives as a `sqlcmd` variable from
  `envs/agent_sqlserver.env` (gitignored; template in `agent_sqlserver.example.env`).
  The script is idempotent and re-running it rotates the password.
- **Self-check built in.** The script ends by impersonating the agent (`EXECUTE AS USER`)
  and asserts: view `SELECT` works; raw-table `SELECT`, `DELETE`/`UPDATE` through the
  view, `DROP TABLE` and `CREATE TABLE` are all refused. Any failure aborts with an error.

## Run

```bash
cp envs/agent_sqlserver.example.env envs/agent_sqlserver.env   # set a strong password, no single quotes
make sqlserver-up && make ingest                               # table must exist first
make agent-db-setup
```

Expected output ends with six `PASS` lines and `All security checks passed.`

`sqlcmd` runs inside the container (`docker exec`), so no host SQL client is needed.

## Known limits (deliberately not solved here)

- **Design drift.** `research.md` and the TDD name the view `VW_ACTIVE_FLEET` with
  `Shipment_ID`, `Cargo_Type`, `Current_Temperature`. The dataset has no shipment id or
  cargo type and holds hourly historical readings, so that view cannot exist. The
  implemented view is `VW_SUPPLY_CHAIN_READINGS`; `research.md` has been updated to match.
- **`SELECT`-only does not stop expensive queries.** A `SELECT *` or a cross join can
  still be slow. Row caps and query timeouts belong in the text2sql tool (next step).
- **No audit log yet.** `AgentAuditLog` from the TDD is not part of this phase.
- **Dev-only secret handling.** The password is visible in the process list while
  `docker exec` runs. Acceptable locally; a client deployment would use a secret store.
- **Local only.** Like Phase 1, this targets the local Docker instance, not the
  client's server.
