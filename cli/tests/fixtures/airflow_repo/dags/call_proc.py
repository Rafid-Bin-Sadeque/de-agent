from datetime import datetime

from airflow import DAG
from airflow.providers.postgres.operators.postgres import PostgresOperator

with DAG("call_proc", schedule="@daily", start_date=datetime(2026, 1, 1), tags=["proc"]) as d:
    PostgresOperator(task_id="refresh", sql="CALL etl.refresh_orders()")
    PostgresOperator(task_id="missing", sql="CALL etl.no_such_proc()")
