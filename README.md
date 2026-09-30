# Supply Chain FDE Project

An agentic AI chat interface over a legacy SQL Server supply-chain dataset. See [`docs/problem_statement.md`](docs/problem_statement.md) for the full problem framing.

## Phase Status

| Phase | Status | Docs |
| --- | --- | --- |
| 1 — Local SQL Server development environment | Complete: Docker container running, CSV validated and published to `dbo.supply_chain_logistics` | [`docs/phase1-plan.md`](docs/phase1-plan.md), [`docs/phase1-notes.md`](docs/phase1-notes.md) |
| 2 — Solution exploration | Complete: business case and technical design (HLD/LLD) drafted; some LLD items (chunking strategy, SOP ingestion pipeline) deferred to Phase 3 | [`docs/research.md`](docs/research.md), [`docs/technical-design-doc.html`](docs/technical-design-doc.html), [`docs/business-pitch.html`](docs/business-pitch.html), [`docs/phase2-notes.md`](docs/phase2-notes.md) |
| 3 — Implementation | In progress: SOP ingestion pipeline (3a) and read-only agent user + semantic view (3b) complete; RAG query tool, text2sql tool, `resources/` app layer, and agent orchestration not started | [`docs/phase3-notes.md`](docs/phase3-notes.md), [`docs/phase-3-plan.md`](docs/phase-3-plan.md) |

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (Python dependency manager and runner)
- Docker (for the local SQL Server container)
- unixODBC + Microsoft ODBC Driver 18 or 17 for SQL Server, installed on the **host** (the ingestion script is a client that connects out to the containerized database — see [`docs/phase1-plan.md`](docs/phase1-plan.md#ingest-the-raw-dataset) for why and for install commands)

## Setup

```bash
uv sync
```

This creates `.venv` and installs the locked dependencies from `uv.lock`.

## Common Commands

| Command | Purpose |
| --- | --- |
| `make install` | Lock and sync dependencies |
| `make sqlserver-up` / `make sqlserver-down` | Start / stop the local Docker SQL Server |
| `make ingest-dry-run` | Validate the raw CSV without connecting to SQL Server |
| `make ingest` | Load the validated CSV into the local SQL Server |
| `make agent-db-setup` | Create the read-only agent user and `ai.VW_SUPPLY_CHAIN_READINGS` view (needs `envs/agent_sqlserver.env`) |
| `make lint` / `make format` | Ruff lint / format |
| `make test` | Run the test suite |

See [`docs/phase1-plan.md`](docs/phase1-plan.md) for the full Phase 1 walkthrough, including `envs/legacy_sqlserver.env` setup.

## Project Structure

```
.
├── AGENTS.md                  # Agent/teaching guidance for this repo
├── Makefile                   # Common dev commands (wraps uv)
├── pyproject.toml             # Project metadata and dependencies
├── uv.lock                    # Locked dependency versions
├── data/
│   ├── README.md              # Dataset documentation
│   ├── raw/                   # Raw source CSV (gitignored)
│   └── policy/                # Sample SOP documents (RAG source)
├── envs/
│   ├── legacy_sqlserver.example.env  # Copy to legacy_sqlserver.env (gitignored)
│   ├── vectorDB.example.env          # Copy to vectorDB.env (gitignored)
│   ├── LLM.example.env               # Copy to LLM.env (gitignored)
│   └── agent_sqlserver.example.env   # Copy to agent_sqlserver.env (gitignored)
├── docs/
│   ├── problem_statement.md        # Problem framing
│   ├── phase1-plan.md              # Phase 1 scope and instructions
│   ├── phase1-notes.md             # Phase 1 retrospective: what was built, concepts
│   ├── research.md                 # Source of truth: architecture, phases, DDD, roadmap
│   ├── domain-model.md             # Canonical class/dependency diagram (current code only)
│   ├── domain-model.html           # domain-model.md, rendered
│   ├── technical-design-doc.html   # Phase 2 TDD draft (HLD/LLD) — not yet implemented
│   ├── business-pitch.html         # Phase 2 business case draft
│   ├── phase2-notes.md             # Phase 2 retrospective and items deferred to Phase 3
│   ├── phase-3-plan.md             # Phase 3 design: SOP ingestion (3a) and agent security layer (3b)
│   └── phase3-notes.md             # Phase 3 retrospective so far: SOP ingestion and agent security layer
└── src/
    └── data/
        ├── ingest_supply_chain_dataset.py  # CSV -> SQL Server loader
        ├── ingest_sop_pinecone.py          # SOP documents -> Pinecone (Phase 3a)
        └── setup_security_and_view.sql     # Read-only agent user + semantic view (Phase 3b)
```
