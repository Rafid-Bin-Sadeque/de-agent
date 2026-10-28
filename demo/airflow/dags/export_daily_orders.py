"""export_daily_orders (daily 02:00): analytics.mart_daily_revenue -> CSV in s3://exports/daily_revenue/date=<ds>/."""
import csv
import io
from datetime import datetime, timedelta

from airflow import DAG
from airflow.exceptions import AirflowSkipException
from airflow.operators.python import PythonOperator
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from airflow.providers.postgres.hooks.postgres import PostgresHook

BUCKET = "exports"


def export(ds: str) -> None:
    hook = PostgresHook(postgres_conn_id="warehouse")
    if hook.get_first("select to_regclass('analytics.mart_daily_revenue')")[0] is None:
        raise AirflowSkipException("analytics.mart_daily_revenue is not built yet (dbt_build has not run)")
    rows = hook.get_records(
        "select order_date, orders, revenue from analytics.mart_daily_revenue order by 1")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["order_date", "orders", "revenue"])
    w.writerows(rows)
    s3 = S3Hook(aws_conn_id="demo_s3")
    if not s3.check_for_bucket(BUCKET):
        s3.create_bucket(BUCKET)
    s3.load_string(buf.getvalue(), key=f"daily_revenue/date={ds}/revenue.csv", bucket_name=BUCKET, replace=True)
    print(f"exported {len(rows)} rows for {ds}")


with DAG(
    dag_id="export_daily_orders",
    schedule="0 2 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    default_args={"owner": "analytics-eng", "retries": 0, "execution_timeout": timedelta(minutes=15)},
    tags=["acme", "export"],
) as dag:
    PythonOperator(task_id="export_revenue_csv", python_callable=export)
