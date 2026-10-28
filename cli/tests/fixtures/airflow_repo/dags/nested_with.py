from datetime import datetime

from airflow import DAG
from airflow.operators.empty import EmptyOperator


def build():
    with DAG("nested_dag", schedule=None, start_date=datetime(2026, 1, 1), tags=["nested"]) as d:
        EmptyOperator(task_id="noop")
    return d


nested = build()
