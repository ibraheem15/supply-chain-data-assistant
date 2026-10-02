# Domain Model

Canonical class and dependency diagram for what is **actually implemented** in `src/`.
Update this file whenever a domain/package changes, and keep
[`domain-model.html`](domain-model.html) in sync with it.

## Current State

`src/data/` contains two Python modules and one SQL script, `src/resources/` holds the
agent's tools (Phase 3c), and `src/agent/` holds the orchestrator (Phase 4a):

- `ingest_supply_chain_dataset.py` (Phase 1, complete) — raw CSV → local SQL Server.
- `ingest_sop_pinecone.py` (Phase 3a, complete) — SOP documents → Pinecone vector
  index (parsing, chunking, embedding, incremental sync).
- `setup_security_and_view.sql` (Phase 3b, complete) — read-only agent user and view.
- `src/resources/` (Phase 3c, complete) — three agent tools behind one MCP server. The
  agent orchestration that picks between them is **not built yet** — see the last
  section below.

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

## Phase 3a: SOP Ingestion Pipeline

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

## Phase 3b: Agent Security Layer (SQL)

`setup_security_and_view.sql` is a SQL script, not Python, so there are no classes to
diagram. What it creates inside `SupplyChainDb`:

| Object | Role |
| --- | --- |
| `dbo.supply_chain_logistics` | Real table with legacy column names; written only by the Phase 1 loader |
| `ai.VW_SUPPLY_CHAIN_READINGS` | View over that table, columns renamed to the ubiquitous-language names |
| `USR_FDE_RO` (login + user) | The agent's identity: `SELECT` on the view only, everything else denied |

Dependency direction: `USR_FDE_RO` -> `ai.VW_SUPPLY_CHAIN_READINGS` -> `dbo.supply_chain_logistics`.
The user has no path to the table except through the view (ownership chaining).

| Concept | Where |
| --- | --- |
| Anti-Corruption Layer | The view is the outbound counterpart of `LEGACY_COLUMN_NAMES`: the loader translates names in, the view translates them out. The mapping exists in both places and must be kept in sync |
| Least privilege | `GRANT SELECT` on one object; `DENY` on `dbo` and `ai` schemas overrides any role or grant |
| Packages as layers | Schema `ai` is the agent-facing layer, `dbo` the legacy layer |

## Phase 3c: Agent Tools (`src/resources/`)

The application layer. Each tool is a plain function returning
`{"status": "ok" | "error" | "unavailable", ...}`; `mcp_server.py` is a thin wrapper that
registers them.

```mermaid
flowchart LR
    Agent["agent (Phase 4a)"] -->|MCP stdio| Server["mcp_server.py"]
    Server --> SQL["query_telemetry_data\nsqlglot validation, 50 row cap, 15s timeout"]
    Server --> Weather["fetch_weather_conditions\n10s timeout, one retry"]
    Server --> SOP["search_compliance_sop\nsame embedding model as indexing"]
    Context["schema_context.py\ncolumn docs + legacy glossary"] --> Server
    SQL -->|"USR_FDE_RO"| View[("ai.VW_SUPPLY_CHAIN_READINGS")]
    Weather --> OpenMeteo["Open-Meteo REST API"]
    SOP -->|"build_embeddings()"| Pinecone[("Pinecone index")]
```

| Concept | Where |
| --- | --- |
| Interface as Contract | The shared `ok`/`error`/`unavailable` result shape: expected failures are data the agent can act on, never exceptions |
| Defence in depth | `validate_sql()` and the row/time caps sit on top of the Phase 3b database permissions |
| Dependency direction | Tools depend on stores (SQL Server, Pinecone, HTTP); nothing in `src/data/` depends on tools. `sop_tool` imports `build_embeddings` from `data/` so query and index embeddings cannot diverge |
| Ubiquitous Language | `schema_context.py` generates the legacy-to-business glossary from `LEGACY_COLUMN_NAMES`, so the SOP's legacy names resolve to the names the agent can query |
| Packages as layers | `resources/` is the application layer between the agent and the data layer |

## Phase 4a: Agent Orchestrator (`src/agent/`)

A LangGraph ReAct agent. `graph.py` is pure wiring (the LLM and tools are arguments);
`llm.py` builds the Gemini or Groq chat model, `tools.py` opens one persistent MCP session to `mcp_server.py`,
`prompts.py` holds the system prompt, and `cli.py` is the terminal chat.

```mermaid
flowchart LR
    S([START]) --> R["Reasoner\n(LLM + system prompt)"]
    R -->|"tool call requested"| T["Tools\n(ToolNode over MCP)"]
    R -->|"no tool call: final answer"| E([END])
    T -->|"tool results"| R
```

| Concept | Where |
| --- | --- |
| Dependency direction | `agent/` depends on `resources/` only through MCP; `resources/` knows nothing about the agent. `graph.py` depends on no I/O, so it is tested with a scripted fake LLM |
| Interface as Contract | The agent sees three MCP tools and their descriptions, never the Python functions or the database |
| State as the single source of truth | The message list is the graph state; the checkpointer (`MemorySaver`, one `thread_id` per chat) gives conversation memory |
| Packages as layers | `agent/` is the orchestration layer above `resources/` (application layer) above `data/` |

## Planned, Not Yet Implemented

What's left to reach the design in `research.md` / `technical-design-doc.html`
(see [`phase-4-plan.md`](phase-4-plan.md)):

- **Audit log (4b)** (`AgentAuditLog`), written by a separate insert-only login.
- **Streamlit UI (4c)** over the same graph.

Until that code exists, this section stays a pointer, not a diagram.
