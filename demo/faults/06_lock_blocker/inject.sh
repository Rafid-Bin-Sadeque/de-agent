#!/usr/bin/env bash
# Fault 06: a session takes ACCESS EXCLUSIVE on analytics.fct_orders and sits idle in transaction.
. "$(dirname "$0")/../_lib.sh"
n=$(wh_sql -tA -c "select count(*) from pg_stat_activity where application_name = 'dea_fault_06'")
if [ "$n" != "0" ]; then say "06 already injected"; exit 0; fi
"${COMPOSE[@]}" exec -T -d -e PGAPPNAME=dea_fault_06 warehouse bash -c \
  '(echo "BEGIN; LOCK TABLE analytics.fct_orders IN ACCESS EXCLUSIVE MODE;"; sleep 3600) | psql -X -q -U etl -d warehouse'
sleep 2
say "06 injected: dea_fault_06 holds ACCESS EXCLUSIVE on analytics.fct_orders (idle in transaction)"
