.PHONY: install lock sync lint format test ingest sop-ingest view-sql view-setup agent-db-setup audit-db-setup ui mcp-server agent sqlserver-up sqlserver-down clean

install: lock sync

lock:
	uv lock

sync:
	uv sync

lint:
	uv run ruff check .

format:
	uv run ruff format .

test:
	uv run pytest

ingest:
	PYTHONPATH=src uv run python -m data.ingest_supply_chain_dataset

sop-ingest:
	PYTHONPATH=src uv run python -m data.ingest_sop_pinecone

agent-db-setup:
	@set -a; . envs/legacy_sqlserver.env; . envs/agent_sqlserver.env; set +a; \
	docker exec -i legacy-sqlserver /opt/mssql-tools18/bin/sqlcmd -C -b -S localhost -U sa \
		-P "$$MSSQL_SA_PASSWORD" -v DbName=SupplyChainDb AgentPassword="$$MSSQL_AGENT_PASSWORD" \
		< src/data/setup_security_and_view.sql

# The view over the current domain's table, generated from domains/<DOMAIN>/columns.toml.
# view-sql only prints it. view-setup runs it as sa; the agent login must already exist (agent-db-setup).
view-sql:
	PYTHONPATH=src uv run python -m data.make_view_sql

view-setup:
	@set -a; . envs/legacy_sqlserver.env; . envs/agent_sqlserver.env; set +a; \
	PYTHONPATH=src uv run python -m data.make_view_sql | \
	docker exec -i legacy-sqlserver /opt/mssql-tools18/bin/sqlcmd -C -b -S localhost -U sa \
		-P "$$MSSQL_SA_PASSWORD" -v DbName=SupplyChainDb AgentUser="$$MSSQL_AGENT_USER"

audit-db-setup:
	@set -a; . envs/legacy_sqlserver.env; . envs/audit_sqlserver.env; set +a; \
	docker exec -i legacy-sqlserver /opt/mssql-tools18/bin/sqlcmd -C -b -S localhost -U sa \
		-P "$$MSSQL_SA_PASSWORD" -v DbName=SupplyChainDb \
		WriterPassword="$$MSSQL_AUDIT_WRITER_PASSWORD" ReaderPassword="$$MSSQL_AUDIT_READER_PASSWORD" \
		< src/data/setup_audit_log.sql

ui:
	PYTHONPATH=src uv run streamlit run src/ui/app.py --server.address localhost

AGENT_ARGS ?=

agent:
	PYTHONPATH=src uv run python -m agent.cli $(AGENT_ARGS)

mcp-server:
	PYTHONPATH=src uv run python -m resources.mcp_server

sqlserver-up:
	docker run \
		--name legacy-sqlserver \
		--hostname legacy-sqlserver \
		--env-file envs/legacy_sqlserver.env \
		--publish 1433:1433 \
		--volume legacy_sqlserver_data:/var/opt/mssql \
		--detach \
		mcr.microsoft.com/mssql/server:2022-latest

sqlserver-down:
	docker stop legacy-sqlserver

clean:
	find . -type d -name "__pycache__" -not -path "./.venv/*" -exec rm -rf {} +
	rm -rf .ruff_cache .pytest_cache
