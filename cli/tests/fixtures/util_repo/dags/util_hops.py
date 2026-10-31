from datetime import datetime

from airflow import DAG
from airflow.operators.python import PythonOperator
from utils import etl

with DAG("util_hops", schedule="@daily", start_date=datetime(2026, 1, 1), tags=["util"]) as dag:
    PythonOperator(task_id="two_hops", python_callable=lambda: etl.refresh("orders"))
    PythonOperator(task_id="three_hops", python_callable=etl.refresh_all)
    PythonOperator(task_id="book", python_callable=etl.bookkeeping)
