resource "google_bigquery_dataset" "analytics" {
  dataset_id = "analytics"
  location   = "EU"
}

resource "google_bigquery_table" "daily_revenue" {
  dataset_id = google_bigquery_dataset.analytics.dataset_id
  table_id   = "daily_revenue"

  view {
    query          = "select order_date, sum(total) from raw.orders group by 1"
    use_legacy_sql = false
  }
}

resource "google_pubsub_topic" "orders" {
  name = "orders-events"
}

resource "google_pubsub_subscription" "orders_to_bq" {
  name  = "orders-to-bq"
  topic = google_pubsub_topic.orders.name

  bigquery_config {
    table = "acme-prod.raw.orders_stream"
  }
}

resource "snowflake_task" "rollup" {
  database      = "ACME"
  schema        = "ANALYTICS"
  name          = "ROLLUP"
  sql_statement = <<-EOT
    insert into analytics.orders_rollup
    select order_date, count(*) from raw.orders group by 1
  EOT
}

resource "databricks_job" "nightly" {
  name = "nightly-export"
}

# a commented-out block must not count:
# resource "aws_s3_bucket" "ghost" { bucket = "ghost" }
