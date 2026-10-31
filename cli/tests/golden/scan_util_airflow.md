---
generated: content.437125cbd2
scan: pipelines
type: airflow
---
# Airflow catalog: pipelines

3 DAGs (0 dynamic) in 3 files; unresolved: 0 calls, 0 dynamic SQL, 0 datasets, 0 unparsed SQL.

## .

### util

- nightly_procs · @daily · — · util · dags/util_loop.py:10 · in: — · out: — · writes: — · reads: — · via util: utils.db.run_proc
- refresh_orders_dag · @daily · — · util · dags/util_proc.py:12 · in: — · out: — · writes: staging.orders_copy, staging.orders_stage · reads: raw.orders, raw.orders_feed · via util: utils.db.copy_table, utils.db.load_orders, utils.db.run_proc
- util_hops · @daily · — · util · dags/util_hops.py:7 · in: — · out: — · writes: etl.load_state · reads: — · via util: utils.etl.bookkeeping, utils.etl.bookkeeping>utils.db.run_proc, utils.etl.refresh>utils.db.run_proc
