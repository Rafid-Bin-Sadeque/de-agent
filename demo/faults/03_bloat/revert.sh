#!/usr/bin/env bash
. "$(dirname "$0")/../_lib.sh"
wh_sql -c "ALTER TABLE raw.events RESET (autovacuum_enabled)"
af dags unpause warehouse_maintenance 2>&1 | tail -1
wh_sql -c "VACUUM (ANALYZE) raw.events"
say "03 reverted: autovacuum re-enabled, raw.events vacuumed, warehouse_maintenance unpaused"
