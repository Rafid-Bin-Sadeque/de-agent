# Fault 03 bloat: observed

Recorded by `DEA_DEMO=1 pytest -m demo` on 2026-10-31 (real run, demo stack).

- symptom: warehouse feels slow
- expected signal seen: yes
- seconds from inject to the signal in `dea diff`: 13

## Trimmed `dea diff`

```
# What changed since 2026-10-31 08:30 UTC (14 s)
## airflow
- dags warehouse_maintenance: is_paused False -> True
## dbt
- no changes
## source
- no changes
## warehouse
- NEW signal: table raw.events 54% dead tuples
- databases warehouse: size_bytes 235.5 MB -> 367.0 MB (+56%)
- schemas raw: total_bytes 208.2 MB -> 339.7 MB (+63%)
- tables raw.events: n_dead_tup 21 -> 1,199,489 (+5711752%); table_bytes 96.6 MB -> 212.2 MB (+120%); total_bytes 126.0 MB -> 257.6 MB (+104%)
```

## Notes

- The `warehouse_maintenance` pause shows as an airflow diff line; the bloat itself is the dead-tuple signal and the table/DB size growth.
