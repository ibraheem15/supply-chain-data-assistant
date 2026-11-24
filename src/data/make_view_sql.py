"""
Builds the SQL that exposes a domain's table to the agent through a view.

The view renames the legacy column names to the clean names in columns.toml, so the agent never sees
the legacy names. The script only PRINTS the SQL: nothing is sent to the database here.

Run:  DOMAIN=retail PYTHONPATH=src uv run python -m data.make_view_sql
"""

from data.domain import Domain, load_domain


def split_name(qualified: str) -> tuple[str, str]:
    """
    "ai_2.VW_RETAIL_SALES" -> ("ai_2", "VW_RETAIL_SALES"). Same for "dbo.retail_sales".
    """
    schema, name = qualified.split(".", 1)
    return schema, name


def build_view_sql(domain: Domain) -> str:
    """
    Returns the T-SQL that creates the domain's view and lets the agent login read it.
    $(DbName) and $(AgentUser) are sqlcmd variables; the Makefile target fills them in.
    """
    view_schema, view_name = split_name(domain.view)
    table_schema, table_name = split_name(domain.table)

    # One "[LEGACY] AS [clean_name]" line per column, in the order of columns.toml.
    select_lines = ",\n".join(
        f"    [{column.legacy}] AS [{column.name}]" for column in domain.columns
    )

    return f"""USE [$(DbName)];
GO

-- The view lives in the same schema as the other agent-facing views. The schema is owned by dbo, like the
-- table, so the agent can read the view without any permission on the table (ownership chaining).
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = N'{view_schema}')
    EXEC('CREATE SCHEMA [{view_schema}] AUTHORIZATION [dbo]');
GO

CREATE OR ALTER VIEW [{view_schema}].[{view_name}] AS
SELECT
{select_lines}
FROM [{table_schema}].[{table_name}];
GO

-- The agent login may read this one view and nothing else.
GRANT SELECT ON OBJECT::[{view_schema}].[{view_name}] TO [$(AgentUser)];
GO

-- Testing: the agent must read the view and must NOT read the raw table.
EXECUTE AS USER = '$(AgentUser)';

SELECT TOP 1 * FROM [{view_schema}].[{view_name}];

BEGIN TRY
    EXEC (N'SELECT TOP 1 * FROM [{table_schema}].[{table_name}]');
    PRINT 'FAIL  agent can read the raw table';
END TRY
BEGIN CATCH
    PRINT 'PASS  raw table SELECT denied';
END CATCH;

REVERT;
"""


if __name__ == "__main__":
    print(build_view_sql(load_domain()))
