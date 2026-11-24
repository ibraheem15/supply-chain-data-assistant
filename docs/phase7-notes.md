# Phase 7 Notes: One Project, Many Domains

**Status: Phase 7 is complete.** Everything that is specific to the logistics use case moved out of Python into a
per-domain folder, `domains/<name>/`, and the `DOMAIN` environment variable picks the folder. A second domain
(`retail`) was added with configuration files, a CSV and two commands, and no change to the agent code. The notes of
Phases 1 to 6 were left as they were when those phases were closed; this file is the record of what Phase 7 changed
in their files.

## What We Built

The same agent, tools, graph, audit log and UI, but the facts about the data (table, columns, roles, prompt wording)
are read from a domain folder instead of being written in the code.

```
DOMAIN env var -> load_domain() -> domains/<name>/*.toml -> Domain object -> loader, schema text, prompts, tool descriptions
```

## File Structure

```
domains/
├── logistics/                   # the original use case, moved out of the code
│   ├── domain.toml              # name, table, view, csv, policy_dir
│   ├── columns.toml             # 26 columns: clean name, legacy name, optional SQL type
│   ├── roles.toml               # default role + 7 roles (title, intro, rules, evidence)
│   └── prompt.toml              # tool descriptions, tool list, data rules, cause examples
└── retail/                      # a second domain that proves the design (5 columns, 2 roles)
    └── (same four files)
src/
├── data/
│   ├── domain.py                # NEW: load_domain(), Domain, Column, Role, available_domains()
│   ├── make_view_sql.py         # NEW: generates the view and grant SQL from columns.toml
│   ├── ingest_supply_chain_dataset.py   # table, csv and columns come from the domain
│   └── ingest_sop_pinecone.py   # SOP folder comes from the domain
├── resources/
│   ├── schema_context.py        # view and columns come from the domain
│   └── mcp_server.py            # tool descriptions are built from the domain
└── agent/
    ├── prompts.py               # roles and the domain wording come from the domain
    ├── tools.py                 # passes DOMAIN to the MCP server process
    └── cli.py                   # description uses the domain name
Makefile                         # ingest and sop-ingest run as modules; new view-sql and view-setup
data/raw/retail_sales.csv        # synthetic retail data (gitignored, like the logistics CSV)
```

## Topic Sections

### The domain folder

| File | What it holds | Read by |
| --- | --- | --- |
| `domain.toml` | `name`, `table` (`dbo.retail_sales`), `view` (`ai_2.VW_RETAIL_SALES`), `csv`, `policy_dir` | loader, SOP ingest, tool server |
| `columns.toml` | one line per column, in CSV order: clean `name`, `legacy` name, optional `sql_type` (default `FLOAT`) | loader, schema text, view generator |
| `roles.toml` | `default` role and one `[roles.<id>]` table per role: `title`, `intro`, `rules`, `evidence` | `prompts.py`, the CLI and the UI role list |
| `prompt.toml` | the wording that depends on the data: `data_summary`, `sql_topics`, `sop_topics` (tool descriptions), `tool_lines` (the numbered tool list), `cause_examples`, `data_rules` | `prompts.py`, `mcp_server.py` |

`load_domain()` in `src/data/domain.py` reads the folder named by `DOMAIN` (default `logistics`) and returns one
frozen `Domain` object. A missing folder or file raises a `ValueError` that names it. `domain.py` lives in
`src/data/`, the lowest layer, so `resources/` and `agent/` import it and it never imports them.

### How it was done safely

Each step followed the same pattern: move the data into a file, prove it identical to the old code, and only then
switch the code to read the file.

| Step | Proof |
| --- | --- |
| Columns and table names | The values from `columns.toml` compared equal to the old constants |
| Roles | The loaded roles compared equal to the old `ROLES` dict |
| Prompt wording | The system prompt of all 7 roles was saved before the change and compared after: byte-identical |
| View SQL | The 26 `[LEGACY] AS [name]` pairs generated for logistics are identical to the hand-written view script |

### The second domain proves the design

`retail` has 5 columns (`timestamp`, `store_id`, `units_sold`, `stock_level`, `unit_price`), two roles (`inventory`
as the default, `pricing`) and its own data rules (no customer or order id, filter by `store_id`). Running it
offline first found three leaks of logistics wording that the first pass missed: the weather tool line in the
prompt, the cause list "equipment, weather, traffic", and the CLI description. All three moved into the domain
folder (`tool_lines`, `cause_examples`, `domain.name`) and the logistics prompts stayed byte-identical.

The first real run found a bug that offline checks could not: the agent answered "no store-specific data" and made no
tool call. The cause was in `tools.py`. The MCP server is a separate process started over stdio with an explicit
`env={"PYTHONPATH": "src"}`, and a child process started that way gets only the variables listed there plus a few
basics. It did not get `DOMAIN`, so the server fell back to `logistics` and described logistics tools next to a retail
prompt. The fix is one line: `env={"PYTHONPATH": "src", "DOMAIN": load_domain().name}`. Every process that reads the
domain must see the same `DOMAIN`.

### Adding a domain: what you need besides the TOML files

The four files make the code switch domains, but the agent can only answer if the data exists where the config says
it is. The full list:

| # | What | Where it must exist | How it is created |
| --- | --- | --- | --- |
| 1 | The four config files | `domains/<name>/` | written by hand |
| 2 | The source data | the CSV path in `domain.toml`; its header must equal the names in `columns.toml` | you supply it |
| 3 | The table | SQL Server, `dbo.<table>`, with the legacy column names | `DOMAIN=<name> make ingest` |
| 4 | The agent login | SQL Server | `make agent-db-setup`, once for the whole project |
| 5 | The view and the grant | SQL Server, the `view` named in `domain.toml` | `DOMAIN=<name> make view-setup` |
| 6 | SOP documents (optional) | the `policy_dir` folder, then Pinecone | `DOMAIN=<name> make sop-ingest` (see the limits below) |
| 7 | `DOMAIN=<name>` in the environment | the shell that starts the agent or the UI | on the command line |

The data flows through the same path as logistics:

```
CSV -> dbo.<table> (legacy names) -> <view> (clean names, agent login can read only this) -> query_telemetry_data -> agent
```

### Why a generated view

The Phase 3b view script listed the 26 logistics columns by hand. `make_view_sql.py` writes the same SQL from
`columns.toml`: one `[LEGACY] AS [clean_name]` line per column, a `GRANT SELECT` on that one view to the agent
login, and a test that the agent can read the view but not the raw table. The login name comes from
`MSSQL_AGENT_USER` in `envs/agent_sqlserver.env`, so nothing is hard-coded. `make view-sql` only prints the SQL, so it
can be read before `make view-setup` runs it as `sa`.

### Problems and limits

| Item | Note |
| --- | --- |
| SOP index is shared | `sync_sop_files()` keeps one cache file (`.cache/sop_ingest_cache.json`) and one Pinecone index for the whole project. Running `sop-ingest` with a different `DOMAIN` sees the other domain's SOPs as deleted and **removes their chunks from the index**. The retail run skipped SOPs for this reason. A per-domain cache and index name (or namespace) is needed first |
| The weather tool is not per domain | The prompt of a domain lists only its own tools, but the weather tool is still registered on the MCP server, so a model could still call it. A `tools` list in `domain.toml` would fix this |
| The loader is logistics-shaped | `parse_csv` parses every column except `timestamp` as a float and checks `risk_classification` against the logistics risk levels. Retail works because it has no such column and `store_id` converts into its `INT` column. A domain with text columns would need a per-column type in the parser |
| The hand-written view script remains | `setup_security_and_view.sql` still creates the logistics view and login by hand; `make_view_sql.py` only generates the view and grant, and expects the login to exist |
| One audit table | The audit log has a `Role` column but no domain column, so rows from different domains share one table |
| The SQL tool name | The tool is still called `query_telemetry_data` and the MCP server `supply-chain-tools`, for every domain |
| Retail data is synthetic | 1,008 rows (3 stores, hourly, 2024-06-17 to 2024-06-30) generated for the test; the file is gitignored |
| The UI was not run with `DOMAIN=retail` | The UI starts the tool server through the same `tools.py`, so it should work, but only the CLI was run |
| No tests | The domain loader and the prompt builder are easy to test offline; the tests are not written |

## Python Concepts: Quick Reference

| Concept | Where used | Note |
| --- | --- | --- |
| `tomllib` (standard library) | `domain.py` | Reads TOML; the file must be opened in binary mode (`"rb"`) |
| Frozen dataclass | `Domain`, `Column`, `Role` | An immutable record; a class must be defined above the class that uses it in a field type |
| `@property` | `Domain.column_names`, `legacy_names`, `sql_types` | Derived values that read like fields |
| `os.environ.get(name, default)` | `load_domain()` | Reads `DOMAIN` with a fallback |
| `Path / "name"` | `domain.py` | Joins paths with `/` |
| Dict and tuple comprehensions | `domain.py` | Build `columns` and `roles` from the TOML tables |
| `str.split(".", 1)` | `make_view_sql.py` | Splits `schema.name` into two parts |
| f-strings that generate text | `make_view_sql.py` | Build SQL from data (safe here: the names come from our own config, not from users) |
| Environment of a child process | `tools.py` | A process started with an explicit `env` does not inherit the parent's variables |

## FDE / DDD Concepts

| Concept | Where |
| --- | --- |
| Configuration over code | A new use case is a new folder, a CSV and two commands, with no change to the agent |
| Move, prove, switch | Data was moved to files, shown identical to the old constants, and only then did the code read the files |
| Layering | `domain.py` is in the lowest package (`data/`); upper layers import it, never the reverse |
| The second use case is the real test | Only running `retail` found the leaked wording and the `DOMAIN` bug |
| A separate process has a separate environment | The MCP server is a child process; configuration it needs must be passed to it |
| Security by construction | The generated view and grant give the agent the same read-only access as logistics, and the script tests it |

## Run

Logistics (the default, nothing changes):

```bash
make agent AGENT_ARGS="--role dispatch"
make ui
```

Add and run a new domain (here `retail`):

```bash
# 1. create domains/retail/{domain,columns,roles,prompt}.toml and put the CSV at the path in domain.toml
# 2. check the config offline (no database, no LLM)
DOMAIN=retail PYTHONPATH=src uv run python -c "from data.domain import available_domains, load_domain; print(available_domains()); d = load_domain(); print(d.name, d.default_role, list(d.roles), d.column_names)"
DOMAIN=retail PYTHONPATH=src uv run python -c "from agent.prompts import build_system_prompt; print(build_system_prompt())"
# 3. create the table and load the CSV (creates and fills only dbo.<table> of this domain)
DOMAIN=retail make ingest
# 4. once per project: the agent login (already done for logistics)
make agent-db-setup
# 5. read the generated view SQL, then create the view and the grant
DOMAIN=retail make view-sql
DOMAIN=retail make view-setup          # the last line must say: PASS  raw table SELECT denied
# 6. chat
DOMAIN=retail make agent
DOMAIN=retail make agent AGENT_ARGS="--role pricing"
DOMAIN=retail make ui
```

Sample questions for `retail`: "What was the latest stock level for store 3?" (126), "Which store sold the most units
overall?" (store 1, 3,888 units), and "Show me the active orders for customer 42." (the data has no such ids, so the
agent should say so).

## What's Next

Possible extensions: a per-domain SOP cache and Pinecone index (or namespace) so SOPs of several domains can be
indexed side by side, a `tools` list in `domain.toml` so a domain can drop the weather tool, per-column types in the
CSV parser, a domain column in the audit log, a domain picker in the UI, and tests for the domain loader and the
prompt builder.
