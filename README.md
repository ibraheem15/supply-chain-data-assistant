# Supply Chain FDE Project

An agentic AI chat interface over a legacy SQL Server supply-chain dataset. See [`docs/problem_statement.md`](docs/problem_statement.md) for the full problem framing.

## Phase Status

| Phase | Status | Docs |
| --- | --- | --- |
| 1 — Local SQL Server development environment | Complete: Docker container running, CSV validated and published to `dbo.supply_chain_logistics` | [`docs/phase1-plan.md`](docs/phase1-plan.md), [`docs/phase1-notes.md`](docs/phase1-notes.md) |
| 2 — Solution exploration | Complete: business case and technical design (HLD/LLD) drafted; some LLD items (chunking strategy, SOP ingestion pipeline) deferred to Phase 3 | [`docs/research.md`](docs/research.md), [`docs/technical-design-doc.html`](docs/technical-design-doc.html), [`docs/business-pitch.html`](docs/business-pitch.html), [`docs/phase2-notes.md`](docs/phase2-notes.md) |
| 3 — Implementation | Complete: SOP ingestion (3a), read-only agent user + semantic view (3b), and the three agent tools behind an MCP server (3c) | [`docs/phase3-notes.md`](docs/phase3-notes.md), [`docs/phase-3-plan.md`](docs/phase-3-plan.md) |
| 4 — Agent orchestration | Complete: LangGraph ReAct agent (Gemini or Groq reasoner) calling the Phase 3 tools via MCP, with a terminal chat (`make agent`) | [`docs/phase4-notes.md`](docs/phase4-notes.md), [`docs/phase-4-plan.md`](docs/phase-4-plan.md) |
| 5 — Audit log and Streamlit UI | Complete: append-only audit log with insert-only and read-only logins (5a), and a single-chat Streamlit UI with live tool cards and an audit viewer (5b) | [`docs/phase5-notes.md`](docs/phase5-notes.md), [`docs/phase5-plan.md`](docs/phase5-plan.md) |

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
| `make agent` | Chat with the agent in the terminal. Pass options with `AGENT_ARGS`, e.g. `make agent AGENT_ARGS="--provider groq --verbose"` (provider is Gemini by default, or Groq; `LLM_PROVIDER` in `envs/LLM.env` sets the default) |
| `make mcp-server` | Run the MCP server (three agent tools) over stdio |
| `make audit-db-setup` | Create the audit schema, table and the two audit logins (needs `envs/audit_sqlserver.env`) |
| `make warm-models` | Download the local SOP embedding model once, so the MCP server can run offline |
| `make ui` | Start the Streamlit chat and audit viewer on http://localhost:8501 (light and dark themes: top-right menu, Settings, Theme) |
| `make agent-db-setup` | Create the read-only agent user and `ai.VW_SUPPLY_CHAIN_READINGS` view (needs `envs/agent_sqlserver.env`) |
| `make lint` / `make format` | Ruff lint / format |
| `make test` | Run the offline unit tests (`uv run pytest -m integration` runs the live SQL Server/Pinecone tests) |

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
│   ├── agent_sqlserver.example.env   # Copy to agent_sqlserver.env (gitignored)
│   └── audit_sqlserver.example.env   # Copy to audit_sqlserver.env (gitignored)
├── docs/
│   ├── problem_statement.md        # Problem framing
│   ├── phase1-plan.md              # Phase 1 scope and instructions
│   ├── phase1-notes.md             # Phase 1 retrospective: what was built, concepts
│   ├── research.md                 # Source of truth: architecture, phases, DDD, roadmap
│   ├── domain-model.md             # Canonical class/dependency diagram (current code only)
│   ├── domain-model.html           # domain-model.md, rendered
│   ├── technical-design-doc.html   # Phase 2 TDD draft (HLD/LLD); implemented in Phases 3 to 5
│   ├── business-pitch.html         # Phase 2 business case draft
│   ├── phase2-notes.md             # Phase 2 retrospective and items deferred to Phase 3
│   ├── phase-3-plan.md             # Phase 3 design: SOP ingestion (3a), agent security layer (3b), agent tools (3c)
│   ├── phase3-notes.md             # Phase 3 retrospective: SOP ingestion, agent security layer, agent tools
│   ├── phase-4-plan.md             # Phase 4 plan: agent orchestration (implemented)
│   ├── phase4-notes.md             # Phase 4 retrospective: the orchestrator
│   ├── phase5-plan.md              # Phase 5 plan: audit log (5a) and Streamlit UI (5b)
│   └── phase5-notes.md             # Phase 5 retrospective: audit log, UI, runtime
├── src/
│   ├── data/
│   │   ├── ingest_supply_chain_dataset.py  # CSV -> SQL Server loader
│   │   ├── ingest_sop_pinecone.py          # SOP documents -> Pinecone (Phase 3a)
│   │   ├── setup_security_and_view.sql     # Read-only agent user + semantic view (Phase 3b)
│   │   └── setup_audit_log.sql             # Audit schema, table and insert-only/read-only logins (Phase 5a)
│   ├── resources/                          # Agent tools, application layer (Phase 3c)
│   │   ├── settings.py                     # Reads envs/*.env
│   │   ├── db.py                           # Shared SQL Server connection helper
│   │   ├── schema_context.py               # View description + legacy glossary for the LLM
│   │   ├── sql_tool.py                     # query_telemetry_data
│   │   ├── weather_tool.py                 # fetch_weather_conditions
│   │   ├── sop_tool.py                     # search_compliance_sop
│   │   └── mcp_server.py                   # Exposes the three tools over MCP (stdio)
│   ├── agent/                              # Orchestrator (Phase 4a) and audit (Phase 5a)
│   │   ├── llm.py                          # Gemini or Groq chat model (temperature 0, one retry)
│   │   ├── prompts.py                      # System prompt
│   │   ├── tools.py                        # Persistent MCP session -> LangChain tools
│   │   ├── graph.py                        # LangGraph ReAct wiring
│   │   ├── turn.py                         # run_turn(): one audited turn as a stream of events
│   │   ├── audit.py                        # Audit writer (insert-only) and reader (select-only)
│   │   ├── warm.py                         # Download the embedding model once
│   │   └── cli.py                          # Terminal chat
│   └── ui/                                 # Streamlit UI, Phase 5b
│       ├── runtime.py                      # Background agent runtime (event loop, MCP session, turn lock)
│       └── app.py                          # Chat, tool cards, sidebar, audit viewer
└── tests/                                  # Unit tests; integration tests need live services
```
