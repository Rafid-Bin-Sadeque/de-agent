from datetime import datetime

from airflow import DAG
from airflow.datasets import Dataset
from airflow.operators.python import PythonOperator

ORDERS = Dataset("warehouse://raw/orders")

with DAG("ds_producer", schedule="@hourly", start_date=datetime(2026, 1, 1), tags=["ds"]) as p:
    PythonOperator(task_id="produce", python_callable=print, outlets=[ORDERS, Dataset("warehouse://raw/events")])

with DAG("ds_consumer", schedule=[Dataset("warehouse://raw/orders")], start_date=datetime(2026, 1, 1), tags=["ds"]) as c:
    PythonOperator(task_id="consume", python_callable=print, inlets=[Dataset("warehouse://staging/stg_orders")])
