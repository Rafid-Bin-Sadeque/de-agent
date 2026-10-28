# Fault 05 dbt test failure: observed

Recorded by `DEA_DEMO=1 pytest -m demo` on 2026-10-27 (real run, demo stack).

- symptom: dbt_build failing since this morning
- expected signal seen: yes
- seconds from inject to the signal in `dea diff`: 29

## Trimmed `dea diff`

```
# What changed since 2026-10-27 12:34 UTC (28 s)
## airflow
- NEW signal: dag dbt_build: 1 failed run(s) in 24h (tasks: dbt_build)
- runs: added dbt_build.manual__2026-10-27T12:34:13+00:00 (failed, 15 s)
- failures_24h: added dbt_build.dbt_build.1 (failed, 10 s)
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
