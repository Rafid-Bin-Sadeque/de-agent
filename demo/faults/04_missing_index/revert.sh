#!/usr/bin/env bash
. "$(dirname "$0")/../_lib.sh"
wh_sql <<'SQL'
DELETE FROM raw.orders WHERE id > 10000000;
CREATE INDEX IF NOT EXISTS orders_updated_at_idx ON raw.orders (updated_at);
ANALYZE raw.orders;
SQL
say "04 reverted: extra rows deleted, index recreated"
