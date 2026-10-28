# Fault 04 missing index: observed

Recorded by `DEA_DEMO=1 pytest -m demo` on 2026-10-27 (real run, demo stack).

- symptom: extract_shop got 5x slower
- expected signal seen: yes
- seconds from inject to the signal in `dea diff`: 57

## Trimmed `dea diff`

```
# What changed since 2026-10-27 12:31 UTC (56 s)
## airflow
- runs: added dbt_build.dataset_triggered__2026-10-27T12:32:29.030011+00:00 (success, 21 s)
- runs: added extract_shop.manual__2026-10-27T12:32:06+00:00 (success, 43 s)
## dbt
- no changes
## source
- no changes
## warehouse
- tables raw.orders: n_live_tup 200,169 -> 711,809 (+256%)
```

## Notes

- extract_shop run finished: success
- extract_orders task: 0.75 s before the fault, 2.77 s after (3.7x). The DAG run's wall time is dominated by scheduler latency, so the run-level duration in the snapshot barely moves; the evidence is the raw.orders row jump plus seq_scan growth on the table and the missing index in `\d raw.orders`.
