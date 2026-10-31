---
generated: pipelines@content.437125cbd2,warehouse@content.c53a063b4e
type: edges
---
# Edges: who writes and reads each table

Derived by `dea scan`. `←` writers, `→` readers; `dea edges <table>` for detail.

## Tables

analytics.daily_customers ← dag:nightly_procs, procedure:etl.sp_nightly_b | → —
analytics.daily_orders ← dag:nightly_procs, procedure:etl.sp_nightly_a | → —
analytics.stale_jobs ← procedure:etl.sp_monitor | → —
etl.load_state ← dag:nightly_procs, dag:refresh_orders_dag, dag:util_hops, procedure:etl.sp_refresh_customers, procedure:etl.sp_refresh_orders, table:etl.load_state | → procedure:etl.sp_monitor
etl.run_log ← dag:util_hops, procedure:etl.sp_audit, table:etl.run_log | → —
raw.customers ← dag:nightly_procs, procedure:etl.sp_refresh_customers | → dag:nightly_procs, procedure:etl.sp_nightly_b
raw.orders ← dag:nightly_procs, dag:refresh_orders_dag, dag:util_hops, procedure:etl.sp_refresh_orders | → dag:nightly_procs, dag:refresh_orders_dag, procedure:etl.sp_nightly_a
raw.orders_feed ← — | → dag:refresh_orders_dag
staging.customers_stage ← — | → dag:nightly_procs, procedure:etl.sp_refresh_customers
staging.orders_copy ← dag:refresh_orders_dag | → —
staging.orders_stage ← dag:refresh_orders_dag | → dag:nightly_procs, dag:refresh_orders_dag, dag:util_hops, procedure:etl.sp_refresh_orders

## Cross-project

- etl.load_state: dag:nightly_procs [pipelines] → procedure:etl.sp_monitor [etl]
- etl.load_state: dag:refresh_orders_dag [pipelines] → procedure:etl.sp_monitor [etl]
- etl.load_state: dag:util_hops [pipelines] → procedure:etl.sp_monitor [etl]
- raw.customers: dag:nightly_procs [pipelines] → procedure:etl.sp_nightly_b [etl]
- raw.customers: procedure:etl.sp_refresh_customers [etl] → dag:nightly_procs [pipelines]
- raw.orders: dag:nightly_procs [pipelines] → procedure:etl.sp_nightly_a [etl]
- raw.orders: dag:refresh_orders_dag [pipelines] → procedure:etl.sp_nightly_a [etl]
- raw.orders: dag:util_hops [pipelines] → procedure:etl.sp_nightly_a [etl]
- raw.orders: procedure:etl.sp_refresh_orders [etl] → dag:nightly_procs [pipelines]
- raw.orders: procedure:etl.sp_refresh_orders [etl] → dag:refresh_orders_dag [pipelines]
- staging.orders_stage: dag:refresh_orders_dag [pipelines] → procedure:etl.sp_refresh_orders [etl]

## DAG links

none

## Unresolved

Names referenced but never written anywhere in the scanned projects: external sources or typos.

- table raw.orders_feed (referenced by dag:refresh_orders_dag)
- table staging.customers_stage (referenced by dag:nightly_procs, procedure:etl.sp_refresh_customers)

## Counts

- cross_project_edges: 11
- pipelines.unattributed_sql: 0
- pipelines.unparsed_sql: 0
- pipelines.unresolved_calls: 0
- pipelines.unresolved_datasets: 0
- pipelines.unresolved_sql: 0
- pipelines.unresolved_sql_files: 0
- pipelines.unresolved_util_args: 0
- tables: 11
- unqualified_tables_skipped: 0
- unresolved_names: 2
