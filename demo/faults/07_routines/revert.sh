#!/usr/bin/env bash
. "$(dirname "$0")/../_lib.sh"
wh_sql <<'SQL'
CREATE OR REPLACE PROCEDURE analytics.refresh_orders() LANGUAGE plpgsql AS $$
BEGIN
  DELETE FROM analytics.daily_orders WHERE order_date = current_date;
  INSERT INTO analytics.daily_orders (order_date, orders, revenue)
  SELECT current_date, count(*), coalesce(sum(total_amount), 0)
  FROM raw.orders
  WHERE created_at >= current_date;
END
$$;
SQL
say "07 reverted: analytics.refresh_orders restored"
