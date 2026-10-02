import pytest

from resources import sql_tool
from resources.sql_tool import validate_sql

# Constant for the only table-like object the agent is allowed to query (a semantic view).
VIEW = "ai.VW_SUPPLY_CHAIN_READINGS"


# Parametrized test: runs once per SQL string. These are queries the validator must accept:
# plain SELECTs, CTEs (WITH), case variations, bracketed names and a trailing semicolon.
# The f-strings embed VIEW into each query.
@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT TOP 5 [timestamp], iot_temperature FROM {VIEW}",
        f"select count(*) from {VIEW} where iot_temperature > 4.0",
        f"SELECT risk_classification, AVG(delay_probability) FROM {VIEW} GROUP BY risk_classification",
        f"WITH hot AS (SELECT * FROM {VIEW} WHERE iot_temperature > 4) SELECT COUNT(*) FROM hot",
        "SELECT TOP 3 * FROM [ai].[VW_SUPPLY_CHAIN_READINGS];",
    ],
)
# validate_sql returns None when the SQL is fine, or a problem string when it is not.
def test_accepts_plain_selects(sql):
    assert validate_sql(sql) is None


# Parametrized again: queries the validator must reject (each returns a non-None problem string).
@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   ",
        f"DELETE FROM {VIEW}",
        f"UPDATE {VIEW} SET iot_temperature = 0",
        "DROP TABLE dbo.supply_chain_logistics",
        f"SELECT 1 FROM {VIEW}; DROP TABLE dbo.supply_chain_logistics",
        f"SELECT * INTO ai.copy FROM {VIEW}",
        "SELECT * FROM dbo.supply_chain_logistics",
        "SELECT * FROM sys.tables",
        f"SELECT 1 FROM {VIEW} UNION SELECT 1 FROM dbo.supply_chain_logistics",
        "EXEC xp_cmdshell 'dir'",
        "SELECT * FROM",
    ],
)
# Proves: the guardrail rejects anything that is not a read of the view: empty input, writes,
# DDL, multi-statement injection, SELECT INTO, other tables or schemas, UNION to other tables,
# stored procedures and unparseable SQL. Defence in depth on top of the read-only DB user.
def test_rejects_everything_else(sql):
    assert validate_sql(sql) is not None


# Proves: if the model uses legacy SOP column names, the error message names the correct
# column, so the model can self-correct on retry. Correct names pass (None).
def test_legacy_column_names_are_rejected_with_the_exact_fix():
    problem = validate_sql(f"SELECT IOT_TEMP_C FROM {VIEW} WHERE RISK_CLASS = 'High Risk'")
    assert "iot_temperature" in problem
    assert "risk_classification" in validate_sql(f"SELECT 1 FROM {VIEW} ORDER BY risk_class")
    assert validate_sql(f"SELECT iot_temperature, risk_classification FROM {VIEW}") is None


# Proves: rejected SQL never opens a database connection and is echoed back in the result.
# The replacement `boom` fails the test if called: a tripwire in place of a Mockito verify(never()).
def test_invalid_sql_returns_error_without_touching_database(monkeypatch):
    def boom(_settings):
        raise AssertionError("must not connect")

    monkeypatch.setattr(sql_tool, "_connect", boom)
    result = sql_tool.query_telemetry_data("DROP TABLE x")
    assert result["status"] == "error" and result["sql"] == "DROP TABLE x"


# Fake DB cursor (JDBC Statement/ResultSet stub): returns canned rows. `description` mimics
# DB-API column metadata: a list of tuples whose first item is the column name.
class FakeCursor:
    description = [("n",)]

    def __init__(self, rows):
        self.rows = rows

    # Returns self so calls can be chained, like the real cursor.execute(...).fetchmany(...).
    def execute(self, _sql):
        return self

    # Returns at most `size` rows, as the real driver does.
    def fetchmany(self, size):
        return self.rows[:size]


# Fake DB connection that records whether close() was called, to prove no connection leaks.
class FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False

    def cursor(self):
        return FakeCursor(self.rows)

    def close(self):
        self.closed = True


# Proves: results are capped at MAX_ROWS and flagged truncated, so a huge result cannot flood
# the model's context, and the connection is closed. The lambda ignores its argument (`_s`,
# the settings) and returns the fake connection.
def test_row_cap_sets_truncated(monkeypatch):
    connection = FakeConnection([(i,) for i in range(sql_tool.MAX_ROWS + 5)])
    monkeypatch.setattr(sql_tool, "_connect", lambda _s: connection)
    result = sql_tool.query_telemetry_data(f"SELECT n FROM {VIEW}")
    assert result["status"] == "ok"
    assert result["row_count"] == sql_tool.MAX_ROWS and result["truncated"] is True
    assert connection.closed


# Proves: a second cap, on total characters, also truncates (wide rows, few of them). The sum
# uses a generator expression over rows; +1100 allows for the last row that crosses the limit.
def test_wide_rows_are_cut_by_the_character_budget(monkeypatch):
    wide = "x" * 1000
    connection = FakeConnection([(wide,) for _ in range(sql_tool.MAX_ROWS)])
    monkeypatch.setattr(sql_tool, "_connect", lambda _s: connection)
    result = sql_tool.query_telemetry_data(f"SELECT n FROM {VIEW}")
    assert result["truncated"] is True and 0 < result["row_count"] < sql_tool.MAX_ROWS
    assert sum(len(str(row)) for row in result["rows"]) <= sql_tool.MAX_RESULT_CHARS + 1100


# Proves: infrastructure failures (e.g. missing password) become status 'unavailable', not a
# crash; the agent can then tell the user the data source is down.
def test_backend_failure_is_unavailable(monkeypatch):
    def refuse(_settings):
        raise RuntimeError("MSSQL_AGENT_PASSWORD is missing.")

    monkeypatch.setattr(sql_tool, "_connect", refuse)
    assert sql_tool.query_telemetry_data(f"SELECT 1 FROM {VIEW}")["status"] == "unavailable"


# Marker ~ JUnit @Tag("integration"): needs the live SQL Server container, skipped by default.
@pytest.mark.integration
# Proves against the real database: valid queries work, SQL errors come back as status 'error'
# with the DB message, big results are truncated, and the connection is still usable after
# an early close (the last query).
def test_live_query_and_denials():
    ok = sql_tool.query_telemetry_data(f"SELECT COUNT(*) AS n FROM {VIEW}")
    assert ok["status"] == "ok" and ok["rows"][0][0] > 0
    bad_column = sql_tool.query_telemetry_data(f"SELECT bogus FROM {VIEW}")
    assert bad_column["status"] == "error" and "bogus" in bad_column["error"]
    capped = sql_tool.query_telemetry_data(f"SELECT * FROM {VIEW}")
    assert capped["truncated"] is True
    after_dirty_close = sql_tool.query_telemetry_data(f"SELECT COUNT(*) AS n FROM {VIEW}")
    assert after_dirty_close["status"] == "ok"
