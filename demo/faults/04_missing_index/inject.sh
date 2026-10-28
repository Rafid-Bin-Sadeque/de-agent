#!/usr/bin/env bash
# Fault 04: the raw.orders(updated_at) index is dropped and 500k old rows are added, so the post-load check scans.
. "$(dirname "$0")/../_lib.sh"
wh_sql <<'SQL'
DROP INDEX IF EXISTS raw.orders_updated_at_idx;
INSERT INTO raw.orders (id, customer_id, status, total_amount, created_at, updated_at)
SELECT 10000000 + g, 1 + g % 20000, 'delivered', 10 + g % 90,
       now() - interval '200 days' + (g % 1000) * interval '1 minute',
       now() - interval '200 days' + (g % 1000) * interval '1 minute'
FROM generate_series(1, 500000) g
WHERE NOT EXISTS (SELECT 1 FROM raw.orders WHERE id = 10000001);
ANALYZE raw.orders;
SQL
say "04 injected: raw.orders_updated_at_idx dropped, +500k old rows in raw.orders (ids > 10,000,000)"
