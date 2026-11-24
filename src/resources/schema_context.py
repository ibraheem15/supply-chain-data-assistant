# This file builds a short block of text that describes the view, and the agent sends it to the LLM with every question. Think of it as the docs page you'd hand a new analyst before they write queries.

# COLUMNS is the tuple of business names, LEGACY_COLUMN_NAMES maps each business name to the
# legacy name that the SOP documents use. Both come from Phase 1.
from data.domain import load_domain

domain = load_domain()
VIEW_NAME = domain.view
COLUMNS = domain.column_names
LEGACY_COLUMN_NAMES = domain.legacy_names

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
