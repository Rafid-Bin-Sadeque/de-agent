---
generated: content.4af6afe945
scan: infra
type: terraform
---
# Terraform catalog: infra

15 resources in 2 files (15 data-relevant, 18 edges); unresolved refs: 1; parse errors: 0.

## aws_dms_endpoint (2)

- aws_dms_endpoint.shop_pg · main.tf:19
- aws_dms_endpoint.lake_s3 · main.tf:26

## aws_dms_replication_task (1)

- aws_dms_replication_task.orders_cdc · main.tf:37 · replicates: public.customers → s3://acme-lake/raw/public/customers/, public.orders → s3://acme-lake/raw/public/orders/

## aws_glue_catalog_table (1)

- aws_glue_catalog_table.orders · main.tf:80 · reads: s3://acme-lake/raw/public/orders/ · writes: lake_raw.orders

## aws_glue_job (1)

- aws_glue_job.silver_orders · main.tf:89 · reads: s3://acme-lake/raw/public/orders/, s3://acme-lake/reference/ · writes: s3://acme-lake/silver/orders/ · direction unclear: s3://acme-lake/reference/

## aws_kinesis_firehose_delivery_stream (1)

- aws_kinesis_firehose_delivery_stream.events_to_lake · main.tf:64 · reads: kinesis://shop-events · writes: s3://acme-lake/events/

## aws_kinesis_stream (1)

- aws_kinesis_stream.events · main.tf:60 · node kinesis://shop-events

## aws_mwaa_environment (1)

- aws_mwaa_environment.airflow · main.tf:104 · reads: s3://acme-lake/dags/

## aws_s3_bucket (1)

- aws_s3_bucket.lake · main.tf:15 · node s3://acme-lake/

## databricks_job (1)

- databricks_job.nightly · modules/warehouse.tf:39

## google_bigquery_dataset (1)

- google_bigquery_dataset.analytics · modules/warehouse.tf:1

## google_bigquery_table (1)

- google_bigquery_table.daily_revenue · modules/warehouse.tf:6 · reads: raw.orders · writes: analytics.daily_revenue

## google_pubsub_subscription (1)

- google_pubsub_subscription.orders_to_bq · modules/warehouse.tf:20 · reads: pubsub://topic/orders-events · writes: raw.orders_stream

## google_pubsub_topic (1)

- google_pubsub_topic.orders · modules/warehouse.tf:16 · node pubsub://topic/orders-events

## snowflake_task (1)

- snowflake_task.rollup · modules/warehouse.tf:29 · reads: raw.orders · writes: analytics.orders_rollup

## Unresolved references

- s3://${var.external_bucket}/x/ (in aws_glue_job.silver_orders)
