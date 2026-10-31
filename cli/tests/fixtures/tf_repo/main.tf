# Fictional Acme Shop data platform infrastructure.
variable "lake_bucket" {
  type    = string
  default = "acme-lake"
}

variable "external_bucket" {
  type = string # no default: cannot be resolved
}

locals {
  raw_folder = "raw"
}

resource "aws_s3_bucket" "lake" {
  bucket = var.lake_bucket
}

resource "aws_dms_endpoint" "shop_pg" {
  endpoint_id   = "shop-pg"
  endpoint_type = "source"
  engine_name   = "postgres"
  database_name = "shop"
}

resource "aws_dms_endpoint" "lake_s3" {
  endpoint_id   = "lake-s3"
  endpoint_type = "target"
  engine_name   = "s3"

  s3_settings {
    bucket_name   = aws_s3_bucket.lake.id
    bucket_folder = local.raw_folder
  }
}

resource "aws_dms_replication_task" "orders_cdc" {
  replication_task_id      = "orders-cdc"
  migration_type           = "full-load-and-cdc"
  source_endpoint_arn      = aws_dms_endpoint.shop_pg.endpoint_arn
  target_endpoint_arn      = aws_dms_endpoint.lake_s3.endpoint_arn
  table_mappings = jsonencode({
    rules = [
      {
        "rule-type"      = "selection"
        "rule-id"        = "1"
        "rule-action"    = "include"
        "object-locator" = { "schema-name" = "public", "table-name" = "orders" }
      },
      {
        "rule-type"      = "selection"
        "rule-id"        = "2"
        "rule-action"    = "include"
        "object-locator" = { "schema-name" = "public", "table-name" = "customers" }
      },
    ]
  })
}

resource "aws_kinesis_stream" "events" {
  name = "shop-events"
}

resource "aws_kinesis_firehose_delivery_stream" "events_to_lake" {
  name        = "events-to-lake"
  destination = "extended_s3"

  kinesis_source_configuration {
    kinesis_stream_arn = aws_kinesis_stream.events.arn
    role_arn           = "arn:aws:iam::000000000000:role/fake"
  }

  extended_s3_configuration {
    bucket_arn = aws_s3_bucket.lake.arn
    role_arn   = "arn:aws:iam::000000000000:role/fake"
    prefix     = "events/!{timestamp:yyyy/MM/dd}/"
  }
}

resource "aws_glue_catalog_table" "orders" {
  name          = "orders"
  database_name = "lake_raw"

  storage_descriptor {
    location = "s3://acme-lake/raw/public/orders/"
  }
}

resource "aws_glue_job" "silver_orders" {
  name = "silver-orders"

  command {
    script_location = "s3://acme-lake/scripts/silver_orders.py"
  }

  default_arguments = {
    "--input_path"  = "s3://acme-lake/raw/public/orders/"
    "--output_path" = "s3://acme-lake/silver/orders/"
    "--extra_path"  = "s3://acme-lake/reference/"
    "--other_path"  = "s3://${var.external_bucket}/x/"
  }
}

resource "aws_mwaa_environment" "airflow" {
  name              = "acme-airflow"
  source_bucket_arn = aws_s3_bucket.lake.arn
  dag_s3_path       = "dags"
}
