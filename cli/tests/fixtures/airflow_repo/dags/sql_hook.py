from datetime import datetime

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook

BASE = "select * from raw.customers"
SCHEMA = "raw"
BAD = "select * from ((( broken"


def work():
    hook = PostgresHook(postgres_conn_id="warehouse")
    hook.run("update raw.customers set country = 'XX' where country is null")
    hook.get_records(BASE + " where id > 10")
    hook.get_first(f"select count(*) from {SCHEMA}.products")
    hook.get_pandas_df(BAD)
    hook.run(f"truncate {hook.schema}.x")


with DAG("sql_hook", schedule="@daily", start_date=datetime(2026, 1, 1), tags=["sql"]) as d:
    PythonOperator(task_id="work", python_callable=work)
