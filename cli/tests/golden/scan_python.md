---
generated: content.81f03dd7b8
scan: jobs
type: python
---
# Python catalog: jobs

8 files (2 notebooks), 13 functions; 14 touch tables or SQL. Unresolved: 0 dynamic SQL, 0 unparsed SQL, 0 util args; parse errors: 1.

## jobs/load_orders.py

- py:jobs/load_orders.py · jobs/load_orders.py:1 · reads: — · writes: — · via util: jobs.load_orders.main>jobs.load_orders.load_orders, jobs.load_orders.main>jobs.load_orders.refresh_marts, jobs.load_orders.main>lib.etl.run_step
- py:jobs/load_orders.py:load_orders · jobs/load_orders.py:8 · reads: staging.orders_stage · writes: raw.orders, staging.orders_stage
- py:jobs/load_orders.py:main · jobs/load_orders.py:34 · reads: — · writes: — · via util: jobs.load_orders.load_orders, jobs.load_orders.refresh_marts, lib.etl.nightly>lib.etl.run_step, lib.etl.run_step
- py:jobs/load_orders.py:refresh_marts · jobs/load_orders.py:14 · reads: raw.orders · writes: analytics.daily_orders
- py:jobs/load_orders.py:stage_all · jobs/load_orders.py:23 · reads: landing.customers, landing.orders · writes: staging.customers_stage, staging.orders_stage

## jobs/pandas_job.py

- py:jobs/pandas_job.py:archive · jobs/pandas_job.py:14 · reads: raw.orders · writes: archive.orders
- py:jobs/pandas_job.py:export_daily · jobs/pandas_job.py:8 · reads: analytics.daily_orders, raw.customers · writes: exports.daily_orders_export

## jobs/spark_job.py

- py:jobs/spark_job.py:build_silver · jobs/spark_job.py:9 · reads: bronze.customers, s3://acme-lake/raw/orders/ · writes: s3://acme-lake/silver/orders_enriched/, silver.orders_enriched
- py:jobs/spark_job.py:sql_step · jobs/spark_job.py:22 · reads: public.refunds, s3://acme-lake/raw/orders/late/, silver.orders_enriched · writes: gold.daily_revenue
- py:jobs/spark_job.py:upsert · jobs/spark_job.py:17 · reads: gold.customer_ltv · writes: gold.customer_ltv

## lib/etl.py

- py:lib/etl.py:nightly · lib/etl.py:13 · reads: — · writes: — · via util: lib.etl.run_step
- py:lib/etl.py:vacuum_all · lib/etl.py:18 · reads: — · writes: — · via util: lib.etl.run_step

## notebooks/dbx_export.py

- nb:notebooks/dbx_export.py · notebooks/dbx_export.py:1 · reads: gold.revenue_daily, silver.orders_enriched · writes: gold.revenue_daily, s3://acme-lake/export/revenue/

## notebooks/orders_rollup.ipynb

- nb:notebooks/orders_rollup.ipynb · notebooks/orders_rollup.ipynb:1 · reads: analytics.orders_rollup_nb, raw.orders · writes: analytics.orders_rollup_nb
