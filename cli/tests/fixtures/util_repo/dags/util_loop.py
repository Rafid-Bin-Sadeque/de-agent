from datetime import datetime

from airflow import DAG
from airflow.operators.python import PythonOperator
from utils.db import run_proc

NIGHTLY = ["etl.sp_nightly_a", "etl.sp_nightly_b"]
TABLE_JOBS = {"orders": "etl.sp_refresh_orders", "customers": "etl.sp_refresh_customers"}

with DAG("nightly_procs", schedule="@daily", start_date=datetime(2026, 1, 1), tags=["util"]) as dag:
    for p in NIGHTLY:
        PythonOperator(task_id=f"run_{p}", python_callable=run_proc, op_args=[p])
    for name, proc in TABLE_JOBS.items():
        PythonOperator(task_id=f"run_{name}", python_callable=run_proc, op_kwargs={"proc": proc})
