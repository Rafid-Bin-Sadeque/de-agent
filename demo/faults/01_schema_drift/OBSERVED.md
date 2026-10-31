# Fault 01 schema drift: observed

Recorded by `DEA_DEMO=1 pytest -m demo` on 2026-10-31 (real run, demo stack).

- symptom: orders mart is stale
- expected signal seen: yes
- seconds from inject to the signal in `dea diff`: 17

## Trimmed `dea diff`

```
# What changed since 2026-10-31 08:27 UTC (18 s)
## airflow
- NEW signal: dag extract_shop: 1 failed run(s) in 24h (tasks: extract_orders)
- runs: added extract_shop.manual__2026-10-31T08:27:57+00:00 (failed, 11 s)
- failures_24h: added extract_shop.extract_orders.1 (failed, 0 s)
## dbt
- no changes
## source
- no changes
## warehouse
- no changes
```

## Notes

- extract_shop run finished: failed
