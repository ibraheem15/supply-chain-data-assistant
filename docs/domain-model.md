# Domain Model

Canonical class and dependency diagram for what is **actually implemented** in `src/`.
Update this file whenever a domain/package changes, and keep
[`domain-model.html`](domain-model.html) in sync with it.

## Current State

`src/data/` contains two modules:

- `ingest_supply_chain_dataset.py` (Phase 1, complete) — raw CSV → local SQL Server.
- `ingest_sop_pinecone.py` (Phase 3, in progress) — SOP documents → Pinecone vector
  index. This covers **ingestion only** (parsing, chunking, embedding, incremental
  sync). The RAG *query* tool (question → embedding → Pinecone search → result), the
  text2sql tool, the `resources/` application layer, and the agent orchestration that
  picks between them are **not built yet** — see the last section below.

## Phase 1: Structured Data Ingestion

### Class Diagram

```mermaid
classDiagram
    class DatabaseConfig {
        <<Value Object>>
        +str host
        +int port
        +str database
        +str username
        +str password
        +str driver
    }
    class IngestionError {
        <<Domain Exception>>
    }
    class ingest_supply_chain_dataset {
        <<module>>
        +parse_csv(path) list~tuple~
        +load_config(args, env_file) DatabaseConfig
        +publish_records(config, records) None
        +main() int
    }
    ingest_supply_chain_dataset ..> DatabaseConfig : builds
    ingest_supply_chain_dataset ..> IngestionError : raises
```

### Dependency Diagram

```mermaid
flowchart LR
    CSV[("Raw CSV\ndata/raw/*.csv")] --> Parse["parse_csv()\nvalidation, pure"]
    EnvFile["envs/legacy_sqlserver.env"] --> Config["load_config()\nbuilds DatabaseConfig"]
    Parse --> Publish["publish_records()"]
    Config --> Publish
    Publish -->|pyodbc| Staging[("staging table")]
    Staging -->|one transaction| Final[("dbo.supply_chain_logistics")]
    Final --> SQLServer[("Local Docker\nSQL Server 2022")]
```

`parse_csv()` has no dependency on the database — it can be reasoned about, and
tested, with SQL Server never running. Only `load_config()` and `publish_records()`
depend on `pyodbc` and the environment file.

### DDD Concepts Applied Here

| Concept | Where |
| --- | --- |
| Ubiquitous Language | `COLUMNS` — the business-readable field names (`iot_temperature`, `risk_classification`) used everywhere except the one translation point below |
| Anti-Corruption Layer | `LEGACY_COLUMN_NAMES` — the single place that maps ubiquitous language to the legacy system's terse column names (`IOT_TEMP_C`) |
| Entity vs. Value Object | `DatabaseConfig` is a Value Object: no identity, immutable, defined only by its field values |
| Interface as Contract | `IngestionError` is the contract for "expected, actionable failure" vs. an unexpected bug |
| Dependency direction | Validation logic (`parse_csv`) depends on nothing infrastructure-related; only the database-facing functions depend on `pyodbc` |

## Phase 3 (In Progress): SOP Ingestion Pipeline

### Class Diagram

```mermaid
classDiagram
    class IngestError {
        <<Domain Exception>>
    }
    class PARSERS {
        <<dispatch table>>
        ".md" --> parse_markdown
        ".txt" --> parse_text
        ".pdf" --> parse_pdf
        ".csv" --> parse_csv
        ".xlsx" --> parse_xlsx
    }
    class ingest_sop_pinecone {
        <<module>>
        +parse_file(path) list~Document~
        +split_documents(docs) list~Document~
        +plan_sync(files, cache) dict
        +build_embeddings(provider, settings) tuple
        +ensure_pinecone_index(pc, name, dim, cloud, region) None
        +load_cache(path) dict
        +save_cache(path, cache) None
        +main() int
    }
    ingest_sop_pinecone ..> PARSERS : dispatches via
    ingest_sop_pinecone ..> IngestError : raises
```

### Dependency Diagram

```mermaid
flowchart LR
    Files[("data/policy/\n*.md .txt .pdf .csv .xlsx")] --> Parse["parse_file()\nformat dispatch"]
    Parse --> Split["split_documents()\nheading-aware for .md,\nsize-based safety net"]
    Cache[(".cache/sop_ingest_cache.json\nhash -> chunk_ids")] --> Plan["plan_sync()\nnew / changed / unchanged / deleted"]
    Split --> Plan
    Plan -->|deletes: old chunk_ids| VectorStore[("Pinecone index")]
    Plan -->|new+changed chunks| Embed["build_embeddings()\nlocal (default) or gemini"]
    Embed --> VectorStore
    VectorDBEnv["envs/vectorDB.env"] --> Provision["ensure_pinecone_index()\nself-heals dimension mismatch"]
    Provision --> VectorStore
    LLMEnv["envs/LLM.env"] --> Embed
```

### DDD Concepts Applied Here

| Concept | Where |
| --- | --- |
| Interface as Contract | `PARSERS` — every entry conforms to `Callable[[Path], list[Document]]`; adding a new file format means writing one function, not touching the pipeline |
| Anti-Corruption Layer | `_base_metadata()` is the one place a raw file path becomes the canonical `source`/`file_type` vocabulary attached to every chunk, regardless of format |
| Domain exception as a contract | `IngestError`, same role as Phase 1's `IngestionError` |
| Self-healing infrastructure | `ensure_pinecone_index()` detects a dimension mismatch (e.g. from switching embedding models) and recreates the index instead of failing opaquely |
| Simplified manifest (deviation from the original design) | `phase-3-plan.md` designed the new/changed/deleted sync as a SQL Server table; the actual implementation uses a local JSON file (`.cache/`) instead — same hash-based logic, no shared/durable state. Acceptable for a single-operator script; would need revisiting if this becomes a scheduled or multi-operator job |

## Planned, Not Yet Implemented

What's left to reach the design in `research.md` / `technical-design-doc.html`:

- **RAG query tool** — the other half of the unstructured path: take a user's
  question, embed it with the same model used at indexing time, search the Pinecone
  index, and return the matched chunks with their source metadata. Ingestion (this
  file's Phase 3 section above) does not include this.
- **text2sql tool** — the structured data path, via MCP, against a governed SQL Server
  view rather than the raw table.
- **Application layer (`resources/`)** — wraps both tools behind one interface the
  agent calls.
- **Agent orchestration** — the LangGraph ReAct loop that decides which tool a
  question needs.

Until that code exists, this section stays a pointer, not a diagram.
