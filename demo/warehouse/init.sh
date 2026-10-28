#!/bin/bash
# Warehouse roles. etl = write role; readonly_agent = de-agent; airflow_meta = Airflow metadata DB owner.
set -euo pipefail
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres <<SQL
CREATE ROLE etl LOGIN PASSWORD '${DEMO_ETL_PASSWORD}';
CREATE ROLE readonly_agent LOGIN PASSWORD '${DEMO_WH_PASSWORD}';
CREATE ROLE airflow_meta LOGIN PASSWORD '${DEMO_ETL_PASSWORD}';
GRANT pg_read_all_stats TO readonly_agent;
CREATE DATABASE airflow_meta OWNER airflow_meta;
SQL
