# Phase 5: Audit Log and Streamlit UI

**Status: complete.** Built as designed below; see [`phase5-notes.md`](phase5-notes.md) for what was built
and the small differences from this plan.

Phase 4 built the agent and left two items: a durable audit log and a UI. This phase builds both.
5a (audit log) comes first because it works from the terminal and can be tested without a browser. 5b
(Streamlit UI) then sits on top of the same turn loop.

| Step | Scope |
| --- | --- |
| 5a | Audit table, two new database logins, an audit writer hooked into the turn loop, a `make` target and tests |
| 5b | Streamlit chat with live tool-call cards, an audit viewer, and start-up warm-up |

## Decisions made

| Question | Decision | Why |
| --- | --- | --- |
| Who uses it? | **One user at a time, one chat.** Closing or clearing the chat ends it; there are no past chats to reopen | Removes a persistent checkpointer, per-user MCP processes, concurrency design and the blocking-tool risk. The audit log keeps the history, so nothing is lost |
| Chat memory | Existing `MemorySaver`; "New chat" starts a new `thread_id` | Already built. Lost on restart, which is fine for a chat that is not meant to be resumed |
| Two browser tabs | A process-wide lock: while one turn runs, another tab's turn is refused with a "busy" message | Streamlit cannot stop a second tab, and one shared MCP session should not be driven by two turns at once |
| Private data in the log | **Stored in full** (prompts, SQL, tool output, answers) | Accepted: the database is local and the data is the project's own sample data |
| Audit write fails | **Notify and carry on.** The agent keeps working; the UI shows a warning ("audit log unavailable, this turn was not recorded") and the CLI logs a warning | A broken log must not take the assistant down. The cost is a gap in the trail, so the warning is not optional |
| Tamper-resistance | **Accepted as is.** The writer login can only `INSERT`, so the agent and the app cannot change history. The database owner can | Enough for a development system. A client deployment would add hash chaining or write-once storage |
| Where the table lives | New schema `audit`, **not** `ai` | `ai` is the agent-facing, read-only layer and Phase 3b `DENY`s the agent's login `INSERT` there. A grant on a denied schema cannot work, and a write grant to the agent's own login would end the read-only guarantee |
| Who hooks the audit in | One shared function that runs a turn and yields events; both the CLI and the UI call it, and it writes the audit rows | One code path, so neither front end can skip the audit, and `graph.py` stays free of I/O |

## 5a: Audit log

### Identities (three logins, least privilege)

| Login | Can | Cannot | Used by |
| --- | --- | --- | --- |
| `USR_FDE_RO` (existing) | `SELECT` on `ai.VW_SUPPLY_CHAIN_READINGS` | Anything on `audit` (explicit `DENY`) | The agent's SQL tool |
| `USR_FDE_AUDIT_W` (new) | `INSERT` on `audit.AgentAuditLog` | `SELECT`, `UPDATE`, `DELETE`, `ALTER`, anything else | The audit writer |
| `USR_FDE_AUDIT_R` (new) | `SELECT` on `audit.AgentAuditLog` | Writes of any kind | The UI's audit viewer |

The agent can neither read nor edit its own trail, and the writer cannot read what it wrote. The
script `src/data/setup_audit_log.sql` follows the 3b pattern: idempotent, passwords as `sqlcmd`
variables from `envs/audit_sqlserver.env` (gitignored, with an `.example.env`), and a self-check that
impersonates each login and asserts what is allowed and refused. Run it with `make audit-db-setup`.

### Table `audit.AgentAuditLog`

One row per event, so a turn can be replayed in order.

| Column | Type | Meaning |
| --- | --- | --- |
| `LogId` | `BIGINT IDENTITY` primary key | Ordering and a stable reference |
| `LoggedAtUtc` | `DATETIME2(3)`, default `SYSUTCDATETIME()` | Set by the database, not the app |
| `SessionId` | `VARCHAR(64)` | The chat's `thread_id` |
| `TurnId` | `VARCHAR(64)` | One user question and everything it triggered |
| `StepNo` | `INT` | Order of events inside the turn |
| `Node` | `VARCHAR(20)` | `reasoner` or `tools` (the graph node that ran) |
| `EventType` | `VARCHAR(20)` | `user_prompt`, `llm_response`, `tool_call`, `tool_result`, `final_answer`, `error` |
| `ToolName` | `VARCHAR(100)` null | For tool events |
| `ToolCallId` | `VARCHAR(100)` null | Links a `tool_call` to its `tool_result` |
| `Status` | `VARCHAR(20)` null | `ok`, `error` or `unavailable` for tool results |
| `InputData` | `NVARCHAR(MAX)` null | The prompt, the tool arguments or the SQL |
| `OutputData` | `NVARCHAR(MAX)` null | The model reply or the tool result, in full |
| `ErrorMessage` | `NVARCHAR(MAX)` null | For errors, with secrets redacted |
| `Provider`, `Model` | `VARCHAR(50)` | `gemini`/`groq` and the model name |
| `DurationMs` | `INT` null | Time since the previous event |
| `PromptTokens`, `CompletionTokens` | `INT` null | From the model's usage data when it reports it |

Indexes on `(SessionId, LogId)` and `LoggedAtUtc`. There is no user column: with one local user and no
login there is nobody to record. The earlier plan's "Log ID, timestamp, session ID, node, tool name,
action type, input, output, errors" are all kept; `TurnId`, `ToolCallId`, `Status`, `Provider`, `Model`,
`DurationMs` and the token counts are added.

### Writer (`src/agent/audit.py`)

- `AuditLogger.record(event)` inserts one row through `pyodbc` as `USR_FDE_AUDIT_W`.
- It runs in `asyncio.to_thread`, so a database call never blocks the chat.
- It **never raises**. It returns `False` on failure, and the caller shows the warning. Pooling stays
  off, as in the SQL tool.
- Error text is redacted before storing: no API keys, no passwords.

### The shared turn loop

`ask()` in `cli.py` is refactored into a generator that runs one turn and yields typed events (prompt,
tool call, tool result, final answer, error), built from the same `graph.astream(stream_mode="updates")`
output used today. For each event it:

1. writes the audit row (and yields a warning event if that write fails), and
2. yields the event to the caller (the CLI prints it, the UI renders it).

Because the graph is unchanged, `graph.py` and its tests stay as they are.

### Tests (5a)

| Layer | Checks |
| --- | --- |
| Unit | Each event type maps to the right row. A failing writer returns `False` and does not raise. Redaction removes keys and passwords. The turn loop yields a warning event, and still the answer, when the write fails |
| Integration | The writer can `INSERT` and cannot `SELECT`, `UPDATE`, `DELETE` or `DROP`. The reader can `SELECT` and cannot `INSERT`. `USR_FDE_RO` is denied on `audit`. A scripted fake-LLM turn writes the expected rows in order |

## 5b: Streamlit UI

### Layout

`src/ui/app.py`, started with `make ui` (`streamlit run`, bound to `localhost` only because there is no login).

- **Chat:** `st.chat_message` and `st.chat_input`. The final answer renders as Markdown (the report
  tables and headings from the prompt).
- **Live tool calls:** each tool call appears as it happens, as a card (`st.status`) showing the tool
  name, its arguments (SQL shown formatted), a result preview and its status. Errors show in an error
  banner, and an audit failure shows the warning from 5a.
- **New chat** button: clears the chat and starts a new `thread_id`. The old chat stays in the audit log only.
- **Sidebar:** the active provider and model, connection status chips for the database, the vector
  index and the LLM, and the "New chat" button.
- **Audit tab:** a table read through `USR_FDE_AUDIT_R` with filters (date range, session, tool, status,
  event type) and text search over input and output. Rows expand to show the full data. It can show
  past chats, since the log persists after a chat is closed. This replaces the earlier "sidebar of
  active sessions", which is not needed with one chat.
- **"Beautiful" made concrete:** wide layout, a `.streamlit/config.toml` theme, status chips,
  collapsible tool cards, readable tables. No extra UI dependency.

### Running an async agent inside Streamlit

Streamlit re-runs the script on every interaction, but the agent is async and keeps one persistent MCP
session open. Decision: create **one background thread with its own event loop** once, with
`st.cache_resource`. It owns the MCP session, the graph and the `MemorySaver`. The UI submits each turn
with `asyncio.run_coroutine_threadsafe` and reads events from a queue to render them live. The
process-wide lock from the decisions table guards a turn.

### Warm-up and the Hugging Face model

The earlier plan said to cache a Hugging Face model to render the UI. A model cannot render a UI.
The real issue is the **SOP embedding model**, which loads inside the MCP server on the first SOP
search: in a live run that took about 37 seconds, and it contacted `huggingface.co` on every start
with an unauthenticated warning. Fix:

1. A `make warm-models` step downloads the model once.
2. The MCP server is started with `HF_HUB_OFFLINE=1`, so it uses the local copy and makes no network calls.
3. On UI start-up a background task sends one throwaway SOP query, so the model is already loaded when
   the user asks their first question. The UI shows "starting up" until it finishes.

### Tests (5b)

Streamlit's `AppTest` with a fake graph: a message renders a tool-call card and an answer; a failing
audit write shows the warning; "New chat" clears the chat and changes the `thread_id`; a second turn
while one is running is refused with the busy message.

## Run

```bash
make agent-db-setup && make audit-db-setup      # database logins and tables, once
make warm-models                                # download the embedding model once
make agent                                      # terminal chat, now also audited
make ui                                         # Streamlit chat on http://localhost:8501
make test                                       # offline tests
uv run pytest -m integration                    # live database permission checks and agent runs
```

## Known limits (deliberately not solved here)

- **Gaps in the trail.** Notify-and-carry-on means a failed audit write is lost, apart from the warning.
  A local spool file that is replayed later would close this gap.
- **Owner can edit.** The database owner can alter the table; INSERT-only only protects against the
  agent and the app.
- **No identity.** There is no login, so the log records sessions, not people.
- **One user.** Concurrent users, resumable chats and a persistent checkpointer are out of scope. The
  design above (one background loop, one MCP session, one lock) would have to change first.
- **Free-tier quotas** apply to every turn; the UI shows the quota message from Phase 4 but cannot raise the limit.
- **Private data at rest.** Full prompts and tool output are stored unencrypted in the local database.

## Differences between this plan and what was built

- `resources/db.py` was added: the connection helper moved out of the SQL tool so the audit code could share it.
- `ask()` in the CLI was kept as a thin wrapper over the new `run_turn()` (in `agent/turn.py`) so existing tests
  and the live tests did not change; `friendly_error` and `message_text` moved to `turn.py`.
- The MCP server is started with `HF_HUB_OFFLINE=1` automatically when the model is already cached, instead of
  always, so a first run on a new machine still works online.
- After a turn finishes the page calls `st.rerun()`, so the finished turn is drawn from history like older ones.
- Light and dark themes were added (not in the original plan), which meant switching the toolbar from "minimal" to
  "viewer" so the theme switch is reachable.
- The audit table also denies `REFERENCES` to both audit logins, and the script cleans up its own self-check row.
