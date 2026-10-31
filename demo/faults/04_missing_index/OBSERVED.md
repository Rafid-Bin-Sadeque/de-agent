# Fault 04 missing index: observed

Recorded by `DEA_DEMO=1 pytest -m demo` on 2026-10-31 (real run, demo stack).

- symptom: extract_shop got 5x slower
- expected signal seen: yes
- seconds from inject to the signal in `dea diff`: 39

## Trimmed `dea diff`

```
# What changed since 2026-10-31 08:31 UTC (39 s)
## airflow
- runs: added dbt_build.dataset_triggered__2026-10-31T08:32:14.330980+00:00 (success, 11 s)
- runs: added extract_shop.manual__2026-10-31T08:31:58+00:00 (success, 27 s)
## dbt
- no changes
## source
- no changes
## warehouse
- tables raw.orders: n_live_tup 200,115 -> 700,134 (+250%)
```

## Notes

- extract_shop run finished: success
- extract_orders task: 0.54 s before the fault, 1.51 s after (2.8x). The DAG run's wall time is dominated by scheduler latency, so the run-level duration in the snapshot barely moves; the evidence is the raw.orders row jump plus seq_scan growth on the table and the missing index in `\d raw.orders`.
