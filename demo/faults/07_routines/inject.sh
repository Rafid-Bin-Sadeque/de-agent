#!/usr/bin/env bash
# Routine edit (eval case R2): someone changes analytics.refresh_orders directly in the warehouse (the repo has no copy of it).
. "$(dirname "$0")/../_lib.sh"
wh_sql <<'SQL'
CREATE OR REPLACE PROCEDURE analytics.refresh_orders() LANGUAGE plpgsql AS $$
BEGIN
  DELETE FROM analytics.daily_orders WHERE order_date = current_date;
  INSERT INTO analytics.daily_orders (order_date, orders, revenue)
  SELECT current_date, count(*), coalesce(sum(total_amount), 0)
  FROM raw.orders
  WHERE created_at >= current_date AND status <> 'cancelled';
END
$$;
SQL
say "07 injected: analytics.refresh_orders now skips cancelled orders"
say "next: cd demo && dea snapshot && dea diff   (the changed hunk is in the routines section)"
