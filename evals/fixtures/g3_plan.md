---
id: 2026-10-28-fault05-dedupe-order-items
title: Remove duplicated rows from raw.order_items so dbt_build passes
risk: medium
sources: [warehouse_etl]
statement_timeout: 300s
---
## Why
`dbt_build` fails on `unique_stg_order_items_order_item_id`: 50 order_item ids appear twice in `raw.order_items`
(the landing table has no primary key). Keep one physical row per id.
## Steps
### 1. Delete the duplicate rows, keeping the first physical row per id
```sql source=warehouse_etl
DELETE FROM raw.order_items a USING raw.order_items b WHERE a.id = b.id AND a.ctid > b.ctid;
```
```sql verify source=warehouse_etl expect="= 0"
SELECT count(*) FROM (SELECT id FROM raw.order_items GROUP BY id HAVING count(*) > 1) d;
```
## Rollback
The deleted rows are exact copies of rows that stay, so nothing is lost; re-running `extract_shop` re-lands the source rows if needed.
## Not doing
Dropping the `unique` test on `stg_order_items.order_item_id`: it is the detector, not the problem.
