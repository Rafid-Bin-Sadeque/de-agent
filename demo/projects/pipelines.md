---
owner: data-eng
domain: acme-shop
repo: airflow
updated: 2026-10-27
tags: [airflow, ingestion]
---
# pipelines

Acme Shop's Airflow project: hourly incremental extract from the shop database into the warehouse `raw` schema,
a Dataset-triggered dbt build, a nightly CSV export of daily revenue to S3, and warehouse maintenance.

## Gotchas

- The dbt build runs only after BOTH `raw.orders` and `raw.order_items` were refreshed in the same window.
- `export_daily_orders` skips (not fails) until dbt has built `analytics.mart_daily_revenue` at least once.
- Maintenance (VACUUM) runs at 03:00; avoid long ad-hoc jobs on `raw.*` around that time.
