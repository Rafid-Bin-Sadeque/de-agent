# Fault 05 dbt test failure: observed

Recorded by `DEA_DEMO=1 pytest -m demo` on 2026-10-31 (real run, demo stack).

- symptom: dbt_build failing since this morning
- expected signal seen: yes
- seconds from inject to the signal in `dea diff`: 16

## Trimmed `dea diff`

```
# What changed since 2026-10-31 08:33 UTC (16 s)
## airflow
- NEW signal: dag dbt_build: 1 failed run(s) in 24h (tasks: dbt_build)
- runs: added dbt_build.manual__2026-10-31T08:33:19+00:00 (failed, 9 s)
- failures_24h: added dbt_build.dbt_build.1 (failed, 7 s)
## dbt
- NEW signal: dbt last run: 1 error/fail result(s)
- NEW signal: 1 dbt test(s) failing, guarding model.acme.stg_order_items
- last_run: n_fail - -> 1
- last_run: n_skipped - -> 8
- tests: added test.acme.unique_stg_order_items_order_item_id.90e333a108
## source
- no changes
## warehouse
- no changes
```

## Notes

- dbt_build run finished: failed
