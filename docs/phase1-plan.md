# Phase 1 Plan: Local SQL Server Development Environment

## Goal

Create a local Microsoft SQL Server 2022 environment in Docker so development can proceed against the database type expected by the client.

## Scenario and Assumption

For the project scenario, assume the client provides the SQL Server IP address and port needed for a future client-hosted connection. Those details are not currently verified. During development, run SQL Server locally in Docker and connect to the local host and mapped port instead.

The local container is a development substitute. It is not the client's legacy system, and starting it does not establish access to the client's data.

## Why Docker

Think of the database container as a locally run infrastructure dependency, similar to starting a service in a development or test environment. Docker gives the project a repeatable SQL Server instance without requiring a connection to the client's protected network. The application should read the database host and port from configuration so those values can differ between local development and a future client environment.

## Instructions

1. Confirm Docker is installed and running.
2. Define a Docker configuration for Microsoft SQL Server 2022.
3. Map the container's SQL Server port to a configurable port on the developer's machine. Record the local development host and mapped port in the setup documentation.
4. Configure the SQL Server administrator password through a local environment file or another untracked secret source. Provide an example environment file containing variable names and safe placeholders only; never commit real credentials.
5. Attach a Docker-managed volume for SQL Server data so database files survive container replacement.
6. Start the container and wait for SQL Server to report readiness before attempting a connection.
7. Connect to the local instance using a SQL client and the configured local host, port, and credentials.
8. Keep local connection settings separate from the assumed client IP and port. Make connection settings configurable rather than embedding either endpoint in application code.
9. Document start, stop, readiness, and connection steps, including how to preserve the named volume when stopping or recreating the container.
10. Run the implemented ingestion script to load the supplied raw CSV into the local SQL Server.

### Start the container

Copy `envs/legacy_sqlserver.example.env` to `envs/legacy_sqlserver.env` and replace the placeholder with a strong development password:

```dotenv
ACCEPT_EULA=Y
MSSQL_SA_PASSWORD=ReplaceWithA_Strong_Development_Password1!
```

Keep `envs/legacy_sqlserver.env` out of version control (it already is — see `.gitignore`). Then run:

```bash
docker run \
  --name legacy-sqlserver \
  --hostname legacy-sqlserver \
  --env-file envs/legacy_sqlserver.env \
  --publish 1433:1433 \
  --volume legacy_sqlserver_data:/var/opt/mssql \
  --detach \
  mcr.microsoft.com/mssql/server:2022-latest
```

(`make sqlserver-up` runs the same command; `make sqlserver-down` stops it.)

The port mapping is `host:container`: connect locally to `localhost,1433`. To use another local port, change only the first number, for example `--publish 1434:1433`, and connect to `localhost,1434`. The named volume keeps SQL Server data when the container is removed and recreated; do not remove the volume when resetting the container.

Check startup with `docker logs -f legacy-sqlserver` and wait for SQL Server to report that it is ready for client connections. Stop and restart the existing container with `docker stop legacy-sqlserver` and `docker start legacy-sqlserver`.

The image requires `ACCEPT_EULA=Y` and a valid strong `MSSQL_SA_PASSWORD`; the password must meet SQL Server's complexity policy. Treat the password file as a local secret even though it is not committed.

### Ingest the raw dataset

The CSV is the source artifact for local development. The loader is implemented in `src/data/ingest_supply_chain_dataset.py` using Python and `pyodbc`, a well-established fit for CSV processing and SQL Server integration. It reads the SQL Server administrator password from `envs/legacy_sqlserver.env` by default and never logs credential values. Install the Python dependencies with `uv sync` (see the [project README](../README.md) for `uv` setup).

`pyodbc` is only the client-side Python binding — the database server runs in the Docker container above, but the *ingestion script itself runs on the host* and needs its own client-side ODBC stack to connect out to that container over the network, the same way a `psql` or `mysql` client would need installing locally to reach a containerized database. Two host system packages are required, neither of which comes from `uv sync`:

- **unixODBC** — the ODBC driver manager `pyodbc` links against. On Arch: `sudo pacman -S unixodbc`.
- **Microsoft ODBC Driver 18 (or 17) for SQL Server** (`msodbcsql18`) — the actual SQL Server driver. Microsoft does not publish an official Arch package; install it from the AUR (e.g. `yay -S msodbcsql18`) or follow [Microsoft's manual Linux install instructions](https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server).

If `make ingest` fails with an error mentioning `libodbc.so`, that means unixODBC specifically is missing.

The ingestion workflow should:

1. Normalize CSV headers (the source has inconsistent surrounding spaces) and validate the expected 26 columns and each row's field count before connecting to SQL Server.
2. Parse timestamps and numeric fields explicitly, and reject invalid rows with a useful error that identifies the row and column without exposing credentials. Stop the ingestion process on encountering invalid rows.
3. Load validated records in batches into a staging table, then publish the completed load to the final table in a SQL transaction. A failed validation or load must leave the prior final dataset intact.
4. Make reruns idempotent: a successful rerun replaces the previous dataset rather than appending duplicate rows. Do not deduplicate source rows unless a business key is established.
5. Report the source, validated row count, batch progress, and completion status. Fail with a non-zero exit status on invalid input or an unsuccessful load.

Keep the raw CSV unchanged. The script accepts only `localhost` or `127.0.0.1` as its host, so it cannot connect to the assumed client endpoint. Its default database is `SupplyChainDb` and its default port is `1433`; host, port, database, and login can be configured with `MSSQL_HOST`, `MSSQL_PORT`, `MSSQL_DATABASE`, and `MSSQL_USER`. The password is read from `MSSQL_SA_PASSWORD`.

Run a validation-only pass first, then load the data after SQL Server is ready:

```bash
uv sync
make ingest-dry-run
make ingest
```

A successful run creates the target database and tables if needed, then reports the row count published to `dbo.supply_chain_logistics`. A repeated run replaces that table's contents in one transaction; the staging table and prior target data roll back together if loading or publishing fails.

## Scope Boundaries

- Do not connect to the client's protected system during this phase.
- Do not claim the client IP or port has been supplied, validated, or reached.
- Do not put credentials in tracked files or logs.

## Completion Criteria

- SQL Server 2022 starts locally through the documented Docker configuration.
- A developer can connect using the configured local host and mapped port.
- Credentials are provided outside tracked source files.
- SQL Server data persists through container replacement using a Docker-managed volume.
- Documentation distinguishes the local development endpoint from the assumed client-provided endpoint.
- The raw CSV loads into the local SQL Server with validation, transactional publishing, and safe reruns.
