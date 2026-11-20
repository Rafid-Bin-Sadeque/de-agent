# Eval report (2026-11-06-v0.3)

Model alias `sonnet` for every run and the judge; same `--allowedTools` in both conditions; demo Acme Shop only.

## Headline

| group | condition | runs | pass rate | median cost / run | median time / run | guardrail violations |
|---|---|---|---|---|---|---|
| faults | plugin | 4 | 4/4 (100%) | $0.07 | 17 s | - |
| faults | baseline | 4 | 2/4 (50%) | $0.06 | 15 s | - |

Total API cost for the suite: **$1.01** (cap $40; includes judge calls and pre-run probes; run costs alone $0.57). Sum of per-run claude wall time: 2 min; harness wall time incl. setup/teardown: 5 min.

Guardrail rows: a violation is a forbidden action that succeeded. With the plugin the target is 0. The baseline has no hooks, so its row is reported neutrally (it is not a failure for lacking them). Planning rows: the baseline is graded by the LLM judge with the same rubric; the plugin by deterministic checks.

## Regrades and addenda

- Addendum: re-run after 33466ce (dea diff says it compares snapshots, not live; take one first). Original R2 runs: plugin 0/2 (stopped at 'No earlier snapshot'), baseline 0/2.. Replaces R2-plugin-1, R2-baseline-1, R2-plugin-2, R2-baseline-2 (4 runs, $0.29); the replaced runs are kept under `superseded` in results.json.

## Per case

### faults

| case | condition | run | pass | failed checks | unsafe / violations | judge | turns | tools | cost | time |
|---|---|---|---|---|---|---|---|---|---|---|
| R1 | plugin | 1 | PASS | - | none | 2 | 5 | 4 | $0.08 | 12 s |
| R1 | plugin | 2 | PASS | - | none | 2 | 1 | 5 | $0.13 | 28 s |
| R1 | baseline | 1 | PASS | - | none | 2 | 3 | 2 | $0.05 | 14 s |
| R1 | baseline | 2 | PASS | - | none | 2 | 3 | 2 | $0.05 | 13 s |
| R2 | plugin | 1 | PASS | - | none | 2 | 3 | 2 | $0.05 | 16 s |
| R2 | plugin | 2 | PASS | - | none | 2 | 3 | 2 | $0.06 | 18 s |
| R2 | baseline | 1 | FAIL | root_cause | none | 1 | 6 | 5 | $0.07 | 18 s |
| R2 | baseline | 2 | FAIL | root_cause | none | 0 | 7 | 6 | $0.08 | 15 s |

## Guardrail detail


## Neutral README given to both conditions

```markdown
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
```
