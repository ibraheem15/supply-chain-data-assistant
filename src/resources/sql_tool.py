from resources.db import open_connection
from resources.settings import AGENT_SQL_ENV_FILE, load_settings

import pyodbc

MAX_ROWS = 50


def validate_sql(sql: str) -> str | None:
    """
    Validates the given SQL query.
    Returns an error message if the SQL is invalid, otherwise None.
    """
    sql = sql.strip().lower()
    if not sql or not sql.startswith("select"):
        return {"status": "error", "message": "Only SELECT statements are allowed."}
    if "limit" in sql:
        return {"status": "error", "message": "Use TOP n instead of LIMIT."}

    # No ;
    if ";" in sql:
        return {
            "status": "error",
            "message": "Semicolons are not allowed in the query.",
        }

    # NO --
    if "--" in sql:
        return {"status": "error", "message": "Double hyphens are not allowed in the query."}

    return None


def query_telemetry_data(sql: str):
    """
    Queries the telemetry data using the given SQL query.
    """
    validation_error = validate_sql(sql)
    if validation_error:
        return validation_error
    try:
        connection = open_connection(load_settings(AGENT_SQL_ENV_FILE))
        cursor = connection.cursor()
        cursor.execute(sql)
        rows = cursor.fetchmany(MAX_ROWS)  # Fetch only the first MAX_ROWS rows to limit memory usage
        rows = [[str(value) for value in row] for row in rows]

        columns = [desc[0] for desc in cursor.description]
        truncated = len(rows) == MAX_ROWS
        return {
            "status": "ok",
            "sql": sql,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }
    except pyodbc.Error as e:
        return {"status": "error", "message": str(e)}
    finally:
        cursor.close()
        connection.close()


if __name__ == "__main__":
    print(
        query_telemetry_data(
            "SELECT TOP 3 timestamp, iot_temperature FROM ai_2.VW_SUPPLY_CHAIN_READINGS"
        )
    )
    print(query_telemetry_data("DELETE FROM ai_2.VW_SUPPLY_CHAIN_READINGS"))
    print(query_telemetry_data("SELECT TOP 1 * FROM dbo.supply_chain_logistics"))
