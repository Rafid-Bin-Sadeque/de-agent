from datetime import datetime

from airflow.decorators import dag, task
from airflow.sdk import Asset
from common.pg_utils import copy_df


@dag(schedule="@daily", start_date=datetime(2026, 1, 1), tags=["taskflow", "acme"],
     default_args={"owner": "team-tf"})
def taskflow_orders():
    @task(outlets=[Asset("warehouse://raw/orders")])
    def load():
        copy_df([], "raw.orders")

    load()


taskflow_orders()
