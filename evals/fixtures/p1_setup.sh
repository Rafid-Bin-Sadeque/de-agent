#!/usr/bin/env bash
# P1 setup (eval): seed a REAL late correction so "rebuild the mart for 2026-10-01..03" is a job worth planning.
#  1. normalise: rebuild the whole mart from fct_orders, so repeated runs start from a consistent state
#  2. correct the amounts of 30 source orders created 2026-10-01..03 (updated_at = now)
#  3. extract them into raw.orders (a direct task test: it does not emit the Dataset event, so dbt_build does not fire)
#  4. rebuild stg_orders + fct_orders (they pick the change up by updated_at); the mart is deliberately NOT rebuilt
#  5. verify the divergence exists (fct_orders revenue != mart revenue for those dates); fail setup if not.
# Demo stack only (localhost containers).
. "$(dirname "${BASH_SOURCE[0]}")/../../demo/faults/_lib.sh"
DBT=/home/airflow/dbt-venv/bin/dbt
dbt_run() { "${COMPOSE[@]}" exec -T airflow bash -c "cd /opt/dbt/acme && $DBT run $*" >/dev/null; }

dbt_run --select mart_daily_revenue
src_sql <<'SQL'
UPDATE public.orders
   SET total_amount = total_amount + 25, updated_at = now()
 WHERE id IN (SELECT id FROM public.orders
               WHERE created_at >= '2026-10-01' AND created_at < '2026-10-04' ORDER BY id LIMIT 30);
SQL
af tasks test extract_shop extract_orders "$(date -u +%Y-%m-%d)" >/dev/null
dbt_run --select stg_orders fct_orders

diff=$(wh_sql -At <<'SQL'
SELECT count(*) FROM (
  SELECT f.d, f.rev FROM (
    SELECT created_at::date d, sum(total_amount) FILTER (WHERE status NOT IN ('cancelled','refunded')) rev
      FROM analytics.fct_orders WHERE created_at >= '2026-10-01' AND created_at < '2026-10-04' GROUP BY 1) f
  JOIN analytics.mart_daily_revenue m ON m.order_date = f.d
  WHERE m.revenue IS DISTINCT FROM f.rev) x;
SQL
)
if [ "${diff:-0}" -lt 1 ]; then
  echo "[p1_setup] FAILED: fct_orders and mart_daily_revenue agree for 2026-10-01..03, no divergence was seeded" >&2
  exit 1
fi
say "p1 seeded: fct_orders carries a late correction the mart lacks on $diff of 3 days"
