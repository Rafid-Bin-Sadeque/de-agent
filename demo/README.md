# Acme Shop demo platform

A small, fictional online shop for trying de-agent end to end. Everything runs locally in Docker (about 2 GB of RAM) and
binds to 127.0.0.1. No real data: the seed is generated (`seed/generate.py`, random seed 42).

```
source-db (postgres:16.4, OLTP shop) --app-sim trickle--> extract_shop (Airflow 2.10.5, hourly)
        --> warehouse (postgres:16.4: raw / staging / analytics) --> dbt_build (dbt 1.8, Dataset-triggered)
        --> export_daily_orders --> s3 (SeaweedFS, S3 stand-in)
```

| command | what it does |
|---|---|
| `make demo-up` | build + start (about 1 minute once images are cached); copies `.env.example` to `.env` |
| `make demo-snapshot` | `dea snapshot` against warehouse, source, airflow and dbt (`demo/de-agent.yaml`) |
| `cd demo && DEA_HUB=$PWD ../cli/.venv/bin/dea scan` | static scan of `airflow/` and `dbt/acme` (needs `dbt/acme/target/manifest.json`, written by `dbt parse`/`build` inside the airflow container): `catalog/` (gitignored), generated header in `projects/*.md`; then `dea edges analytics.fct_orders`, `dea impact raw.orders` |
| `make fault-inject N=01` / `make fault-revert N=01` | seeded faults 01-06 (see `faults/NN_*/EXPECTED.md`) |
| `DEA_DEMO=1 pytest -m walkthrough -s cli/tests/test_walkthrough.py` | the scripted `claude -p` flow on fault 01 (triage, plan, human approve via pty, executor apply, re-snapshot); writes `faults/01_schema_drift/WALKTHROUGH.md` |
| `make demo-down` / `make demo-reset` | stop / stop and wipe volumes |
| `DEA_DEMO=1 pytest -m demo -s cli/tests/test_demo_e2e.py` | the full e2e: every fault injected, detected through `dea diff`, reverted |

Ports (override in `demo/.env`): source 5440, warehouse 5441, Airflow 8085 (not 8080, which is often taken), S3 8333.
Credentials in `.env.example` are demo-only. Airflow users: `agent_ro` (Viewer, used by the collector) and `admin`.

Behind a TLS-inspecting proxy, `pip` inside the image build fails certificate verification: drop the proxy's root CA as
`demo/extra-ca/<name>.pem` (gitignored) and rebuild; the Dockerfiles add it to the pip trust bundle.

`faults/NN_*/OBSERVED.md` is written by the e2e from real runs (signal seen, which diff line, seconds taken).
`tools/record_fixtures.py` re-records the Airflow/dbt test fixtures from a running stack.

`source_etl` in `de-agent.yaml` is the demo's write-capable source (`writable: true`, the `etl` role): only `dea apply` uses it, for approved plans; it is never snapshotted or queried.
