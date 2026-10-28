#!/usr/bin/env bash
# Fault 03: autovacuum off on raw.events, maintenance DAG paused, 60% of rows updated twice => dead tuples.
. "$(dirname "$0")/../_lib.sh"
af dags pause warehouse_maintenance 2>&1 | tail -1
wh_sql -c "ALTER TABLE raw.events SET (autovacuum_enabled = false)"
for i in 1 2; do
  wh_sql -c "UPDATE raw.events SET payload = payload WHERE id % 10 < 6"
  say "update pass $i done"
done
say "03 injected: raw.events has ~1.2M dead tuples, autovacuum disabled, warehouse_maintenance paused"
