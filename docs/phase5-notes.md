# Phase 5 Notes: Audit Log and Streamlit UI (rewrite)

**Status: Phase 5 is complete in the rewrite, apart from tests.** The audit log (5a) was written by hand: the SQL
script, the writer, the reader and the shared turn loop. The Streamlit UI (5b) was **adapted from the reference
implementation** (in git history: commit `dd9ee4a`, `src_reference/ui/`), not written by hand, and ported to the rewrite's function names. See
[`phase5-plan.md`](phase5-plan.md) for the original design. The reference version of these notes (with its tests and
its live-run findings) is in git history.

## What We Built

An append-only audit trail of everything the agent does, enforced by SQL Server permissions, and a Streamlit chat
that shows each tool call live and has a viewer for the audit log.

```
question -> run_turn() -> graph.astream() -> one event per step -> audit row (INSERT-only login) + event to CLI/UI
                                                                        audit tab <- SELECT-only login <- audit_2.AgentAuditLog
```

## File Structure

```
src/
├── data/
│   └── setup_audit_log.sql    # audit_2 schema, AgentAuditLog table, two logins, grants and denies
├── resources/
│   ├── db.py                  # open_connection(settings, user_key, password_key): defaults to the agent's login
│   └── settings.py            # + AUDIT_SQL_ENV_FILE
├── agent/
│   ├── audit.py               # record() writer, redact(), ping(), AuditReader (sessions, search)
│   ├── turn.py                # TurnEvent, run_turn(): the shared turn loop
│   └── cli.py                 # prints the events of run_turn()
└── ui/                        # adapted from the reference implementation
    ├── runtime.py             # background thread: event loop, MCP session, graph, one-turn lock
    └── app.py                 # chat, live tool cards, sidebar status, audit viewer
.streamlit/config.toml         # light and dark themes, file watcher off, toolbar "viewer"
```

The rewrite uses new names so it can sit next to the reference objects in the same database: schema `audit_2`,
logins and users `USR_FDE_AUDIT_W_2` and `USR_FDE_AUDIT_R_2`.

## Topic Sections

### Three identities, one table

| Login | Can | Cannot | Used by |
| --- | --- | --- | --- |
| `USR_FDE_RO_2` (3b) | `SELECT` on `ai_2.VW_SUPPLY_CHAIN_READINGS` | anything on `audit_2` (explicit `DENY`) | the SQL tool |
| `USR_FDE_AUDIT_W_2` | `INSERT` on `audit_2.AgentAuditLog` | `SELECT`, `UPDATE`, `DELETE`, `ALTER` | `audit.record()` |
| `USR_FDE_AUDIT_R_2` | `SELECT` on `audit_2.AgentAuditLog` | writes of any kind | `AuditReader` (the audit tab) |

The agent cannot read or edit its own trail, the writer can only append, and the reader cannot write. The log is in
its own schema, not in `ai_2`: that schema is the agent-facing read layer, and a write grant there would end the
read-only guarantee. The one party who can still edit history is the database owner (`sa`/`dbo`), which is accepted
for a development system. A client deployment would add hash chaining or write-once storage.

`setup_audit_log.sql` follows the 3b pattern: logins created in `master` with `IF NOT EXISTS ... CREATE LOGIN ...
ELSE ALTER LOGIN` (so re-running also rotates the passwords), the schema owned by `dbo` created through `EXEC`,
users created for the logins, and one `GRANT` plus explicit `DENY`s for each user. `DENY` is written even where
nothing was granted, because a `DENY` holds even if someone later adds a role or a grant. Passwords come in as
`sqlcmd` variables (`$(WriterPassword)`, `$(ReaderPassword)`) from `envs/audit_sqlserver.env`. Run it with
`make audit-db-setup`. The reference script also had a self-check that impersonates each login and prints PASS or
FAIL; the rewrite has none, and the boundary was checked by hand with `EXECUTE AS USER`.

### One row per event

`audit_2.AgentAuditLog` has one row per event, so a turn can be replayed in order: `SessionId` (the chat's
`thread_id`), `TurnId` (one question), `StepNo`, `Node`, `EventType` (`user_prompt`, `tool_call`, `tool_result`,
`final_answer`, `error`), the tool name and call id, `Status`, the input and output in full, the error text,
provider and model, `DurationMs` (time since the previous event) and the token counts.

- **`IDENTITY(1,1)`:** the database numbers the rows, so the app cannot reuse or fake a `LogId`.
- **`DEFAULT SYSUTCDATETIME()`:** the database stamps the time, so the app cannot backdate an entry.

### The writer (`audit.py`)

- `_insert(event)` builds `INSERT INTO audit_2.AgentAuditLog (cols) VALUES (?, ?, ...)` from a whitelist of
  column names (names go into the SQL text, so only names from `COLUMNS` are used). Values travel as `?`
  parameters, so a prompt that contains SQL is stored as text and never runs. `connection.commit()` is required:
  `pyodbc` does not auto-commit, and without it the row is rolled back.
- `record(event)` runs `_insert` with `asyncio.to_thread` (the call blocks, and the chat's event loop must not
  freeze) and returns `True` or `False`. It **never raises**: a broken log must not take the agent down. The
  `except Exception` is deliberately broad, and the `False` is what makes the failure visible.
- `redact(text)` replaces every secret value found in the env files (any setting whose name contains `PASSWORD`,
  `KEY`, `TOKEN` or `SECRET`, 8 characters or longer) with `***`. It works on values, not patterns, so an unusual
  key format is still caught. It is applied to every text field. A secret that is not in those env files would not
  be caught.
- `open_connection(settings, user_key, password_key)` takes the setting names, so the same helper opens the
  agent's, the writer's or the reader's connection. The audit code merges the agent env file (server, driver,
  database) with the audit env file (the two logins) through `load_settings(*env_files)`.

### Notify and carry on

If a write fails, the agent keeps working and the front end shows one warning per turn ("this turn is not being
recorded"). The cost is a gap in the trail, which is why the warning is not optional.

### One shared turn loop (`turn.py`)

`run_turn(graph, question, session_id, provider, model)` is an **async generator**: an `async def` with `yield`,
read with `async for`, so the caller receives each event while the agent is still working. It calls
`graph.astream(..., stream_mode="updates")`, which yields one dictionary per finished node, and turns each message
into an audit row and a `TurnEvent`:

| Message | Audit row | Event |
| --- | --- | --- |
| user question | `user_prompt` | none |
| `AIMessage` with tool calls | one `tool_call` per call (arguments as JSON, token usage once) | `tool_call` |
| `ToolMessage` | `tool_result` with the `status` read from the tool's JSON | `tool_result` |
| `AIMessage` without tool calls | `final_answer` | `answer` |
| an exception | `error` | `error` |

An inner function cannot `yield` for its caller, so `log()` appends events to a `pending` list and the main loop
yields them. `run_turn` never raises: any exception (quota, network, the graph's step limit) becomes an `error`
row and event. Both the CLI and the UI consume it, so neither can skip the audit, and `graph.py` stays free of I/O.

### Streamlit and an async agent

Streamlit re-runs the whole page script on every click, but the agent is async and keeps one persistent MCP
session. `AgentRuntime` (`ui/runtime.py`) therefore owns **one background thread with its own event loop**, started
once through `st.cache_resource`. It opens the adapter, builds the graph, sends one throwaway SOP query so the
embedding model is loaded before the first question, and waits. The page submits a turn with
`asyncio.run_coroutine_threadsafe` and reads events from a `queue.Queue`. A `threading.Lock` allows one turn at a
time: a second browser tab gets a "busy" message.

### The UI

A chat with a status box that shows each tool call as it happens (SQL shown as SQL), tool cards (expanders with
arguments, a result preview, status and timing) in the history, and error and audit-warning banners. The sidebar
has "New chat" (a new `thread_id`; the old chat is kept only in the audit log) and status dots for the agent, the
tools, the SOP model and the audit log. The **Audit log** tab filters by chat, tool, event, status, dates and free
text, and opens any entry in full. It reads past chats too, because the log persists. `.streamlit/config.toml`
defines a light and a dark theme; the viewer switches in the top-right menu.

### Porting the reference UI: what had to change

| Mismatch | Fix |
| --- | --- |
| `TurnEvent` had no tool-call id, argument dict or duration | Added `tool_call_id`, `data` and `duration_ms`; `run_turn` fills them |
| The UI used an `AuditLogger` object and `AuditReader` class | `audit.py` gained `AuditReader` (`sessions()`, `search()`) and `ping()`; the writer stays a function |
| `runtime.audit` no longer exists (`AttributeError` in the sidebar) | The sidebar uses `runtime.audit_error`, which the start-up `ping` fills in |
| Reference names: `mcp_tools`, `build_graph(llm, tools, MemorySaver())`, `describe_llm(llm)` | `open_adapter()`, `build_graph(llm, tools)`, `describe_llm()` |
| The reference SOP tool took a `top_k` argument | The warm-up sends only `{"query": ...}` |

### Problems found while building

- **A renaming slip in a security script.** After switching to `_2` names, `setup_audit_log.sql` still denied the old
  `USR_FDE_RO` on the audit schema (that user exists in the database from the reference implementation, so the
  `IF EXISTS` was true) and the old `ai` schema. The rewrite's agent, `USR_FDE_RO_2`, had no explicit `DENY`. Fixed
  to the `_2` names. When a script names security principals, the self-check is what would have caught it.
- **`print` inside a tool never shows** (Phase 4): relevant here too, because audit failures must be reported
  through events and warnings, not prints.

## Accepted limits

| Limit | Why it is accepted for now |
| --- | --- |
| One user, no login, one chat | Removes a durable checkpointer, per-user MCP processes and concurrency design. The audit log keeps the history |
| Private data stored in full | The database is local and the data is the project's own sample data. Redaction only removes secrets |
| A failed audit write is lost | The agent carries on and shows a warning; there is no retry queue |
| Tamper-resistance stops at the database owner | Enough for a development system |
| No tests for `audit.py`, `turn.py` or `ui/` | `turn.py` and `audit.py` are built to be tested with a fake graph and a fake writer; the tests are not written yet |
| The UI was not written by hand | Adapted from the reference implementation on purpose; read it before changing it |
| No self-check in `setup_audit_log.sql` | Checked manually with `EXECUTE AS USER` instead |
| Tools return only `ok` or `error` | `unavailable` is mentioned in the prompt, but the weather and SOP tools do not return it yet |

## Python Concepts: Quick Reference

| Concept | Where used | Note |
| --- | --- | --- |
| Async generator (`async def` + `yield`, `async for`) | `turn.py` | Deliver events while the work is still running |
| `asyncio.to_thread` | `audit.record` | Run blocking code (`pyodbc`) without freezing the event loop |
| `nonlocal` | `log()` in `turn.py` | Lets an inner function change the outer function's variables |
| `@dataclass` | `TurnEvent` | A plain record type with defaults |
| `?` placeholders (parameterised SQL) | `audit.py` | Values travel separately from the SQL text |
| Default parameter values | `open_connection` | The agent's keys stay the default; the audit code passes its own |
| Threads and `queue.Queue` | `ui/runtime.py` | The page thread reads events produced in the runtime thread |
| `st.cache_resource` | `ui/app.py` | One shared object per server process, across page re-runs |

## FDE / DDD Concepts

| Concept | Where |
| --- | --- |
| Least privilege, separation of duties | Three logins: act, record, view |
| Audit trail as a security control | The agent cannot read or edit its own trail |
| Fail open, but loudly | `record()` returns `False`, the turn yields one `audit_warning` |
| Untrusted text in the log | Parameterised inserts and a column whitelist; secrets redacted by value |
| One code path | `run_turn()` is the only place a turn runs and is audited |
| Dependency direction | `ui/` depends on `agent/`, never the reverse; `resources/db.py` is shared |
| Orchestration vs delivery | The graph produces events; the CLI and the UI each decide how to show them |

## Run

```bash
make agent-db-setup     # once: agent user and view (3b)
make audit-db-setup     # once: audit schema, table and the two logins (needs envs/audit_sqlserver.env)
make agent              # terminal chat; every turn is audited
make ui                 # Streamlit chat and audit viewer on http://localhost:8501
```

`envs/audit_sqlserver.env` holds the writer and reader logins (`MSSQL_AUDIT_WRITER_USER` and `_PASSWORD`,
`MSSQL_AUDIT_READER_USER` and `_PASSWORD`); the server, driver and database come from `envs/agent_sqlserver.env`.
Read the log by hand with the reader login, or through the UI's Audit log tab. If the audit log is unavailable the
sidebar shows "Audit log: off" and each turn shows a warning. Recovery: run `make audit-db-setup` and check the env
file. Restart `make ui` after code changes (the file watcher is off). Needs `streamlit` and `pandas`
(`uv add streamlit pandas`).

## What's Next

The rewrite covers all five phases. Open items: tests for `src/agent/` and `src/ui/` (the graph, `turn.py` and
`audit.py` were built to be tested without live services), returning `unavailable` from the weather and SOP
tools, prompt caching (see the `TODO` in `graph.py`), a durable checkpointer, and a self-check in the audit
script.
