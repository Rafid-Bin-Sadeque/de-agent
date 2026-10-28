import importlib
from datetime import datetime

import common.pg_utils as pgu
from airflow import DAG
from airflow.operators.python import PythonOperator
from common.pg_utils import Loader
from common.pg_utils import copy_df as cdf
from utils import dates
from utils.dates import *  # noqa: F403


def go(ds):
    cdf([], "raw.x")
    pgu.get_conn()
    Loader().run()
    dates.ds_to_date(ds)
    importlib.import_module("common.pg_utils").copy_df([], "x")


with DAG("util_user", schedule="@daily", start_date=datetime(2026, 1, 1), tags=["util"]) as d:
    PythonOperator(task_id="go", python_callable=go)
