"""warehouse_maintenance (daily 03:00): VACUUM (ANALYZE) on raw.*."""
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook

TABLES = ["customers", "products", "orders", "order_items", "events"]


def vacuum() -> None:
    conn = PostgresHook(postgres_conn_id="warehouse").get_conn()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for t in TABLES:
                cur.execute(f"VACUUM (ANALYZE) raw.{t}")
                print(f"vacuumed raw.{t}")
    finally:
        conn.close()


with DAG(
    dag_id="warehouse_maintenance",
    schedule="0 3 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    default_args={"owner": "data-eng", "retries": 0, "execution_timeout": timedelta(minutes=30)},
    tags=["acme", "maintenance"],
) as dag:
    PythonOperator(task_id="vacuum_raw", python_callable=vacuum)
