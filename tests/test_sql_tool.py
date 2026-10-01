import pytest

from resources import sql_tool
from resources.sql_tool import validate_sql

VIEW = "ai.VW_SUPPLY_CHAIN_READINGS"


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
def test_accepts_plain_selects(sql):
    assert validate_sql(sql) is None


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
def test_rejects_everything_else(sql):
    assert validate_sql(sql) is not None


def test_invalid_sql_returns_error_without_touching_database(monkeypatch):
    def boom(_settings):
        raise AssertionError("must not connect")

    monkeypatch.setattr(sql_tool, "_connect", boom)
    result = sql_tool.query_telemetry_data("DROP TABLE x")
    assert result["status"] == "error" and result["sql"] == "DROP TABLE x"


class FakeCursor:
    description = [("n",)]

    def __init__(self, rows):
        self.rows = rows

    def execute(self, _sql):
        return self

    def fetchmany(self, size):
        return self.rows[:size]


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False

    def cursor(self):
        return FakeCursor(self.rows)

    def close(self):
        self.closed = True


def test_row_cap_sets_truncated(monkeypatch):
    connection = FakeConnection([(i,) for i in range(sql_tool.MAX_ROWS + 5)])
    monkeypatch.setattr(sql_tool, "_connect", lambda _s: connection)
    result = sql_tool.query_telemetry_data(f"SELECT n FROM {VIEW}")
    assert result["status"] == "ok"
    assert result["row_count"] == sql_tool.MAX_ROWS and result["truncated"] is True
    assert connection.closed


def test_backend_failure_is_unavailable(monkeypatch):
    def refuse(_settings):
        raise RuntimeError("MSSQL_AGENT_PASSWORD is missing.")

    monkeypatch.setattr(sql_tool, "_connect", refuse)
    assert sql_tool.query_telemetry_data(f"SELECT 1 FROM {VIEW}")["status"] == "unavailable"


@pytest.mark.integration
def test_live_query_and_denials():
    ok = sql_tool.query_telemetry_data(f"SELECT COUNT(*) AS n FROM {VIEW}")
    assert ok["status"] == "ok" and ok["rows"][0][0] > 0
    bad_column = sql_tool.query_telemetry_data(f"SELECT bogus FROM {VIEW}")
    assert bad_column["status"] == "error" and "bogus" in bad_column["error"]
    capped = sql_tool.query_telemetry_data(f"SELECT * FROM {VIEW}")
    assert capped["truncated"] is True
    after_dirty_close = sql_tool.query_telemetry_data(f"SELECT COUNT(*) AS n FROM {VIEW}")
    assert after_dirty_close["status"] == "ok"
