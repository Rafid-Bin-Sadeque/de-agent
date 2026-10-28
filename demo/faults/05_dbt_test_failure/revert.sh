#!/usr/bin/env bash
. "$(dirname "$0")/../_lib.sh"
wh_sql <<'SQL'
DELETE FROM raw.order_items a USING raw.order_items b
WHERE a.id = b.id AND a.ctid > b.ctid AND a.id BETWEEN 1 AND 50;
SQL
say "05 reverted: duplicates removed"
