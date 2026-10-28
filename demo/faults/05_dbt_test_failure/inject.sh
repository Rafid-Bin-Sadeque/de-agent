#!/usr/bin/env bash
# Fault 05: 50 order_items rows are duplicated in the landing layer (raw has no primary key), so the dbt
# unique test on stg_order_items.order_item_id fails. (The source has a PK, so the duplicates enter via raw.)
. "$(dirname "$0")/../_lib.sh"
wh_sql <<'SQL'
INSERT INTO raw.order_items
SELECT * FROM raw.order_items WHERE id BETWEEN 1 AND 50
  AND (SELECT count(*) FROM raw.order_items WHERE id = 1) = 1;
SQL
say "05 injected: 50 duplicate order_items rows (ids 1..50) in raw.order_items"
