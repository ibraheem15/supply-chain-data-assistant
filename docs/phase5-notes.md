# Phase 5 Notes: Audit Log and Streamlit UI

**Status: Phase 5 is complete.** 5a (the audit log) and 5b (the Streamlit UI) are built. See
[`phase5-plan.md`](phase5-plan.md) for the design and the decisions behind it.

## What We Built

An append-only audit trail of everything the agent does, written by a login that can only `INSERT`, and a
single-chat Streamlit UI that shows each tool call live and lets you search the trail.

```
question -> run_turn() -> graph (Reasoner <-> Tools) -> events -> UI cards / CLI lines
                      \\-> audit.AgentAuditLog (insert-only login) <- audit viewer (read-only login)
```

## File Structure

```
src/
├── data/
│   └── setup_audit_log.sql        # schema [audit], table, three-login permissions, self-check
├── resources/
│   └── db.py                      # one shared SQL Server connection helper (was inside sql_tool)
├── agent/
│   ├── audit.py                   # AuditEvent, AuditLogger (writer), AuditReader, redaction
│   ├── turn.py                    # run_turn(): one turn as a stream of TurnEvents, audited
│   ├── warm.py                    # python -m agent.warm: download the embedding model once
│   ├── cli.py                     # now a thin front end over run_turn()
│   └── tools.py                   # starts the MCP server offline when the model is cached
└── ui/
    ├── runtime.py                 # AgentRuntime: background thread, event loop, MCP session, turn lock
    └── app.py                     # the Streamlit page: chat, tool cards, sidebar, audit tab
envs/audit_sqlserver.env           # writer and reader passwords (gitignored; .example.env committed)
.streamlit/config.toml             # light and dark themes, toolbar, file watcher
tests/test_audit.py, tests/test_ui.py
```

## Topic Sections

### Three identities, one table

| Login | Can | Cannot |
| --- | --- | --- |
| `USR_FDE_RO` (agent, from 3b) | read the data view | anything on schema `audit` (explicit `DENY`) |
| `USR_FDE_AUDIT_W` (writer) | `INSERT` into `audit.AgentAuditLog` | `SELECT`, `UPDATE`, `DELETE`, anything else |
| `USR_FDE_AUDIT_R` (reader, UI) | `SELECT` from the table | writes |

The table lives in its own schema `audit`, not `ai`: `ai` is the agent-facing layer where the agent's
login is denied `INSERT`, and giving that login write access would end the read-only guarantee. The
script `setup_audit_log.sql` ends by impersonating each login and asserting ten allowed and refused
actions; `make audit-db-setup` prints ten `PASS` lines. The database owner can still edit the table
(accepted).

### One row per event

`AuditEvent` has one field per column: session, turn, step, node, event type, tool name, tool-call id,
status, input, output, error, provider, model, duration and token counts. A turn is replayed by ordering
on `LogId`; a `tool_call` and its `tool_result` share a `ToolCallId`. Prompts, SQL and tool output are
stored in full (accepted: local database, sample data). Secrets are redacted before storing: every API
key or password found in the env files is replaced with `***`.

### Notify and carry on

`AuditLogger` never raises. A failed write returns a message, the turn loop yields one `audit_warning`
event for that turn, and the agent keeps working; the CLI prints `[warning] ...` and the UI shows a
warning banner. The cost is a gap in the trail, so the warning is not optional.

### One shared turn loop

`run_turn()` in `turn.py` runs one question through the graph and yields `TurnEvent`s (tool call, tool
result, answer, error, audit warning). It also writes the audit rows. The CLI and the UI both consume
it, so neither can skip the audit and `graph.py` stays free of I/O. `ask()` in the CLI is now a thin
wrapper that prints verbose lines. Errors inside a turn become an `error` event and an audit row, never
an exception.

### Streamlit and an async agent

Streamlit re-runs the whole page script on every click, but the agent is async and keeps one persistent
MCP session. `AgentRuntime` therefore owns **one background thread with its own event loop**, started
once through `st.cache_resource`. The page submits a turn with `asyncio.run_coroutine_threadsafe` and
reads events from a `queue.Queue`. A `threading.Lock` allows one turn at a time: a second browser tab
gets a "busy" message. After a turn the page calls `st.rerun()`, so the finished turn is drawn from
history exactly like older turns.

### The UI

A chat with a status box that shows each tool call as it happens (SQL shown as SQL), tool cards
(expanders with arguments, a result preview, status and timing) in the history, and error and
audit-warning banners. The sidebar has "New chat" (a new `thread_id`; the old chat is gone from the
UI and kept only in the audit log), and status dots for the agent, tools, the SOP model and the audit
log. The **Audit log** tab filters by chat, tool, event, status, dates and free text, and opens any
entry in full. It reads past chats too, because the log persists.

### Light and dark themes

`.streamlit/config.toml` defines two complete palettes, `[theme.light]` and `[theme.dark]`, and Streamlit
follows the operating system's setting by default. The viewer can switch at any time from the menu at the top
right (Settings, Theme: light, dark or system); the sidebar says so. The page uses only Streamlit's own colours
(`:green[●]` status dots, expanders, code blocks, tables), so both themes apply everywhere with no custom CSS.

The first config hid the whole menu (`toolbarMode = "minimal"`), and the theme switch lives in that menu, so
dark mode would not have been reachable. It is now `"viewer"`: the menu stays, developer options (rerun, clear
cache, deploy) stay hidden.

| Role | Light | Dark |
| --- | --- | --- |
| Background | `#FFFFFF` | `#0E1117` |
| Secondary background (sidebar, cards) | `#F2F5F9` | `#1A1F2B` |
| Text | `#1F2933` | `#E6EAF0` |
| Accent and links | `#1B4D89` | `#7AB3F0` / `#8FC1FF` |
| Code background | `#EEF2F7` | `#151A24` |

Tests compute the WCAG contrast ratio of every text and background pair in both themes and require at least
4.5:1 for text and links and 3:1 for the accent. **Not verified visually:** a headless Firefox screenshot came
back blank (it captures the page before Streamlit's JavaScript renders), so the themes have been checked by
numbers and by Streamlit accepting the config, not by eye. Look at both once in a browser.

### Start-up: warm-up and the offline embedding model

The SOP embedding model loads inside the MCP server. In a live CLI run the first SOP search waited about
37 seconds and contacted `huggingface.co` on every start. Fixes: `make warm-models` downloads the model
once; `tools.server_env()` then starts the MCP server with `HF_HUB_OFFLINE=1` whenever the model is in the
Hugging Face cache; and the UI sends one throwaway SOP query at start-up, so the model is loaded before the
first question. In the live check the first real SOP call took 0.4 seconds.

### Refactor: one connection helper

`resources/db.py` now holds the ODBC driver choice, connection string and the pooling switch. The SQL
tool and the audit code both use it, so the credential-quoting logic exists once.

### Problems found in the first real browser run

The first run in a real browser (the offline tests and my headless runs had passed) exposed four problems.
The audit log itself found the worst one.

| Symptom | Cause | Fix |
| --- | --- | --- |
| After all tools ran, the answer was "(The model returned no answer.)" | The Phase 4 output cap (`MAX_OUTPUT_TOKENS=2048`) was too small for a reasoning model (`gpt-oss-20b`): its hidden reasoning counts toward the cap, so the whole budget went on thinking. The audit row showed `CompletionTokens = 2048`, exactly the cap, with 31 characters of output | Cap raised to 4096; `reasoning_effort="low"` for the `gpt-oss` models (Groq rejects the parameter for other models); and an empty answer that stopped at `finish_reason=length` now reports "reached its output limit" with the token count, as an error event and an `error` audit row, not a bland "no answer" |
| Hundreds of `ModuleNotFoundError: torchvision` tracebacks, and a slow start | `agent.tools` imported the ingestion module to read a model name; that loaded `transformers` into the Streamlit process, and Streamlit's file watcher then imported every lazy `transformers` submodule | The cache-folder name is spelled out in `tools.py` (a test checks it matches the ingestion constant); a regression test runs a subprocess and asserts `transformers` is not imported; and `.streamlit/config.toml` sets `fileWatcherType = "none"` (the cost: restart `make ui` after code changes) |
| "`use_container_width` will be removed" warning on every re-run | Deprecated Streamlit parameter on the New chat button and the audit table | Replaced with `width="stretch"`; a test fails if it comes back |
| "Final answer not saved in the audit log" | The rows are saved, in full (checked directly in SQL Server and through the reader the UI uses). The one that looked wrong was the earlier empty-answer turn, which stored its 31-character placeholder. The audit table also showed no output column, so an answer was only visible after opening an entry | The empty-answer case is fixed (above) and the table now has a one-line `Preview` column showing the answer, tool result, prompt or error |

Lesson: a headless test proves the wiring, not the experience. A real browser, a real model and a
reasoning model each found a problem that a fake could not. The audit log earned its place by pointing at
the cause (`CompletionTokens` equal to the cap) within one query.

## Python Concepts: Quick Reference

| Concept | Where used | What it's for |
| --- | --- | --- |
| `dataclasses.fields()` | `audit.py` | builds the SQL column list from the dataclass fields, so event and table cannot drift |
| async generator (`async def` + `yield`) | `turn.py` `run_turn` | streams events to the caller as the graph produces them |
| `nonlocal` | `turn.py` | lets a nested function change a counter in the enclosing function |
| `asyncio.to_thread` | `audit.py` | runs the blocking database insert without freezing the event loop |
| `threading.Thread`, `Lock`, `queue.Queue` | `ui/runtime.py` | a background thread, a one-turn-at-a-time lock, and a thread-safe hand-off of events |
| `asyncio.run_coroutine_threadsafe` | `ui/runtime.py` | schedules a coroutine on another thread's event loop |
| walrus `:=` | `ui/app.py` | assigns and tests in one expression: `while (event := events.get()) is not None` |
| `@st.cache_resource` | `ui/app.py` | one shared object per server process, surviving Streamlit's script re-runs |
| `AppTest` (Streamlit) | `tests/test_ui.py` | runs the page headlessly and inspects widgets |

## FDE / DDD Concepts

- **Least privilege, by separation of duties**: the identity that acts, the one that records and the one
  that reads are three different logins. No single credential can both do something and erase the trace.
- **Fail open, but loudly**: a broken audit log must not take the assistant down, so the agent carries on
  and the failure is shown. The alternative (fail closed) is the right choice when the log is a legal
  requirement.
- **One code path**: a single turn loop feeds both front ends, so a guarantee (every step is audited) holds
  for all of them.
- **Dependency direction**: `ui/` depends on `agent/`; `agent/` does not know the UI exists.
- **Observability**: the audit table is the durable form of the `--verbose` view, with timings and token
  counts for cost tracking.

## Verification

| Check | Result |
| --- | --- |
| Offline tests | 110 pass (writer, reader, redaction, failure handling, the turn loop, the UI through `AppTest`, the runtime lock) |
| `make audit-db-setup` self-check | 10 `PASS` lines |
| Live permission test | Writer cannot `SELECT`, `UPDATE` or `DELETE`; reader cannot `DELETE`; the agent login cannot read the log |
| Real turn through the real runtime and page (Groq `gpt-oss-120b`) | Agent ready, 3 tools, SOP model warm, audit on; the SOP question produced a tool card (0.4 s) and the cold-breach answer; 4 audit rows with provider, model, durations and token counts |
| The question that failed in the browser, re-run on the user's model (`gpt-oss-20b`) | Three tool cards (database, weather, SOP), no errors or warnings, a full answer, and the answer stored in the audit log (1,273 characters, ending with the SOP citation) |
| Themes | Both load without Streamlit config warnings; text, link and accent contrast meet the thresholds in both |
| `make ui` | Streamlit starts, health and page return HTTP 200, listening on localhost only |

The live checks use LLM quota, so they are marked `integration` and skipped by default.

## Run

```bash
make agent-db-setup && make audit-db-setup   # database logins and tables, once
make warm-models                             # download the embedding model once
make agent                                   # terminal chat, now audited (--no-audit to skip)
make ui                                      # Streamlit chat on http://localhost:8501
make test                                    # offline tests
uv run pytest -m integration                 # live checks (database, LLM, UI)
```

Copy `envs/audit_sqlserver.example.env` to `envs/audit_sqlserver.env` and set both passwords first.

## What's Next

Nothing in the original design is left unbuilt. Known limits: a failed audit write is lost apart from the
warning (a local spool file that is replayed later would close the gap); the database owner can edit the
table; there is no login, so the log records chats, not people; one user at a time; free-tier quotas apply to
every turn. If the tool is ever used by more than one person, the first changes are a persistent
checkpointer, per-user sessions and an authenticated viewer.
