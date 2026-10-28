from datetime import datetime

from airflow import DAG
from airflow.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.sensors.external_task import ExternalTaskSensor

with DAG("waiter", schedule="@daily", start_date=datetime(2026, 1, 1), tags=["sensor"]) as d:
    w = ExternalTaskSensor(task_id="wait", external_dag_id="ds_producer", pool="sensors")
    t = TriggerDagRunOperator(task_id="go", trigger_dag_id="ds_consumer")
    w >> t
