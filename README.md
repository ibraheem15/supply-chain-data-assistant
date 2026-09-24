# Supply Chain FDE Project

An agentic AI chat interface over a legacy SQL Server supply-chain dataset. See [`docs/problem_statement.md`](docs/problem_statement.md) for the full problem framing.

## Phase Status

| Phase | Status | Docs |
| --- | --- | --- |
| 1 — Local SQL Server development environment | In progress: Docker setup and CSV ingestion script are implemented; container setup and ingestion have not yet been run | [`docs/phase1-plan.md`](docs/phase1-plan.md) |

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
| `make lint` / `make format` | Ruff lint / format |
| `make test` | Run the test suite |

See [`docs/phase1-plan.md`](docs/phase1-plan.md) for the full Phase 1 walkthrough, including `.env.legacy_sqlserver` setup.

## Project Structure

```
.
├── AGENTS.md                  # Agent/teaching guidance for this repo
├── Makefile                   # Common dev commands (wraps uv)
├── pyproject.toml             # Project metadata and dependencies
├── uv.lock                    # Locked dependency versions
├── data/
│   ├── README.md              # Dataset documentation
│   └── raw/                   # Raw source CSV (gitignored)
├── docs/
│   ├── problem_statement.md   # Problem framing
│   └── phase1-plan.md         # Phase 1 scope and instructions
└── src/
    └── data/
        └── ingest_supply_chain_dataset.py  # CSV -> SQL Server loader
```
