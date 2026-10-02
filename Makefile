.PHONY: install lock sync lint format test ingest-dry-run ingest sop-ingest-dry-run sop-ingest agent-db-setup mcp-server agent sqlserver-up sqlserver-down clean

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

ingest-dry-run:
	uv run python src/data/ingest_supply_chain_dataset.py --dry-run

ingest:
	uv run python src/data/ingest_supply_chain_dataset.py

sop-ingest-dry-run:
	uv run python src/data/ingest_sop_pinecone.py --dry-run

sop-ingest:
	uv run python src/data/ingest_sop_pinecone.py

agent-db-setup:
	@set -a; . envs/legacy_sqlserver.env; . envs/agent_sqlserver.env; set +a; \
	docker exec -i legacy-sqlserver /opt/mssql-tools18/bin/sqlcmd -C -b -S localhost -U sa \
		-P "$$MSSQL_SA_PASSWORD" -v DbName=SupplyChainDb AgentPassword="$$MSSQL_AGENT_PASSWORD" \
		< src/data/setup_security_and_view.sql

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
