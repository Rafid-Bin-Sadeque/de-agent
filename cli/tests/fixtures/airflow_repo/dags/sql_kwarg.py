from datetime import datetime

from airflow import DAG
from airflow.providers.postgres.operators.postgres import PostgresOperator

with DAG("sql_kwarg", schedule="@daily", start_date=datetime(2026, 1, 1), tags=["sql"]) as d:
    PostgresOperator(
        task_id="rollup",
        sql="insert into analytics.daily_orders select order_date, count(*) from staging.stg_orders group by 1",
    )
