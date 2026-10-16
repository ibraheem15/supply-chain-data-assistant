# Phase 2 Notes: Solution Exploration

## What We Built

No code this phase — Phase 2 was a design exercise. It produced a business case, a
High-Level Design, and a Low-Level Design for a conversational agent over the
structured logistics data and the unstructured SOP documents.

```
User question → Agent decides: structured or unstructured? → text2sql/MCP (DB) or RAG (SOPs) → grounded, audited answer
```

## File Structure

```
docs/
├── problem_statement.md          # Original problem framing; open questions this phase answers
├── research.md                   # Working notes: the decisions and why, in diagram form
├── technical-design-doc.pdf     # The TDD: business context, scope, HLD, LLD, governance
├── business-pitch.html           # Stakeholder-facing pitch deck version of the same decisions
├── business-pitch-print.html
├── business-pitch.pdf
├── domain-model.md               # Current-code diagram (Phase 1 only — unaffected by this phase)
├── domain-model.html
└── phase2-notes.md               # This file
data/
└── policy/
    └── Cold_Chain_Incident_SOP_v2.md   # Sample SOP used to design the RAG/unstructured path
```

## Topic Sections

### The core architectural decision: is the data structured?

Phase 2 started from a single question ([`research.md`](research.md)): can the data be
handed to the model directly, or does it need to be looked up? The 32k-row logistics
table is too large to fit in a prompt, so it's queried like a database, not read
end-to-end — the **structured path**. The SOP documents are few and short enough to
search by meaning — the **unstructured path**. The solution needs both, not one or the
other; the agent's job is choosing which path a given question needs.

### Structured path: text2sql via MCP

One tool to start: a **text2sql** tool that translates the user's natural-language
question into SQL, runs it, and hands the result back. **MCP (Model Context
Protocol)** is the mechanism for exposing that tool to the agent as a small, controlled
surface, instead of open-ended database access.

- **Trade-off accepted:** a generated query can be wrong. Mitigated with a multi-turn
  loop — the agent sees both the query it ran and the result, and can revise the query
  if the result looks off, rather than trusting the first attempt.
- **Deferred to Phase 3:** the query-validation/correction loop is a design intent
  here, not yet built or tested against real queries.

### Unstructured path: RAG over SOPs

The SOP documents (like [`Cold_Chain_Incident_SOP_v2.md`](../data/policy/Cold_Chain_Incident_SOP_v2.md),
used here as the concrete example) get chunked, embedded, and stored in a vector
store; the agent retrieves relevant chunks and answers with the source cited.

- **Open, not decided this phase** (carried into Phase 3):
  - Chunking strategy — not chosen yet.
  - What happens when a source SOP is edited or deleted after indexing.
  - The actual ingestion pipeline for PDFs/`.md` files — doesn't exist yet.

### Security and governance model

Decided this phase, before any code exists to enforce it:

- **Least-privilege DB role** — the agent's SQL Server login can only `SELECT`.
  Enforced by the database itself, not application code, so even a bad instruction or
  prompt-injection attempt can't reach `INSERT`/`UPDATE`/`DELETE`.
- **Governed views** — the agent queries curated views exposing approved
  columns/rows only, never raw tables.
- **Immutable audit log** — every question and every answer gets written to an
  append-only log for later review.
- **Role clarity** — the agent never surfaces more than the requesting user's own
  role already permits.

### Guardrails: a deliberate scope decision, not an oversight

Content-moderation-style guardrails were explicitly **not** added. This is an internal
tool for internal staff, not a public-facing or customer-data product — the risk
guardrails exist to manage isn't present here today. Documented as a decision to
revisit if the audience or data sensitivity changes, not a gap to fill later.

### Business case as its own artifact

[`business-pitch.html`](business-pitch.html) restates the same technical decisions for
a non-technical audience: the problem (dashboards can't answer unplanned questions),
the cost of the manual process today (a 38-minute walk-through of a temperature alert),
and the same governance model in plain language. Same design, two audiences — the TDD
for engineers, the pitch for stakeholders.

## Python Concepts — Quick Reference

None — this phase was architecture and business documentation only. No source code
was written or changed.

## FDE / DDD Concepts

- **Interface as Contract** — the text2sql tool and the RAG tool are each a bounded
  contract exposed through MCP: the agent calls a defined tool, not an open connection
  to the database or the vector store.
- **Anti-Corruption Layer, applied to security** — governed views are the same idea as
  Phase 1's `LEGACY_COLUMN_NAMES` mapping, one layer up: the agent is only ever shown
  an approved, translated slice of the real schema, never the raw tables directly.
- **Ubiquitous Language, extended** — the query-loop's self-correction depends on the
  agent and the tool sharing the same vocabulary for "the query" and "the result,"
  so the agent can reason about its own output across turns.
- **Application layer (planned)** — `resources/` is where Phase 3 will place the
  tool-facing code that orchestrates both paths; nothing lives there yet.

## Run

Nothing to run — view [`technical-design-doc.pdf`](technical-design-doc.pdf) and
[`business-pitch.html`](business-pitch.html) directly in a browser, or read
[`research.md`](research.md) as Markdown.

## What's Next

Phase 3 — Implementation. Build the `resources/` application layer, the text2sql/MCP
tool against a governed view (not the raw `dbo.supply_chain_logistics` table), and the
SOP ingestion + RAG pipeline — resolving the open items listed above (chunking
strategy, source-change handling, PDF/Markdown ingestion) as they're actually built,
rather than in the abstract. See [`domain-model.md`](domain-model.md), which will grow
once that code exists.
