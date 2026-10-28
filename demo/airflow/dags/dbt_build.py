"""dbt_build: runs when raw.orders AND raw.order_items have both been refreshed (Dataset schedule)."""
from datetime import datetime, timedelta

from airflow import DAG
from airflow.datasets import Dataset
from airflow.operators.bash import BashOperator

DBT = "/home/airflow/dbt-venv/bin/dbt"
CMD = f"""cd /opt/dbt/acme
{DBT} build --select staging+ marts+
rc=$?
{DBT} source freshness || true
exit $rc
"""

with DAG(
    dag_id="dbt_build",
    schedule=[Dataset("warehouse://raw/orders"), Dataset("warehouse://raw/order_items")],
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args={"owner": "analytics-eng", "retries": 0, "execution_timeout": timedelta(minutes=60)},
    tags=["acme", "dbt"],
) as dag:
    BashOperator(task_id="dbt_build", bash_command=CMD)
