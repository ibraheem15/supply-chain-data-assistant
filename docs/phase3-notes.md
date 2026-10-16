# Phase 3 Notes: SOP Ingestion, Agent Security Layer and Agent Tools

**Status: Phase 3 is rewritten by hand and complete.** The original AI-written implementation is in git history
(commit `dd9ee4a` moved it to `src_reference/`, later removed); everything in `src/` was written file by file on the `rewrite-by-hand` branch. Phase 3 covers
the SOP ingestion pipeline (3a), the agent's read-only database access (3b: least-privilege user and semantic
view) and three agent tools exposed through an MCP server (3c). The LangGraph orchestration, audit log and
Streamlit UI belong to Phases 4 and 5, which are not rewritten yet. See **What's Next** below.

## What We Built

A pipeline that reads SOP files, splits them into chunks, turns each chunk into a vector (an *embedding*) and
stores the vectors in a Pinecone index. Only files that are new, changed or deleted since the last run are
touched.

```
3a: data/policy/*.{md,txt,pdf,csv} -> parse -> chunk -> embed -> Pinecone (incremental sync)
3b: dbo.supply_chain_logistics -> ai.VW_SUPPLY_CHAIN_READINGS (renamed columns) -> USR_FDE_RO (SELECT on the view only)
3c: agent -> MCP server -> query_telemetry_data | fetch_weather_conditions | search_compliance_sop -> SQL Server view | Open-Meteo | Pinecone
```

Phase 3b in one line: the agent gets a database login that can read one renamed view and nothing else.

The rewrite uses `USR_FDE_RO` and the schema `ai`, so it sits next to the original `USR_FDE_RO` and `ai`
objects without touching them. The names can be unified when the rewrite is finished.

## File Structure

```
data/
└── policy/
    └── Cold_Chain_Incident_SOP_v2.md
envs/
├── vectorDB.env / vectorDB.example.env    # PINECONE_API_KEY, PINECONE_INDEX_NAME, EMBEDDING_PROVIDER
├── LLM.env / LLM.example.env              # Gemini / Groq keys (used from Phase 4)
└── agent_sqlserver.env / .example.env     # MSSQL_AGENT_*, MSSQL_SERVER, MSSQL_DATABASE, MSSQL_DRIVER (gitignored)
src/
├── data/
│   ├── ingest_supply_chain_dataset.py     # Phase 1
│   ├── ingest_sop_pinecone.py             # Phase 3a
│   ├── setup_security_and_view.sql        # Phase 3b
│   └── __init__.py
└── resources/                             # Phase 3c: application layer
    ├── settings.py                        # merges envs/*.env files into one dict
    ├── db.py                              # open_connection for the read-only agent login
    ├── schema_context.py                  # view and column names (with legacy names) for the LLM
    ├── sql_tool.py                        # query_telemetry_data
    ├── weather_tool.py                    # fetch_weather_conditions
    ├── sop_tool.py                        # search_compliance_sop
    ├── mcp_server.py                      # exposes the three tools over stdio
    └── __init__.py
tests/                                     # written for the original code; not ported to the rewrite yet
.cache/
└── sop_ingest_cache.json                  # generated; gitignored
```

## Topic Sections

### Semantic search in one line

```
SOP files -> chunks -> embed each chunk (text -> vector) -> store in Pinecone
question -> embed -> find the nearest chunk vectors -> return those chunks
```

"Embed" means to place text at a point in a 384-dimension space so that texts with similar meaning land close
together. Searching is then "find the nearest points". The question and the chunks must be embedded with the
**same model**, or the points live in different spaces and "nearest" means nothing.

### Parsing by file type (`if/elif`, one function per format)

`chunk_sop_file()` looks at the file suffix and calls one parser: `parse_md`, `parse_pdf`, `parse_csv` or
`parse_txt`. Each returns `list[Document]`, where a LangChain `Document` is `page_content` plus a `metadata`
dict. A plain `if/elif` chain was chosen over a dispatch dict because it is easier to read at this size.

- **PDF** uses PyMuPDF (`pymupdf.open`, `page.get_text()`): one `Document` per non-empty page, with the page
  number in the metadata.
- **CSV** uses `csv.DictReader` with `utf-8-sig` (strips the byte-order mark Excel adds). Each row becomes one
  `Document` whose text is `key: value, key: value`.
- **TXT** becomes one `Document` for the whole file.
- **MD** goes through the heading splitter described below.
- Any other suffix raises `IngestError`.

### Chunking: headings first, then a size-based splitter

`.md` files go through `MarkdownHeaderTextSplitter` first, so a chunk boundary never falls between a heading and
the rule it governs. Each section carries `Header 1`/`Header 2`/`Header 3` as real metadata. `.md` and `.pdf`
then pass through `RecursiveCharacterTextSplitter` (400 characters, 60 overlap); `split_documents()` keeps each
chunk's metadata through that second pass.

A CSV row is an atomic record whose meaning depends on all of its fields staying together, so CSV skips the
size splitter. A `.txt` file also skips it (see Accepted limits).

### Embeddings

`embed_chunks(provider, chunks)` calls `HuggingFaceEmbeddings(...).embed_documents(texts)` with
`sentence-transformers/all-MiniLM-L6-v2` (384 dimensions) when `EMBEDDING_PROVIDER` is `local` (the default).
Nothing leaves the machine, which matters because the SOP sample is marked "Internal Operations Only". The
`gemini` branch is a placeholder: it exists in the code but is not implemented. Any other provider name raises
`IngestError`. Heavy imports (`langchain_huggingface`, `pymupdf`, the splitters) are done inside the functions
that need them, so importing the module stays fast.

### Pinecone upload and stable ids

`create_serverless_index()` creates the index (384 dimensions, cosine metric, AWS `us-east-1`) only if it does
not exist. Each chunk is stored as a record with the id `<file stem>::<chunk number>`, the vector, and metadata
that includes the chunk text and the `source`. Stable ids mean re-uploading a file overwrites its own records.
There is no dimension self-healing: if the embedding model changes, the index has to be recreated by hand.

### Incremental sync with a local hash cache

`.cache/sop_ingest_cache.json` maps each file path to the SHA-256 of its raw bytes (bytes, not text, because a
PDF is binary). On each run `sync_sop_files()`:

- **new or changed file** (hash differs from the cache): delete all of its old records, chunk, embed, upload,
  update the cache and save it straight away, so a crash on a later file does not lose this one;
- **unchanged file**: print `skipped`;
- **deleted file** (in the cache but no longer in `data/policy/`): delete its records and its cache entry.

Deleting old records first matters: if a file shrinks from four chunks to three, the old fourth chunk would
otherwise stay in the index as a stale record. The records are found with `index.list(prefix="<stem>::")`
(prefix listing is why the ids have that shape), and removed with `index.delete(ids)`. Pinecone is eventually
consistent, so a record may still appear in the dashboard for a moment after an upload or delete.

Expected failures raise `IngestError`; `main()` catches it, prints one clean line and exits with code 1.

**Caveat:** the cache tracks *file content only*, not the ingestion code. Changing the chunking logic does not
change any file's hash, so the cache reports "unchanged" and the old vectors stay. Clearing the cache (and the
index) is the manual fix.

### Phase 3b: read-only agent access (logins, users, schemas, views, ownership chaining)

`setup_security_and_view.sql` builds the agent's identity and its view:

- a **login** `USR_FDE_RO`, created in `master` (server level; the password comes from the `sqlcmd`
  variable `$(AgentPassword)`; re-running rotates it),
- the schema `ai`, owned by `dbo`,
- the view `ai.VW_SUPPLY_CHAIN_READINGS`, which exposes all 26 columns under the ubiquitous-language names
  instead of the legacy ones (the same mapping as `LEGACY_COLUMN_NAMES` in Python),
- a database **user** `USR_FDE_RO` for that login, with `ai` as its default schema,
- `GRANT SELECT` on the single view, `DENY` on the `dbo` schema (select, write, execute, alter, view
  definition), `DENY` on writes to `ai`, and `DENY` on creating tables, views, procedures, functions and
  schemas,
- a self-check that impersonates the user (`EXECUTE AS USER`, `REVERT`) and tries to read the raw table.

A **login** is the server-wide keycard; a **user** is the permission list inside one database. Grants and
denies go on the user. Because the view and the table share an owner (`dbo`), SQL Server skips the permission
check on the table when the view is read (**ownership chaining**), so the agent can read through the view
without any right to the table. A `DENY` always beats a `GRANT`. Run it with `make agent-db-setup`, which pipes
the file into `sqlcmd` inside the Docker container and passes the agent password from `envs/agent_sqlserver.env`.
The script is idempotent: `IF NOT EXISTS` checks and `CREATE OR ALTER VIEW` make a second run harmless.

**Problems found while building it:**

- A login does not appear under the database's *Users* list; it is under the server's *Security -> Logins*
  until a user is created for it.
- `$(AgentPassword)` is only substituted by `sqlcmd`. Run from an ordinary editor, the login is created with the
  literal text as its password.
- `CREATE VIEW` must be the only statement in its batch, so the script needs `GO` between sections.
- `envs/legacy_sqlserver.env` has an unquoted value with spaces (`DB_DRIVER`), which makes the shell print
  `Driver: command not found` when the Makefile sources the file. Python's dotenv reads it fine.
- The legacy-to-business column mapping lives in both Python and SQL and must be kept in sync by hand.
- `SELECT`-only does not stop slow queries; the row cap and timeouts belong to the tool and the connection.

### Phase 3c: the agent's tools (`src/resources/`)

Each tool is a plain Python function that returns a dict and never raises to the agent: `{"status": "ok", ...}`
on success or `{"status": "error", "message": ...}` on an expected failure, so the agent can read the message
and react. One thin MCP server publishes them.

- **`settings.py`**: `load_settings(*env_files)` merges env files into one dict with `dotenv_values` (later
  files win). Constants hold the paths of the env files.
- **`db.py`**: `open_connection(settings)` builds the ODBC connection string from `MSSQL_*` keys of the agent
  env file. It never loads `legacy_sqlserver.env`, so the `sa` password is not reachable from the agent's code.
  Login timeout 10 s, per-query timeout 15 s.
- **`schema_context.py`**: `build_schema_context()` returns the text given to the LLM: the view name, the
  T-SQL rules (`TOP n`, never `LIMIT`; one `SELECT`) and one line per column in the form
  `name [SOP: LEGACY_NAME]`, built from `COLUMNS` and `LEGACY_COLUMN_NAMES` in the Phase 1 file. Column units
  and ranges are not included.
- **`query_telemetry_data(sql)`**: `validate_sql()` accepts only text that starts with `SELECT`, contains no
  `LIMIT`, no `;` and no `--`. The query runs as the agent login, returns at most 50 rows converted to strings
  (so they are JSON-safe) and a `truncated` flag, and returns the executed SQL. These checks give the LLM fast
  feedback; the 3b permissions remain the real boundary.
- **`fetch_weather_conditions(latitude, longitude)`**: calls Open-Meteo for current conditions, with a
  10 second timeout, and returns flat keys (`temperature_c`, `wind_speed_kmh`, `precipitation_mm`,
  `observed_at`). It is current weather only and cannot be compared with a historical reading.
- **`search_compliance_sop(question)`**: embeds the question with the same model used at ingestion (imported
  from the ingestion module so the two cannot drift apart), queries Pinecone for the top 3 chunks, and returns
  `source`, `text` and a similarity `score` for each. The model is loaded once with `@lru_cache`. Semantic
  search always returns its nearest neighbours, so a low score is the only sign of a weak match.
- **`mcp_server.py`**: `MCPServer("supply-chain-tools")` registers each function with `@mcp.tool(description=...)`
  and returns results as JSON text. The SQL tool's description embeds `build_schema_context()`. The
  descriptions are part of the contract, because the LLM picks a tool by reading them.

**Problems found while building it:** `mcp` 2.x renamed `FastMCP` to `MCPServer` (import
`mcp.server.mcpserver`). `pydantic_core.to_json` returns `bytes`, so it needs `.decode()` to be a `str`. The
imports inside `resources/` must be written as `from resources.x import ...` and run with `PYTHONPATH=src` (or
`python -m` from `src/`), because `data/` is only found when `src/` is on the path.

**Accepted limits:**

- The tools return only `ok` and `error`; a failure of the backing service (weather API, Pinecone) is not yet
  distinguished as `unavailable`, so the agent cannot tell "fix your input" from "service is down".
- `validate_sql` uses plain string checks, not a parser, so it is weaker than the original `sqlglot` version;
  the database permissions carry the real protection.
- `.txt` files are not split by size, so a long text file becomes one large chunk.
- The `gemini` embedding branch is not implemented.
- The 3b self-check covers only the raw-table denial; the denials of `DELETE`, `DROP` and `CREATE` are written
  but not exercised.
- Free-tier Open-Meteo (vehicle coordinates go to a third party), no MCP authentication (stdio, local only), a
  single SOP file so retrieval quality on a real library is untested, no audit log yet.
- `mcp>=2.3.0` is installed. The original Phase 4 agent needed `mcp<2` for `langchain-mcp-adapters`; check this
  again when Phase 4 is rewritten.

## Python Concepts: Quick Reference

| Concept | Where used | What it's for |
| --- | --- | --- |
| `if __name__ == "__main__":` | every file | code that runs only when the file is run, not when it is imported |
| `Path` (`/`, `.suffix`, `.stem`, `.name`, `.glob`, `.read_bytes`) | 3a, `settings.py` | file paths as objects |
| `enumerate`, `zip` | `parse_pdf`, `upload_chunks` | numbered items; pairing chunks with their vectors |
| List comprehension | `texts = [c.page_content for c in chunks]`, `build_schema_context` | build a list from another in one line |
| `{**a, "k": v}` | `upload_chunks` | copy a dict and add or override keys |
| Lazy imports (inside a function) | `parse_pdf`, `embed_chunks`, `get_embedding_model`, `open_connection` | heavy or system-dependent libraries load only when used |
| Custom exception | `IngestError` | an expected failure the caller can catch and report cleanly |
| `sys.exit(1)` | `main()` | non-zero exit code for scripts and `make` |
| `*args` | `load_settings(*env_files)` | accept any number of arguments, like Java varargs |
| `str \| None` hint | `validate_sql` | "a string or nothing" |
| `try / except / finally` | `sql_tool`, `weather_tool` | catch expected failures; always close the connection |
| `@lru_cache` decorator | `get_embedding_model` | load the model once and reuse it |
| `@mcp.tool(...)` decorator | `mcp_server.py` | register a function as an MCP tool; type hints become its parameter schema |
| `dict.update`, `dict.get(key, default)` | `load_settings` | merge dicts; read a key with a fallback |

## FDE / DDD Concepts

- **Semantic search / RAG retrieval**: embed documents once, embed the question at query time, return the
  nearest chunks.
- **Idempotent ingestion**: stable ids and a content hash mean running the pipeline twice changes nothing.
- **Anti-Corruption Layer**: the view `ai.VW_SUPPLY_CHAIN_READINGS` is the outbound counterpart of
  `LEGACY_COLUMN_NAMES`. The agent sees only an approved, translated slice of the schema.
- **Least privilege, enforced by the database**: the agent's identity is refused writes by SQL Server itself,
  not by application code.
- **Tool contract / bounded interface**: three tools, one return shape, expected failures returned as data.
  The agent never gets a raw database or HTTP connection.
- **Defence in depth**: tool validation and row and time caps on top of database permissions. Either alone
  would be weaker.
- **Same model at index and query time**: the SOP tool imports the model name from the ingestion module.
- **Single source of truth**: `COLUMNS` and `LEGACY_COLUMN_NAMES` feed the loader, the view and the schema text.
- **Manifest pattern, simplified**: new/changed/deleted logic against a local JSON file instead of a database
  table. Fine for one operator, per-machine state, worth revisiting for a scheduled job.

## Run

```bash
uv sync
make sop-ingest                 # sync data/policy/ to Pinecone (run twice: the second run prints "skipped")

# 3b: needs the SQL Server container up and the table loaded (make ingest)
cp envs/agent_sqlserver.example.env envs/agent_sqlserver.env   # set the password; user USR_FDE_RO
make agent-db-setup                                            # prints one row and "PASS  raw table SELECT denied"

# 3c: check each tool on its own, from the project root
PYTHONPATH=src uv run python -m resources.sql_tool
PYTHONPATH=src uv run python -m resources.weather_tool
PYTHONPATH=src uv run python -m resources.sop_tool
PYTHONPATH=src uv run python -c "import resources.mcp_server"   # loads without errors
make mcp-server                 # MCP server over stdio (waits for a client)
```

`make sop-ingest-dry-run` and `make test` belong to the original implementation: the rewrite has no `--dry-run`
option, and the tests in `tests/` still target the original code.

## What's Next

Phase 4 (done in the rewrite, see [`phase4-notes.md`](phase4-notes.md)): a LangGraph ReAct agent that decides
which tool a question needs and calls the 3c tools through the MCP server. Phase 5 adds the audit log and the
Streamlit UI ([`phase5-plan.md`](phase5-plan.md)).

See [`research.md`](research.md) and [`technical-design-doc.pdf`](technical-design-doc.pdf) for the underlying
design.
