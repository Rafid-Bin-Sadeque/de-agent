## Firehose

### Delivery streams

| stream | status | destination | buffer | success | freshness p95 | throttled |
|---|---|---|---|---|---|---|
| acme-clicks | ACTIVE | s3://acme-raw/firehose/clicks/!{timestamp:yyyy/MM/dd}/ | 300 s / 5 MB | 1.0 | 4 min | 0 |

