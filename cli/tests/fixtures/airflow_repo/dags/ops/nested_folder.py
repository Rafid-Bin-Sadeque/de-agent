from datetime import datetime

from airflow import DAG
from airflow.operators.empty import EmptyOperator

with DAG("ops_noop", schedule="@weekly", start_date=datetime(2026, 1, 1), tags=["ops"]):
    EmptyOperator(task_id="noop")
