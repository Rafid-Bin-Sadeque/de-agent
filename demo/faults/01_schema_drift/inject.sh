#!/usr/bin/env bash
# Fault 01: an upstream team renames orders.customer_id -> cust_id in the source DB.
. "$(dirname "$0")/../_lib.sh"
src_sql <<'SQL'
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='orders' AND column_name='customer_id') THEN
    ALTER TABLE public.orders RENAME COLUMN customer_id TO cust_id;
    RAISE NOTICE 'renamed orders.customer_id -> cust_id';
  ELSE
    RAISE NOTICE 'already injected (orders.cust_id exists)';
  END IF;
END $$;
SQL
say "01 injected: source orders.customer_id is now cust_id; extract_shop's orders task will fail"
