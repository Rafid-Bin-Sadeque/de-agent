# Fault 01 walkthrough (recorded)

Recorded by `DEA_DEMO=1 pytest -m walkthrough` on 2026-10-27 against the demo stack, with real `claude -p --plugin-dir .` runs (model alias `sonnet`).

- wall time: 414 s; API cost shown by the CLI: $0.94
- plan id: `fault01-schema-drift`

## Steps

| # | step | result | detail |
|---|---|---|---|
| 1 | inject fault 01, trigger extract_shop (40.7 s) | PASS | run manual__2026-10-27T12:38:41+00:00 finished: failed |
| 2 | claude names the renamed column and cites evidence (193.4 s) | PASS | names column: True; cites diff/log line: True; turns 4, cost 0.232392825 |
| 3 | plan file exists and passes dea plan check (19.7 s) | PASS | plan exists: True; check exit: 0; cost 0.28098542499999996 |
| 4 | human approves through a pty (dea approve) (0.7 s) | PASS | exit 0; dea plans: fault01-schema-drift  approved     medium Restore public.orders.customer_id (renamed to cu |
| 5 | executor applies; applied.json reports all steps ok (30.7 s) | PASS | status ok; steps ['ok', 'ok']; cost 0.42645652499999986 |
| 6 | re-run extract_shop; dea snapshot + dea diff show the fix (59.5 s) | PASS | re-run manual__2026-10-27T12:43:22+00:00: success; success run in diff: True |
| 7 | no write outside the plan (query_log) | PASS | 3 logged DB calls, 0 writes, 0 outside the plan |
| 8 | transcript summary recorded | PASS | demo/faults/01_schema_drift/WALKTHROUGH.md |

## What the model answered (step 2, trimmed)

```
**Symptom:** `extract_shop` fails in task `extract_orders`.

**Root cause:** the shop app renamed `public.orders.customer_id` to `cust_id`, but the DAG's hardcoded column list still selects `customer_id`.
- [V] Log: `psycopg2.errors.UndefinedColumn: column "customer_id" does not exist`, raised at `extract_shop.py` line 36.
- [V] The live source columns are `id,cust_id,status,total_amount,created_at,updated_at`.
- [V] `airflow/dags/extract_shop.py:17` has `"orders": "id, customer_id, status, ..."`.
- [V] `raw.orders` in the warehouse still has `customer_id`, so the load side is unaffected.

**Ruled out:**
- The `events` table is not affected: the source `events` still has `customer_id`.
- It isn't a warehouse or dbt problem: the `dea diff` shows no changes in source, warehouse or dbt.
- The `dbt_build` failure is unrelated: only one run failed and later runs succeeded.

**Fix:** no plan drafted. The fix is a code edit, not a DB write, and plan files only hold SQL steps. Change line 17 to alias the column:
```python
"orders": "id, cust_id as customer_id, status, total_amount, created_at, updated_at",
```
`raw.orders` and the downstream dbt models keep their schema.

**Not doing:**
- Renaming the column back in the source: the shop app owns it, so that needs the owning team's sign-off.
- Renaming `raw.orders.customer_id` or editing dbt staging: this breaks downstream models.
- Resetting watermarks or truncating raw tables: this isn't needed and loses data.

**Unknown:** whether the rename was deliberate. The shop team should confirm, because other consumers of that table may also break.

Want me to make the line 17 edit?
```

## The plan it wrote (trimmed)

```
---
id: fault01-schema-drift
title: Restore public.orders.customer_id (renamed to cust_id) so extract_shop works again
risk: medium
sources: [source_etl, source]
statement_timeout: 300s
---
## Why
`extract_shop` fails in task `extract_orders` after the shop app renamed `public.orders.customer_id` to `cust_id`. The source DB owner agreed to restore the original name.
- [V] Log: `psycopg2.errors.UndefinedColumn: column "customer_id" does not exist` at `/opt/airflow/dags/extract_shop.py` line 36 (`platform/airflow.md:76`).
- [V] Live `public.orders` columns: `id, cust_id, status, total_amount, created_at, updated_at`.
- [V] `airflow/dags/extract_shop.py:17` selects `id, customer_id, status, ...`.
- [V] Warehouse `raw.orders` still has `customer_id`, so the load side and dbt need no change.
- [V] `dea diff`: source no changes, warehouse no changes, dbt no changes; only the failed run `extract_shop.manual__2026-10-27T12:38:41+00:00`.

## Steps
### 1. Rename cust_id back to customer_id
```sql source=source_etl
ALTER TABLE public.orders RENAME COLUMN cust_id TO customer_id;
```
```sql verify source=source expect="= 1"
select count(*) from information_schema.columns where table_schema = 'public' and table_name = 'orders' and column_name = 'customer_id'
```

### 2. Confirm the drifted name is gone
```sql verify source=source expect="= 0"
select count(*) from information_schema.columns where table_schema = 'public' and table_name = 'orders' and column_name = 'cust_id'
```

## Rollback
```sql source=source_etl
ALTER TABLE public.orders RENAME COLUMN customer_id TO cust_id;
```

## Not doing
- Editing `extract_shop.py` to select `cust_id as customer_id`: the owner agreed to restore the original name, so the DAG stays as is.
- Renaming `raw.orders.customer_id` or editing dbt staging: breaks downstream models for no gain.
- Resetting watermarks or truncating raw tables: nothing was loaded wrong, so there is nothing to undo.
- Editing or skipping dbt tests to hide failures.
- Dropping and re-adding the column: loses data. `RENAME COLUMN` keeps it.

```

## Guard denials the model actually hit

- none (the model never tried a denied action)

## Applied result (`applied.json`, trimmed)

```json
{
  "plan_id": "fault01-schema-drift",
  "mode": "apply",
  "status": "ok",
  "steps": [
    {
      "step": 1,
      "title": "Rename cust_id back to customer_id",
      "status": "ok",
      "rowcount": null,
      "duration_s": 0.396,
      "verify": [
        {
          "source": "source",
          "sql": "select count(*) from information_schema.columns where table_schema = 'public' and table_name = 'orders' and column_name = 'customer_id'",
          "expect": "= 1",
          "value": 1,
          "ok": true
        }
      ],
      "error": null
    },
    {
      "step": 2,
      "title": "Confirm the drifted name is gone",
      "status": "ok",
      "rowcount": null,
      "duration_s": 0.038,
      "verify": [
        {
          "source": "source",
          "sql": "select count(*) from information_schema.columns where table_schema = 'public' and table_name = 'orders' and column_name = 'cust_id'",
          "expect": "= 0",
          "value": 0,
          "ok": true
        }
      ],
      "error": null
    }
  ]
}
```

## Post-apply `dea diff` (trimmed)

```
# What changed since 2026-10-27 12:43 UTC (1 min)

## airflow
- runs: added dbt_build.dataset_triggered__2026-10-27T12:43:37.162354+00:00 (success, 21 s)
- runs: added extract_shop.manual__2026-10-27T12:43:22+00:00 (success, 42 s)

## dbt
- no changes

## source
- no changes

## warehouse
- NEW signal: 1 query running > 5 min
- long_queries: added 4555

```

## Notes

- The `NEW signal: 1 query running > 5 min` on `warehouse` above was noise (M2.1): the Airflow metastore's long-lived `SELECT pg_advisory_unlock(1)` connection sits `idle in transaction`, which the old `state <> 'idle'` filter counted. `long_queries` now counts `state = 'active'` only.
- query_log: 3 DB calls through Bash, 0 writes (all by the executor or none); `dea apply` runs its own approved SQL and is recorded in applied.json, not in query_log.
