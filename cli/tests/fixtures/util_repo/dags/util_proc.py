from datetime import datetime

from airflow import DAG
from airflow.operators.python import PythonOperator
from utils import db


def _refresh():
    db.run_proc("etl.sp_refresh_orders")


with DAG("refresh_orders_dag", schedule="@daily", start_date=datetime(2026, 1, 1), tags=["util"]) as dag:
    PythonOperator(task_id="refresh", python_callable=_refresh)
    PythonOperator(task_id="load", python_callable=db.load_orders)
    PythonOperator(task_id="copy", python_callable=lambda: db.copy_table("raw.orders", "staging.orders_copy"))
