.PHONY: install lock sync lint format test ingest-dry-run ingest sqlserver-up sqlserver-down clean

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

sqlserver-up:
	docker run \
		--name legacy-sqlserver \
		--hostname legacy-sqlserver \
		--env-file .env.legacy_sqlserver \
		--publish 1433:1433 \
		--volume legacy_sqlserver_data:/var/opt/mssql \
		--detach \
		mcr.microsoft.com/mssql/server:2022-latest

sqlserver-down:
	docker stop legacy-sqlserver

clean:
	find . -type d -name "__pycache__" -not -path "./.venv/*" -exec rm -rf {} +
	rm -rf .ruff_cache .pytest_cache
