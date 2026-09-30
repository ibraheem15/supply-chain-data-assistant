# Phase 1 Notes: Local SQL Server Development Environment

## What We Built

A local Docker SQL Server 2022 instance, and a CSV loader that validates the raw
supply-chain dataset and publishes it transactionally into `dbo.supply_chain_logistics`.

```
raw CSV → parse_csv() validation → staging table (batched insert) → transactional publish → dbo.supply_chain_logistics
```

## File Structure

```
.
├── AGENTS.md
├── Makefile
├── pyproject.toml
├── uv.lock
├── envs/
│   └── legacy_sqlserver.example.env
├── data/
│   ├── README.md
│   └── raw/                                    (gitignored; CSV downloaded manually)
├── docs/
│   ├── problem_statement.md
│   └── phase1-plan.md
└── src/
    └── data/
        └── ingest_supply_chain_dataset.py
```

## Topic Sections

### Environment & dependency management (`uv`)

`uv` plays the role a build tool plays in Java: `pyproject.toml` is the equivalent of
`pom.xml`/`build.gradle` (declares dependencies and dependency *groups* — a runtime
group `pyodbc` and a dev group `ruff`/`pytest`), and `uv.lock` is the equivalent of a
committed lockfile pinning exact resolved versions. `uv sync` reads the lock and
creates `.venv`, the project's isolated dependency environment.

### Docker as a local infrastructure dependency

The SQL Server container is started the same way you'd start any dependency service in
a dev/test environment — `docker run` with an env file for secrets, a published port
(`--publish 1433:1433`, `host:container`), and a **named volume**
(`legacy_sqlserver_data:/var/opt/mssql`) so data survives `docker stop`/`docker rm`
and container recreation. This is a local stand-in for a database the project will
eventually reach over the network at a client-supplied host/port — nothing here talks
to that real endpoint yet.

### Secrets handling

`MSSQL_SA_PASSWORD` lives in `envs/legacy_sqlserver.env`, which is gitignored — only
`envs/legacy_sqlserver.example.env` (placeholders only) is committed. `read_env_file()`
parses simple `KEY=VALUE` lines without ever logging their values, and the top-level
error handler in `main()` redacts the password out of any exception text before
logging it — ODBC driver errors can otherwise echo the connection string back verbatim.

### CSV validation as domain logic

`parse_csv()` is pure — it takes a file path and returns validated Python tuples, with
no database involved. It checks, in order: exact header match against the 26 expected
`COLUMNS`, no duplicate headers, every row has the right field count, then per-column
type coercion (timestamp parsing, `risk_classification` against a fixed set of
allowed values, everything else as a finite float). Any violation raises immediately
with the record number and column name — the whole file is rejected rather than
loading a partially-valid dataset.

### `pyodbc` and the ODBC driver stack

`pyodbc` is only the *client-side* Python binding. Connecting to the containerized
SQL Server from the host still needs a system-level ODBC stack installed on the host
itself — the same way a `psql` client binary is separate from a Postgres server.
`_choose_driver()` calls `pyodbc.drivers()` to discover what's actually installed
(unixODBC is the driver *manager*; `msodbcsql18`/`17` is the actual SQL Server driver)
and fails with an actionable message naming what's missing, rather than a raw
`libodbc.so` linker error.

### Transactional staging → publish pattern

`publish_records()` never writes straight into the final table. It truncates a
staging table, batch-inserts into it (1,000 rows per `executemany` call), verifies the
staged row count matches the parsed row count, then — inside one transaction — deletes
the old final-table contents and copies the staging rows in. Any exception anywhere in
that block rolls the whole transaction back, so a failed run leaves the previous
successful dataset untouched. Rerunning is idempotent because the final table's
contents are always fully replaced, not appended to.

### CLI ergonomics

`argparse` turns command-line flags into a structured `Namespace` (`args.csv`,
`args.dry_run`, etc.) instead of hand-parsing `sys.argv`. `--dry-run` runs only
`parse_csv()` and skips every database step entirely — useful as a fast, connection-free
sanity check (`make ingest-dry-run`) before touching SQL Server at all. `main()` returns
`0`/`1` and the script exits with that code, so `make ingest` fails loudly (non-zero
exit) on bad input instead of silently succeeding.

## Python Concepts — Quick Reference

| Concept | Where used | What it's for |
| --- | --- | --- |
| `from __future__ import annotations` | top of file | lets type hints like `str \| None` work without requiring the newest Python |
| `@dataclass(frozen=True)` | `DatabaseConfig` | an immutable value object — a bundle of related fields treated as one unit, can't be mutated after creation |
| `pathlib.Path` | `ROOT`, `DEFAULT_CSV` | filesystem paths as objects with methods (`.exists()`, `.is_file()`), not raw strings |
| `str \| None` / `dict[str, str]` | throughout | built-in generic and union type hints (no `Optional`/`Dict` imports needed) |
| custom `Exception` subclass | `IngestionError` | marks a failure as "expected and actionable," so it can be caught and reported cleanly instead of showing a raw traceback |
| `enumerate(iterable, start=N)` | `parse_csv` | iterate with a running counter that lines up with human-readable file line numbers |
| `with open(...) as f:` | CSV/connection handling | a context manager — guarantees the file or connection is closed even if an exception is raised inside the block |
| `try` / `except` / `finally` | `publish_records`, `main` | roll back a transaction or close a connection no matter how the block exits |
| module-level constants (tuple/dict) | `COLUMNS`, `SQL_TYPES`, `LEGACY_COLUMN_NAMES` | one source of truth, referenced everywhere instead of repeating literal strings |

## FDE / DDD Concepts

- **Ubiquitous Language** — `COLUMNS` are the vocabulary the rest of the project (and
  eventually the agent) will use: readable business names like `iot_temperature`,
  `risk_classification`. Everything downstream speaks in these names.
- **Anti-Corruption Layer** — `LEGACY_COLUMN_NAMES` is the *only* place that translates
  our ubiquitous language into the legacy system's terse historical column names
  (`IOT_TEMP_C`). Nothing else in the script — or anything built later — needs to know
  those legacy names exist.
- **Value Object** — `DatabaseConfig` is defined entirely by its field values, has no
  identity of its own, and is immutable (`frozen=True`) — the same properties a Value
  Object has in DDD.
- **Domain exception as a contract** — `IngestionError` is the explicit boundary
  between "this is a known, actionable failure" (bad CSV row, missing driver, wrong
  password) and an unexpected bug. Callers can catch it specifically.
- **Dependency direction** — `parse_csv()` has zero dependency on the database; it can
  be reasoned about (and tested) with SQL Server never running. The database-facing
  functions depend on `pyodbc`, but the validation rules don't depend on anything
  infrastructure-related.
- **Application layer as orchestration** — `main()` sequences the steps (validate →
  connect → stage → publish) but doesn't itself decide what a valid row is or what the
  legacy names are; those rules live in the constants and helpers it calls.

## Run

```bash
uv sync
make sqlserver-up
make ingest-dry-run
make ingest
make sqlserver-down   # when done for the day; the named volume keeps the data
```

## What's Next

Phase 2 — Solution Exploration. The business case and initial technical design (HLD/LLD)
are drafted in [`technical-design-doc.html`](technical-design-doc.html) and
[`business-pitch.html`](business-pitch.html), with supporting notes in
[`research.md`](research.md) — none of that is implemented yet. See
[`domain-model.md`](domain-model.md) for the current class and dependency diagrams,
which have since grown to cover the Phase 3 SOP ingestion and agent security layer.
