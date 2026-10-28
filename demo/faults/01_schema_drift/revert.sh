#!/usr/bin/env bash
. "$(dirname "$0")/../_lib.sh"
src_sql <<'SQL'
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='orders' AND column_name='cust_id') THEN
    ALTER TABLE public.orders RENAME COLUMN cust_id TO customer_id;
    RAISE NOTICE 'renamed orders.cust_id -> customer_id';
  ELSE
    RAISE NOTICE 'already reverted';
  END IF;
END $$;
SQL
say "01 reverted"
