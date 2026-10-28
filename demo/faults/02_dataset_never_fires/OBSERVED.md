# Fault 02 dataset never fires: observed

Recorded by `DEA_DEMO=1 pytest -m demo` on 2026-10-27 (real run, demo stack).

- symptom: revenue dashboard didn't update today
- expected signal seen: yes
- seconds from inject to the signal in `dea diff`: 36

## Trimmed `dea diff`

```
# What changed since 2026-10-27 12:27 UTC (37 s)
## airflow
- dags extract_shop: is_paused False -> True
- runs: added extract_shop.manual__2026-10-27T12:28:01+00:00 (queued)
## dbt
- no changes
## source
- no changes
## warehouse
- no changes
```

## Notes

- manual run manual__2026-10-27T12:28:01+00:00 state after 20 s: queued (paused DAG: never starts)
- The collector's 'dataset has consumers but no event in 24h' signal needs a real 24 h of silence, so it cannot fire inside a test run. Within minutes the evidence is `is_paused` flipping plus the triggered run sitting `queued`; the dataset signal appears the next day if nobody unpauses.
