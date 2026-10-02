# Phase 4 Notes: Agent Orchestration

**Status: Phase 4 is in progress.** 4a (the orchestrator) is built. 4b (audit log) and 4c
(Streamlit UI) are not. See [`phase-4-plan.md`](phase-4-plan.md) for the design.

## What We Built

A LangGraph ReAct agent: an LLM (Gemini by default, or Groq) reasons about each question, calls the Phase 3 tools through
the MCP server when it needs data, and answers in a terminal chat that remembers the
conversation.

```
question -> Reasoner (Gemini or Groq) <-> Tools (MCP: SQL | weather | SOP) -> cited answer
```

## File Structure

```
src/
├── data/                  # Phase 1, 3a, 3b (+ __init__.py)
├── resources/             # Phase 3c: the three tools and the MCP server
│   └── mcp_server.py
└── agent/                 # Phase 4a
    ├── llm.py             # Gemini or Groq by LLM_PROVIDER: temperature 0, max_retries 1, 60s timeout
    ├── prompts.py         # SYSTEM_PROMPT + build_system_prompt(today)
    ├── tools.py           # one persistent MCP stdio session -> LangChain tools
    ├── graph.py           # build_graph(llm, tools, checkpointer): pure wiring, no I/O
    └── cli.py             # python -m agent.cli [--verbose]
tests/
├── test_agent_graph.py    # routing, memory, step limit, prompt, error messages (fake LLM)
├── test_agent_config.py   # LLM settings
└── test_agent_live.py     # integration: real Gemini + real MCP server
```

## Topic Sections

### The ReAct graph

START connects to the Reasoner only. After every Reasoner step, a conditional edge asks one
question: did the model request a tool? If yes the flow goes to Tools, and Tools always return
to the Reasoner. If no it goes to END. (The first draft of the plan connected END to Tools,
which is wrong: END is reached from the Reasoner.)

| Piece | LangGraph building block |
| --- | --- |
| State | `MessagesState`: the message list, with the `add_messages` reducer (append, not overwrite) |
| Reasoner | An async function: system prompt + messages -> `llm.bind_tools(tools)` -> new `AIMessage` |
| Tools | `ToolNode(tools)` runs every tool call in the last `AIMessage` and returns `ToolMessage`s |
| Routing | `tools_condition`: `"tools"` or `END` |
| Memory | `compile(checkpointer=MemorySaver())` and `thread_id` in the run config |

The system prompt is added inside the Reasoner on each call and is never stored in state, so
the checkpointer does not save a copy of it every turn. `graph.py` takes the LLM and tools as
arguments and reads no files, which is what lets the tests use a scripted fake model.

### Reaching the tools through MCP

`tools.py` starts the Phase 3c server as a subprocess (`sys.executable -m resources.mcp_server`)
and keeps **one session open for the whole chat**. By default each tool call would start a new
server process, which would reload the embedding model on every SOP search. The adapter turns
each MCP tool into a LangChain tool; the model sees only the tool names, descriptions and
argument schemas, never the Python functions or the database.

### Choosing the LLM provider (Gemini or Groq)

`LLM_PROVIDER` in `envs/LLM.env` picks the provider (`gemini` by default); `--provider groq` on the
CLI overrides it for one run, and `make agent AGENT_ARGS="--provider groq --verbose"` passes it
through. `llm.py` returns a LangChain chat model either way, so the graph, tools and tests do not
change. It was added because Gemini's free tier (20 requests per day per model) blocked live testing.
Settings per provider: `GEMINI_API_KEY`/`GEMINI_MODEL`, `GROQ_API_KEY`/`GROQ_MODEL`.

Groq notes: `ChatGroq` turns `temperature=0` into `1e-8` (the Groq API rejects exactly 0). The model
names a provider offers change, so check them: `llama-3.1-8b-instant` returned `404 model_not_found`
on this account, and `GET https://api.groq.com/openai/v1/models` listed `openai/gpt-oss-120b`,
`openai/gpt-oss-20b` and `qwen/qwen3.8-27b` as chat models. `openai/gpt-oss-120b` handled all three
tools correctly (below). A model that cannot call tools will not work with this agent.

### Keeping requests small (free-tier token limits)

Free tiers limit tokens per minute and per day, so every request is kept lean. Measured in
approximate tokens (characters / 4):

| Part of a request | Before | After | How |
| --- | --- | --- | --- |
| System prompt + tool descriptions (sent every time) | ~1,600 | ~1,070 | Shorter prompt; column list and legacy-name glossary merged into one line per column (`iot_temperature (IOT_TEMP_C): ...`); terse tool descriptions |
| Conversation history | unbounded | ~3,000 | `window_messages()` in `graph.py` sends the current question in full plus as many recent older messages as fit. It starts on a human message so no tool result loses its tool call. Only what is *sent* is trimmed: the checkpointer still keeps the whole conversation |
| One SQL result | up to 200 rows | up to 50 rows and ~4,000 characters | `MAX_ROWS=50`, `MAX_RESULT_CHARS=4000`; `truncated` tells the model so it aggregates instead |
| Tool result format | pretty-printed JSON | compact JSON | `mcp_server.py` returns `compact(result)`; the default indentation cost tokens on every result |
| SOP search | 4 passages | 3 passages | default `top_k=3` |
| Answer length | unbounded | 2,048 tokens | `MAX_OUTPUT_TOKENS` on both providers (bounds reasoning too, for reasoning models) |

The schema context also stays under 2,600 characters (asserted by a test), and tests cover the window (current
tool loop never trimmed, never starts on an orphan tool message, memory keeps everything).

**Wasted-request guard.** In a live run the model copied legacy names from the SOP (`IOT_TEMP_C`,
`RISK_CLASS`) into its SQL, got an error from SQL Server and needed another model call to fix it, which
spends free-tier quota. Two fixes: the schema text now says only the business names are columns and
marks legacy names as `[SOP: ...]` (documents only), and `validate_sql()` rejects any legacy column name
locally with the exact correction (`IOT_TEMP_C is a legacy name ... Use iot_temperature`), so the
database is not called. The model still needs one retry in that case, but with a precise message.

### Deprecations

Our own code uses no deprecated API: `@asynccontextmanager` functions are annotated
`-> AsyncGenerator[...]` (not `AsyncIterator`, which is deprecated for this use), `create_react_agent`
is not used (the graph is built from `StateGraph`, `ToolNode` and `tools_condition`), and `mcp`'s
`FastMCP` is the supported API in the pinned 1.x. Two warnings remain, both from third-party packages
under Python 3.14, not from this repository: `google.genai` (`_UnionGenericAlias`) and `langsmith`
(`asyncio.iscoroutinefunction`). They disappear when those packages release a fix; check with
`uv run pytest -W default`.

### The system prompt

Written as part of the plan and kept in `prompts.py`. It tells the model which tool is for
what, to treat `error` as "fix your input and retry (twice at most)" and `unavailable` as "say
the source is down, never guess", to cite SOP sources, to translate legacy column names, to ask
a clarifying question when there is no shipment id, to separate today's date from the data's
end date (2024-08-29), and to refuse writes. A test asserts that each rule is present, so an
edit that drops one fails.

### Loop control and failures

`recursion_limit=12` per question (about five tool rounds) protects the free quota from a
looping agent. The CLI turns the three expected failures into plain messages: step limit
reached, quota or rate limit, and Gemini overloaded (HTTP 503). It never prints a traceback and
never ends the chat. The 3c tools never raise, so a failing tool is just another message the
model reads.

### Dependency upgrade (a problem found while building)

The plan said `langgraph` and `langchain-mcp-adapters` could be added to the existing lockfile.
That was wrong, for two reasons that only appeared when the code ran:

1. `langchain-mcp-adapters` 0.2+ needs `langchain-core` 1.x, and 0.1.x needs `mcp` 1.x. No
   version works with `langchain-core` 0.3 and `mcp` 2.x together.
2. The Gemini 3 family returns "thought signatures" on tool calls and rejects the next request
   if they are missing (`400: Function call is missing a thought_signature`). `langchain-google-genai`
   2.x drops them; 4.x keeps them.

So the LangChain family moved to 1.x (`langchain-core`, `text-splitters`, `huggingface`, `google-genai`
4.x, `pinecone`, `mcp-adapters` 0.3) and `mcp` was pinned back to `<2` (the server uses
`FastMCP` again). The Phase 1 and 3a ingestion dry runs, all offline tests and the live SQL and
SOP tests pass after the upgrade.

After that, all remaining dependencies were upgraded to their latest versions (`uv lock --upgrade`,
with the upper caps on `langgraph`, `sentence-transformers` and `pypdf` removed): `langgraph` 1.2,
`sentence-transformers` 6, `transformers` 5, `pypdf` 6, `pytest` 9, `pinecone` 7. Two packages are
held back by their dependents rather than by choice: `mcp` stays below 2 because
`langchain-mcp-adapters` requires it, and `pinecone` stops at 7 because `langchain-pinecone` does.
The same checks pass after this second upgrade (59 offline tests, both ingestion dry runs, live SQL
and SOP tests).

A smaller lesson: an earlier compatibility check used `uv run --with ...`, which builds a
throwaway environment, so it did not test the project's real dependency set. Verify against the
project environment.

## Python Concepts: Quick Reference

| Concept | Where used | What it's for |
| --- | --- | --- |
| `async def` / `await` / `asyncio.run` | whole `agent/` package | the MCP adapter tools are async only, so the graph is run with `ainvoke`/`astream` |
| `@asynccontextmanager` | `tools.py` `mcp_tools()` | opens the MCP session on entry and closes it on exit, even on error |
| `async for` | `cli.py` `ask()` | consumes the graph's stream of per-node updates |
| `asyncio.to_thread(input, ...)` | `cli.py` | reads a line from the terminal without blocking the event loop |
| `ExceptionGroup` | `error_text()` | async task groups wrap errors in a group; the helper unwraps it to read the message |
| Subclassing `BaseChatModel` | `tests/test_agent_graph.py` | a scripted fake LLM: the test decides each reply and records every input |

## FDE / DDD Concepts

- **ReAct (reason and act)**: the model alternates between thinking and calling tools until it
  can answer. The graph is the control loop; the model only chooses the next step.
- **State as the single source of truth**: the message list is the whole state, so memory,
  step limits and (later) the audit log attach to the graph.
- **Dependency direction**: `agent/` reaches `resources/` only through MCP; `resources/` knows
  nothing about the agent. `graph.py` has no I/O dependencies.
- **Interface as Contract**: the model's world is three tool descriptions. A good description
  is part of the interface, which is why `mcp_server.py` carries the schema and the rules.
- **Deterministic tests for probabilistic systems**: the routing, memory and step-limit
  behaviour is tested with a scripted fake model. Only the choice of tool by the real model
  needs live tests.
- **Evals**: the six-question table in the plan is the first eval set. Each question has an
  expected tool choice, checked by which tools were called, not by the wording of the answer.

## Verification status

| Check | Result |
| --- | --- |
| Offline tests (71) | Pass |
| Ingestion dry runs after the dependency upgrade | Pass |
| Live SQL and SOP tool tests | Pass |
| Live Gemini: model name and key | Passed once, before the upgrade |
| Live end to end, "how many readings above 4 degrees" | Passed: the agent called `query_telemetry_data` and answered 8,674 readings |
| Live end to end through Groq (`openai/gpt-oss-120b`), CLI | **Passed.** SQL question: called `query_telemetry_data`, answered 8,674. SOP question: called `search_compliance_sop`, answered with the 4.0 C threshold, the two dispatcher steps and a source-and-heading citation. Weather question: called `fetch_weather_conditions` and reported the values |
| Live end to end through Gemini: "delete all rows" | Passed: the agent refused and called no tools |
| Live end to end through Gemini: SOP and weather questions | **Not yet verified through Gemini.** Gemini first returned HTTP 503 "high demand", then HTTP 429: the free tier allows **20 requests per day per model** and the repeated live runs used them up (resets in about 14 hours). The tests skip on 503 and 429 instead of failing. Run `uv run pytest -m integration tests/test_agent_live.py -s` after the quota resets |
| CLI behaviour on a provider error | Verified live: with the quota exhausted it printed "Gemini quota or rate limit reached" and kept the chat open |

## Run

```bash
uv sync
make agent-db-setup && make sop-ingest      # data layer, once
make test                                   # 71 offline tests
make agent                                  # chat in the terminal
make agent AGENT_ARGS="--provider groq --verbose"   # pick the provider, show each tool call
uv run pytest -m integration                # live LLM (the chosen provider) + SQL Server + Pinecone
```

Set the key and model for your provider in `envs/LLM.env` (and `LLM_PROVIDER`), and start the SQL Server container
first (`make sqlserver-up`, or `docker start legacy-sqlserver` if it already exists).

## What's Next

- **4b, audit log**: `AgentAuditLog`, append-only, written by a second login that has `INSERT`
  and nothing else, so SQL Server enforces "append-only".
- **4c, Streamlit UI** over the same graph.
- **Quota planning.** A question that uses a tool costs at least two Gemini requests, so 20 a day is
  about ten questions. Run the live suite once per quota window, and use the scripted fake model
  for everything else. Moving to a paid tier or a second model removes this limit.
- Accepted limits until then: in-process memory (lost on restart), no message trimming for very
  long chats, and free-tier Gemini, which may use submitted content to improve Google's
  products. Fine for sample data; not for real client data.
