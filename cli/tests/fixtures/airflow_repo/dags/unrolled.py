from datetime import datetime

from airflow import DAG
from airflow.datasets import Dataset
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook

TABLES = ("customers", "products")


def refresh():
    hook = PostgresHook()
    for t in TABLES:
        hook.run(f"insert into staging.copy_{t} select * from raw.{t}")
    for name in dynamic_names():
        hook.run(f"delete from raw.{name}")


with DAG("unrolled", schedule="@daily", start_date=datetime(2026, 1, 1), tags=["unroll"]) as d:
    for tbl in TABLES:
        PythonOperator(task_id=f"t_{tbl}", python_callable=refresh, outlets=[Dataset(f"warehouse://staging/copy_{tbl}")])
    PythonOperator(task_id="dyn", python_callable=refresh, outlets=[Dataset(make_uri())])
