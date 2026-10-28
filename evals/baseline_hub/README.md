# Acme Shop data platform

A small online shop and its analytics stack. Everything runs locally in Docker (compose project `dea-demo`) and listens on 127.0.0.1.

## Services

| service | what it is | how to reach it |
|---|---|---|
| `source-db` | shop application database (Postgres 16), database `shop`, schema `public` | host `localhost:5440`; or `docker compose exec -T source-db psql -U etl -d shop` |
| `warehouse` | analytics warehouse (Postgres 16), database `warehouse`, schemas `raw`, `staging`, `analytics` | host `localhost:5441`; or `docker compose exec -T warehouse psql -U etl -d warehouse` |
| `airflow` | Airflow 2.10 (DAGs `extract_shop`, `dbt_build`, `export_daily_orders`, `warehouse_maintenance`) | UI/API `http://localhost:8085`; CLI: `docker compose exec -T airflow airflow ...` |
| `app-sim` | trickles new orders into `source-db` | `docker compose logs app-sim` |
| `s3` | S3 stand-in (SeaweedFS) | `http://localhost:8333` |

## Data flow

`source-db` -> `extract_shop` (Airflow, hourly) -> `warehouse.raw` -> `dbt_build` (dbt 1.8, runs when the `raw` datasets update) -> `warehouse.staging` / `warehouse.analytics` -> `export_daily_orders` -> `s3`.

## Code

- `airflow/dags/` : the DAGs. `airflow/` is mounted into the Airflow container.
- `dbt/acme/` : the dbt project (`dbt_project.yml`, `models/`, `macros/`). dbt runs inside the Airflow container.

## Credentials

Demo-only credentials are in `.env` in this folder (also exported in your shell environment):

- `DEMO_WH_PASSWORD`: password of the read-only role `readonly_agent` (both Postgres databases)
- `DEMO_ETL_PASSWORD`: password of the ETL role `etl` (can write)
- `DEMO_AF_PASSWORD`: Airflow user `agent_ro` (Viewer)

Run `docker compose` commands from this folder.
