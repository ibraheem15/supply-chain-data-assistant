# Phase 6 Notes: Role-Based Department Assistants

**Status: Phase 6 is complete.** The agent can run as one of seven department assistants. Roles are prompts, not
permissions. The earlier phases' notes were left as they were when those phases were closed; this file is the record
of what Phase 6 added to the files of Phases 4 and 5.

## What We Built

The same agent, tools, data and audit log, but with a role chosen per chat. A role changes who the agent is, which
columns it focuses on, the evidence table in its answer and which SOP it quotes. The role travels in the per-call
config, so one compiled graph serves every role.

```
role + question -> run_turn() -> graph (reasoner builds the system prompt for the role) -> tools -> answer + audit row (with the role)
```

## File Structure

```
data/policy/
├── Cold_Chain_Incident_SOP_v2.md        # existing: cold chain, congestion diversion, risk triggers
├── Dispatch_Delay_SOP_v1.md             # new: delay escalation, rerouting, weather holds
├── Warehouse_Operations_SOP_v1.md       # new: dock time, equipment, inventory
├── Fuel_and_Cost_SOP_v1.md              # new: fuel inspection, cost approval
├── Supplier_and_Customs_SOP_v1.md       # new: supplier reliability, lead time, customs
└── Driver_Safety_SOP_v1.md              # new: fatigue, behaviour, reporting rules
src/
├── agent/
│   ├── prompts.py     # shared base prompt + ROLES table + role_titles() + build_system_prompt(role)
│   ├── graph.py       # reasoner reads the role from the per-call config
│   ├── turn.py        # run_turn(..., role) passes it on and records it
│   ├── audit.py       # "Role" is a writable and readable column
│   └── cli.py         # --role
├── data/setup_audit_log.sql   # Role column (and an idempotent ALTER for an older table)
└── ui/                # sidebar "Assistant" picker; the audit viewer shows the role
```

Existing files from Phases 4 and 5 were edited for this; no new Python module was needed.

## Topic Sections

### The roles

| Role id | Assistant | Focus columns | SOP |
| --- | --- | --- | --- |
| `cold_chain` (default) | Cold-Chain Analyst | cargo temperature, risk | Cold-Chain & Transit Anomalies |
| `dispatch` | Route & Delay Analyst | `eta_variation_hours`, `traffic_congestion_level`, `delivery_time_deviation`, `delay_probability` | Dispatch, Delays & Rerouting |
| `warehouse` | Warehouse Operations | `warehouse_inventory_level`, `loading_unloading_time`, `handling_equipment_availability` | Warehouse Operations |
| `cost` | Cost & Fuel Analyst | `fuel_consumption_rate`, `shipping_costs` | Fuel & Shipping Cost |
| `supplier` | Supplier & Customs Analyst | `supplier_reliability_score`, `lead_time_days`, `customs_clearance_time`, `port_congestion_level` | Supplier Reliability & Customs |
| `safety` | Driver Safety Analyst | `driver_behavior_score`, `fatigue_monitoring_score` | Driver Safety & Fatigue |
| `risk` | Risk & Compliance | `risk_classification`, `route_risk_level`, `disruption_likelihood_score`, `delay_probability` | Cold-Chain & Transit Anomalies (risk triggers) |

### One prompt template, many roles (`prompts.py`)

The Phase 4 prompt was split into a shared base (tools, data rules, output format) with placeholders, and a `ROLES`
dictionary. Each role has a `title` (shown to the user), an `intro` (who the agent is), `rules` (what to focus on and
which SOP topic to quote) and `evidence` (the columns of the answer's evidence table). `build_system_prompt(role)`
fills the placeholders with `.replace` (not `.format`, because the prompt contains other braces) and raises
`ValueError` for an unknown role. The default role reproduces the Phase 4 behaviour; its temperature rule moved from
the base into the role. The Phase 4 output table (temperature, cargo risk) became a generated "Evidence" table, so
each role shows its own columns.

### The role travels in the config, not in the state

LangGraph passes a per-call `config` to a node that declares a `config: RunnableConfig` parameter. `run_turn` puts
`role` into `config["configurable"]` next to `thread_id`, and the reasoner reads it with
`config.get("configurable", {}).get("role", DEFAULT_ROLE)`. Consequences:

- One compiled graph serves every role; nothing is rebuilt when the role changes.
- The role is not saved in the state, so the memory still holds only the conversation, and the system prompt is
  still never stored (the Phase 4 rule).
- A saved conversation belongs to the role it started with, so the UI locks the picker once a chat has messages and
  "New chat" unlocks it. The CLI fixes the role for the whole run.

### The audit log records the role

`audit_2.AgentAuditLog` gained a nullable `Role VARCHAR(50)` column. `run_turn` adds `"Role"` to every event it logs,
`COLUMNS` in `audit.py` lists it (the writer's whitelist) and `AuditReader.search` returns it, so the audit tab can
show which assistant produced an answer. `setup_audit_log.sql` creates the column for a new table and adds it to an
existing one with `IF COL_LENGTH(...) IS NULL ... ALTER TABLE ... ADD`, so re-running `make audit-db-setup` is safe.
**The script must be run before using Phase 6**: the writer inserts `Role`, and without the column every audit
write fails and the turn shows the audit warning.

### The sample SOPs

Each new SOP follows the style of the existing one: a version header, `###` headings (which the Phase 3a chunker
splits on) and thresholds written with the **legacy column names** (`ETA_VAR_HR`, `WH_INV_LVL`, `FUEL_RT_LPH`,
`SUPL_REL_SCR`, `FTG_MON_SCR`), so the agent has to translate them through the schema glossary, as before. The
thresholds are invented for demonstration, chosen inside the real ranges of the data (checked against the CSV), and
each document says it is a sample. The Driver Safety SOP states that the data has no driver identity and that reviews
must not name or blame a person, and the safety role's prompt repeats it.

### Problems and limits

| Item | Note |
| --- | --- |
| A role is a prompt, not a permission | Every role reads the same view and the same SOP index. Per-team access would need one view and one login per role (the Phase 3b pattern) |
| The model may drift | A role only asks the model to focus on certain columns and SOPs; whether it follows is checked by asking questions, not guaranteed |
| Sample thresholds | The numbers in the new SOPs are illustrative |
| Prompt cost | Each role's prompt is about 460 to 480 words, sent on every LLM call, so the cost per call is about the same as Phase 4's |
| No tests | The prompt builder and the role routing are easy to test offline; the tests are not written |
| Role switch needs a new chat | The conversation belongs to one role, so the UI locks the picker mid-chat |

## Python Concepts: Quick Reference

| Concept | Where used | Note |
| --- | --- | --- |
| Dictionary of dictionaries as a lookup table | `ROLES` | One entry per role; adding a role is adding an entry |
| `argparse` with `choices` | `cli.py` | Validates `--role` and builds the `--help` text |
| A node parameter `config: RunnableConfig` | `graph.py` | LangGraph passes the per-call config to the node |
| `dict.get(key, default)` chained | `graph.py` | Safe lookup of `role` with a default |
| `st.selectbox(..., key=..., disabled=...)` | `ui/app.py` | A widget whose value lives in `st.session_state` |

## FDE / DDD Concepts

| Concept | Where |
| --- | --- |
| One platform, many assistants | A shared data layer, tools and audit trail with role-specific prompts and policies on top |
| Configuration over code | A new assistant is a `ROLES` entry plus an SOP document, with no new code path |
| Roles are not permissions | The prompt shapes behaviour; the database enforces access |
| Policy as retrievable documents | Each team's rules live in its own SOP file, found by semantic search, not hard-coded in the prompt |
| Audit includes context | Each audit row records which assistant answered |

## Run

```bash
make audit-db-setup    # once: adds the Role column to the audit table
make sop-ingest        # once, and after editing a SOP: syncs data/policy/ to Pinecone
make agent AGENT_ARGS="--role dispatch"
make ui                # pick the assistant in the sidebar when starting a chat
```

Roles: `cold_chain` (default), `dispatch`, `warehouse`, `cost`, `supplier`, `safety`, `risk`.

## What's Next

Possible extensions: an **audit reviewer** assistant that reads the audit log instead of the telemetry (it needs a
tool over the select-only login), one database view and login per role for real per-team access, tests for the prompt
builder and the role routing with a fake model, and `unavailable` statuses from the weather and SOP tools.
