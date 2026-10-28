## Glue

### Job runs (last 5 per job)

| job | run | state | started | duration | DPU-s | error |
|---|---|---|---|---|---|---|
| load_orders | jr_a1 | SUCCEEDED | 2026-10-20 03:00 | 8 min | 1,920 | - |
| load_orders | jr_a0 | SUCCEEDED | 2026-10-19 03:00 | 7 min | 1,800 | - |

### Crawlers

| crawler | state | last crawl | last status | error |
|---|---|---|---|---|
| bronze_crawler | READY | 2026-10-20 01:00 | SUCCEEDED | - |

### Catalog tables

| table | location | partition keys | partitions | last partition added |
|---|---|---|---|---|
| bronze.orders | s3://acme-lake/bronze/app/orders/ | dt | 31 | 2026-10-20 01:05 |
| bronze.customers | s3://acme-lake/bronze/app/customers/ | - | - | - |
| bronze.unrelated | s3://acme-lake/other/ | - | - | - |
| raw_clicks.clicks | s3://acme-raw/firehose/clicks/ | dt | 12 | 2026-10-20 00:10 |

