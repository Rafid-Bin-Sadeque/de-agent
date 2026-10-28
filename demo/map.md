# Acme Shop data platform (demo): map

Fictional demo hub. Every name here is made up.

## Domains / Projects

| Name | What it is | Runs on |
|---|---|---|
| [[pipelines]] | Airflow DAGs: hourly `extract_shop` into warehouse `raw`, Dataset-triggered `dbt_build`, nightly `export_daily_orders`, `warehouse_maintenance` | Airflow (demo compose) |
| [[analytics]] | dbt project: `staging` views over `raw`, `analytics` marts (`fct_orders`, `dim_customers`, `mart_daily_revenue`) | warehouse Postgres via `dbt_build` |

## Platform

Live state is generated, never hand-written: see platform/index.md (refresh with `dea snapshot`).
Sources: `source` (shop app DB), `warehouse`, `airflow`, `dbt`.

## In flight

Dated checks and open issues live in status.md (none yet in the demo).
