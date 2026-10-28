from datetime import datetime

from airflow import DAG
from airflow.providers.postgres.operators.postgres import PostgresOperator

with DAG("sql_file", schedule="@daily", start_date=datetime(2026, 1, 1), tags=["sql"]) as d:
    PostgresOperator(task_id="load_x", sql="sql/load_x.sql")
