## DMS

### Instances

| id | class | engine | storage | multi-AZ | status | CPU avg/p95/max % | free mem min | swap max |
|---|---|---|---|---|---|---|---|---|
| acme-dms-1 | dms.r5.large | 3.5.2 | 100 GB | no | available | 22.5 / 48.0 / 61.0 | 6.0 GB | 0 B |

### Tasks

| id | type | status | latency src p95/max s | latency tgt p95/max s | tables errored/suspended | last failure |
|---|---|---|---|---|---|---|
| acme-orders-cdc | full-load-and-cdc | running | 4.0 / 9.0 | 6.0 / 15.0 | 0 / 0 | - |
| acme-dwh-cdc | cdc | running | 12.0 / 30.0 | 20.0 / 60.0 | 0 / 0 | - |

### Top tables by CDC changes (since task start)

| task | table | changes | inserts | updates | deletes | ddls |
|---|---|---|---|---|---|---|
| acme-dwh-cdc | app.orders | 120,500 | 90,000 | 30,000 | 500 | 0 |
| acme-dwh-cdc | app.events | 80,100 | 80,000 | 100 | 0 | 0 |

