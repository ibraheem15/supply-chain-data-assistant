# Phase 3 Notes: SOP Ingestion, Agent Security Layer and Agent Tools

**Status: Phase 3 is in progress, not complete.** This covers three pieces of it: the SOP
ingestion pipeline (3a), the agent's read-only database access (3b: least-privilege user
and semantic view), and the three agent tools exposed through an MCP server (3c). The
agent orchestration (LangGraph loop), the Streamlit UI and the audit log are not built
yet. See **What's Next** below.

## What We Built

An incremental pipeline that parses SOP documents by format, chunks them (heading-aware
for Markdown), embeds them, and syncs the result into a Pinecone vector index — only
touching files that are new, changed, or deleted since the last run.

```
3a: data/policy/*.{md,txt,pdf,csv,xlsx} -> parse (format dispatch) -> chunk -> embed -> Pinecone (incremental)
3b: dbo.supply_chain_logistics -> ai.VW_SUPPLY_CHAIN_READINGS (renamed columns) -> USR_FDE_RO (SELECT on the view only)
```

Phase 3b, in one line: the agent gets a database login that can read one renamed view
and nothing else.

```
3c: agent -> MCP server -> query_telemetry_data | fetch_weather_conditions | search_compliance_sop -> SQL Server view | Open-Meteo | Pinecone
```

## File Structure

```
data/
└── policy/
    └── Cold_Chain_Incident_SOP_v2.md
envs/
├── vectorDB.env / vectorDB.example.env    # PINECONE_*, EMBEDDING_PROVIDER
├── LLM.env / LLM.example.env              # GEMINI_*
└── agent_sqlserver.env / .example.env     # MSSQL_AGENT_* (3b; real file gitignored)
src/
└── data/
    ├── ingest_supply_chain_dataset.py     # Phase 1
    ├── ingest_sop_pinecone.py             # Phase 3a
    ├── __init__.py
    └── setup_security_and_view.sql        # Phase 3b
└── resources/                             # Phase 3c: application layer
    ├── settings.py                        # reads envs/*.env
    ├── schema_context.py                  # view description + legacy glossary for the LLM
    ├── sql_tool.py                        # query_telemetry_data
    ├── weather_tool.py                    # fetch_weather_conditions
    ├── sop_tool.py                        # search_compliance_sop
    └── mcp_server.py                      # exposes the three tools over stdio
tests/                                     # 45 unit tests + 2 integration tests
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

### Phase 3b: read-only agent access (schemas, views, ownership chaining)

`dbo` and `ai` are two **schemas** (folders) in the one database `SupplyChainDb`. `dbo`
holds the real tables; `ai` holds only views meant for the agent. The script
`setup_security_and_view.sql` creates:

- the login and user `USR_FDE_RO` (member of `public` only, no other roles),
- the schema `ai`, owned by `dbo`,
- the view `ai.VW_SUPPLY_CHAIN_READINGS`, which exposes all 26 columns under the
  project's ubiquitous-language names instead of the legacy ones.

`USR_FDE_RO` has `SELECT` on that one view and nothing else. Because the view and the
table share an owner, SQL Server skips the permission check on the table when the view is
read (**ownership chaining**) — the user can read through the view without any right to
the table. `DENY`s on `dbo` and `ai` (write, alter, create, view definition) override any
grant. The script is idempotent, takes the password from `envs/agent_sqlserver.env` as a
`sqlcmd` variable, and ends with a self-check that impersonates the user and confirms one
allowed read and five refused actions (raw-table `SELECT`, `DELETE`, `UPDATE`, `DROP`,
`CREATE`).

**Problems found while building it:**

- The live table had a column literally named `"DELAY_PROB"` (quotes included), left by an
  earlier loader version — `ensure_tables` only creates missing tables. Renamed with
  `sp_rename`; a loader rerun had been broken by it too.
- The Phase 2 design's `VW_ACTIVE_FLEET` (`Shipment_ID`, `Cargo_Type`) cannot exist: the
  dataset has no such columns and holds hourly history. `research.md` was updated.
- The legacy-to-friendly column mapping now lives in both Python (`LEGACY_COLUMN_NAMES`)
  and SQL (the view) and must be kept in sync.
- `SELECT`-only does not stop slow queries; row caps and timeouts belong in the text2sql
  tool.

### Phase 3c: the agent's tools (`src/resources/`)

Each tool is a plain Python function returning a dict, wrapped by one thin MCP server. All
three follow one contract and **never raise to the agent**: `ok`, `error` (the input was
wrong, the message says how to fix it, so the agent can retry) or `unavailable` (the backend
failed, the agent must say the data is missing and not guess).

- **`query_telemetry_data(sql)`**: the agent writes T-SQL. `sqlglot` parses it and accepts
  exactly one plain `SELECT` from `ai.VW_SUPPLY_CHAIN_READINGS` only; it connects as
  `USR_FDE_RO`, returns at most 200 rows (with a `truncated` flag), times out after 15
  seconds, and returns the executed SQL with the rows. These checks give fast feedback;
  the 3b database permissions remain the real boundary. `schema_context.py` gives the LLM
  column meanings, units, the data range, and a legacy-to-business name glossary generated
  from `LEGACY_COLUMN_NAMES` (the SOP quotes legacy names such as `IOT_TEMP_C`).
- **`fetch_weather_conditions(latitude, longitude)`**: Open-Meteo `current=` values for one
  point, 10 second timeout, one retry. `weather_disruption_level` is a labelled heuristic from
  documented thresholds, not an operational measure. Current conditions only: it cannot be
  compared with a historical reading's timestamp.
- **`search_compliance_sop(query, top_k)`**: embeds the query with the same
  `build_embeddings` the ingestion uses (so query and index embeddings match), searches
  Pinecone, and returns text with `source` and `headings` for citation.

**Problems found while building it:** `pyodbc` connection pooling reused a connection that
still had unread rows from a capped query and failed the next call ("Connection is busy");
fixed with `pyodbc.pooling = False`. `mcp` 2.x renamed `FastMCP` to `MCPServer`.

**Accepted limits:** free-tier Open-Meteo (vehicle coordinates go to a third party); no MCP
authentication (stdio, local only); single SOP file, so retrieval quality on a real library
is untested; no audit log yet.

## Python Concepts — Quick Reference

| Concept | Where used | What it's for |
| --- | --- | --- |
| `dict[str, Callable[[Path], list[Document]]]` | `PARSERS` | polymorphic dispatch by file suffix — the Python way to avoid an if/elif chain per format |
| Generator function | `_batched()` | yields fixed-size slices without building every batch in memory at once |
| Dict merge (`|`) | `_base_metadata(path) | {"page": ...}` | combines the shared metadata fields with format-specific ones into one new dict |
| Late/local imports | `from pypdf import PdfReader` inside `parse_pdf()`, etc. | keeps heavy, format-specific dependencies (pypdf, openpyxl, sentence-transformers) from loading unless that code path actually runs |
| Dependency injection by parameter | `fetch_weather_conditions(..., client=)` | tests pass a fake `httpx` client instead of calling the network |
| `monkeypatch` (pytest) | `tests/` | swaps a function (e.g. `_connect`) for a fake for one test |
| Test markers | `@pytest.mark.integration` | tests needing live services are skipped by default and run with `-m integration` |
| Atomic file write | `save_cache()`: write to `.tmp`, then `Path.replace()` | `replace()` is atomic on POSIX — a crash mid-write can't leave a half-written, corrupted cache file |

## FDE / DDD Concepts

- **Interface as Contract** — `PARSERS`, same pattern as Phase 1's `IngestionError`
  contract, applied to pluggable format support instead of error handling.
- **Anti-Corruption Layer** — `_base_metadata()` is the one place a raw file path
  becomes the canonical `source`/`file_type` vocabulary every chunk carries.
- **Self-healing infrastructure** — `ensure_pinecone_index()` treats a dimension
  mismatch as a recoverable, expected state rather than a fatal error.
- **Tool contract / bounded interface**: three tools, one return shape, expected failures
  returned as data. The agent never gets a raw database or HTTP connection.
- **Defence in depth**: SQL validation and row/time caps in the tool, database permissions
  underneath. Either alone would be weaker.
- **Same model at index and query time**: the SOP tool reuses `build_embeddings`.
- **Least privilege, enforced by the database** — Phase 2's read-only decision, now real:
  the agent's identity is refused writes by SQL Server itself, not by application code.
- **Anti-Corruption Layer, applied to security** — `ai.VW_SUPPLY_CHAIN_READINGS` is the
  Phase 2 governed view: the agent sees only an approved, translated slice of the schema.
- **Manifest pattern, simplified** — same new/changed/deleted logic designed in
  `phase-3-plan.md`, implemented against a local file instead of a database table. See
  [`domain-model.md`](domain-model.md) for this documented as a deliberate deviation.

## Run

```bash
uv sync
make sop-ingest-dry-run
make sop-ingest

# 3b: needs the SQL Server container up and the table loaded (make ingest)
cp envs/agent_sqlserver.example.env envs/agent_sqlserver.env   # set a strong password
make agent-db-setup                                            # ends with six PASS lines

# 3c
make test                       # 45 offline unit tests
uv run pytest -m integration    # live SQL Server + Pinecone checks
make mcp-server                 # MCP server over stdio
```

## What's Next

Still open in Phase 3, per [`domain-model.md`](domain-model.md)'s "Planned, Not Yet
Implemented" section:

- **Agent orchestration** (Phase 3d): the LangGraph loop that decides which tool a question
  needs, using the MCP server built in 3c, plus the audit log.
- **Streamlit UI** over that agent.

See [`research.md`](research.md) and [`technical-design-doc.html`](technical-design-doc.html)
for that design.
