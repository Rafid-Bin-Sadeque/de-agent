---
owner: analytics-eng
domain: acme-shop
repo: dbt/acme
updated: 2026-10-27
tags: [dbt, marts]
---
# analytics

Acme Shop's dbt project: staging views over `raw.*`, then the `analytics` marts (`fct_orders`, `dim_customers`,
`mart_daily_revenue`). Built by the `dbt_build` DAG in [[pipelines]].

## Gotchas

- `fct_orders` is incremental on `updated_at`; a late-arriving order older than the watermark is picked up only by a full refresh.
- Tests guard order status values; an unexpected status fails the build (see seeded fault 05).
