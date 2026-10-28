# Fault 01 schema drift: observed

Recorded by `DEA_DEMO=1 pytest -m demo` on 2026-10-27 (real run, demo stack).

- symptom: orders mart is stale
- expected signal seen: yes
- seconds from inject to the signal in `dea diff`: 44

## Trimmed `dea diff`

```
# What changed
## airflow (since 2026-10-27T12:25:59Z, 40 s)
- NEW signal: dag extract_shop: 1 failed run(s) in 24h (tasks: extract_orders)
- runs: added extract_shop.manual__2026-10-27T12:26:07+00:00 (failed, 24 s)
- failures_24h: added extract_shop.extract_orders.1 (failed, 0 s)
## dbt (since 2026-10-27T12:25:59Z, 40 s)
- no changes
## source (since 2026-10-27T12:25:59Z, 40 s)
- no changes
## warehouse (since 2026-10-27T12:25:58Z, 41 s)
- no changes
```

## Notes

- extract_shop run finished: failed
