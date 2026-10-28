"""Collector registry: {type: Collector}."""
from __future__ import annotations

from de_agent.collectors.airflow import AirflowCollector
from de_agent.collectors.aws import AwsCollector
from de_agent.collectors.base import Collector, CollectorError, RunContext  # noqa: F401
from de_agent.collectors.dbt import DbtCollector
from de_agent.collectors.postgres import PostgresCollector
from de_agent.collectors.redshift import RedshiftCollector

REGISTRY: dict[str, Collector] = {
    PostgresCollector.type: PostgresCollector(),
    AirflowCollector.type: AirflowCollector(),
    DbtCollector.type: DbtCollector(),
    RedshiftCollector.type: RedshiftCollector(),
    AwsCollector.type: AwsCollector(),
}


def registered_types() -> list[str]:
    return sorted(REGISTRY)


def get_collector(ctype: str) -> Collector:
    return REGISTRY[ctype]
