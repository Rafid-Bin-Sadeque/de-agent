#!/usr/bin/env bash
# Demo-only: migrate the metadata DB, create the users, then run `airflow standalone`.
set -euo pipefail
airflow db migrate
airflow users create --username admin --password "${DEMO_AF_ADMIN_PASSWORD}" --firstname Demo --lastname Admin \
  --role Admin --email admin@example.invalid >/dev/null 2>&1 || true
# the Viewer role only exists once the webserver has initialised, so the read-only API user is created in a retry loop
(
  for _ in $(seq 1 60); do
    if airflow users list 2>/dev/null | grep -q agent_ro; then exit 0; fi
    airflow users create --username agent_ro --password "${DEMO_AF_PASSWORD}" --firstname Agent --lastname ReadOnly \
      --role Viewer --email agent@example.invalid >/dev/null 2>&1 || true
    sleep 5
  done
) &
# the S3 stand-in (SeaweedFS) connection used by export_daily_orders
airflow connections add demo_s3 --conn-type aws --conn-login "${DEMO_S3_USER}" --conn-password "${DEMO_S3_PASSWORD}" \
  --conn-extra '{"endpoint_url": "http://s3:8333", "region_name": "us-east-1"}' >/dev/null 2>&1 || true
exec /entrypoint standalone
