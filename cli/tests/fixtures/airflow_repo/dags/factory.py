from datetime import datetime

from airflow import DAG
from airflow.operators.empty import EmptyOperator

REGIONS = ["eu", "us"]


def make_export(region):
    with DAG(f"export_{region}", schedule="@daily", start_date=datetime(2026, 1, 1), tags=["factory"]) as d:
        EmptyOperator(task_id="start")
    return d


for r in REGIONS:
    globals()[f"export_{r}"] = make_export(r)


def make_unknown(name):
    return DAG(f"report_{name}", schedule=None)


report = make_unknown(__name__)
