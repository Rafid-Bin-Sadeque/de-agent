# Fault 06 lock blocker: observed

Recorded by `DEA_DEMO=1 pytest -m demo` on 2026-10-27 (real run, demo stack).

- symptom: dbt_build is hanging
- expected signal seen: yes
- seconds from inject to the signal in `dea diff`: 50

## Trimmed `dea diff`

```
# What changed since 2026-10-27 12:35 UTC (46 s)
## airflow
- runs: added dbt_build.manual__2026-10-27T12:35:25+00:00 (running)
## dbt
- no changes
## source
- no changes
## warehouse
- NEW signal: sizes of schemas, tables are estimates: a relation is locked (lock timeout)
- NEW signal: 1 blocked session(s)
- blocking: added 3983.3905 ('LOCK TABLE analytics.fct_orders IN ACCESS EXCLUSIVE MODE;' blocks '/* {"app": "dbt", "dbt_version": "1.8.10", "profil')
```

## Notes

- dbt_build run state after 30 s: running
