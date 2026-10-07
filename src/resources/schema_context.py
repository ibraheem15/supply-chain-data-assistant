# This file builds a short block of text that describes the view, and the agent sends it to the LLM with every question. Think of it as the docs page you'd hand a new analyst before they write queries.

# COLUMNS is the tuple of business names, LEGACY_COLUMN_NAMES maps each business name to the
# legacy name that the SOP documents use. Both come from Phase 1.
from data.ingest_supply_chain_dataset import COLUMNS, LEGACY_COLUMN_NAMES

# The only database object the agent may query (the view from setup_security_and_view.sql)
VIEW_NAME = "ai_2.VW_SUPPLY_CHAIN_READINGS"


def build_schema_context() -> str:
    """
    Builds the plain-text description of the view that is sent to the LLM.
    """
    lines = [
        f"View: {VIEW_NAME} (the only queryable object). T-SQL: use TOP n, never LIMIT; one SELECT.",
        "Query ONLY the column names below. Names in [SOP: ...] appear in SOP documents and are NOT columns.",
        "Columns:",
    ]
    # One line per column: `name [SOP: legacy_name]`
    lines += [f"{column} [SOP: {LEGACY_COLUMN_NAMES[column]}]" for column in COLUMNS]
    return "\n".join(lines)


if __name__ == "__main__":
    print(build_schema_context())
