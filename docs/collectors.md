# Collectors: Redshift and AWS (M3)

Everything here is read-only. Sources go in `de-agent.yaml`; `dea snapshot` runs them concurrently and writes
`platform/<source>.md`; `dea diff` shows what changed; `dea scan` turns the latest AWS snapshot into lineage edges.

## Redshift (`type: redshift`)

Two transports. Pick one per source.

```yaml
# PG wire protocol (psycopg or psql, same secret rules as postgres; ssh needs password: pgpass on that host)
- {name: dwh, type: redshift, timeout_seconds: 600,
   conn: {host: acme-dwh.example.internal, port: 5439, dbname: dev, user: readonly_agent, password: pgpass},
   exec: local}                      # or exec: ssh:<host>  (rows come back through `psql --csv`)
# Redshift Data API (boto3, IAM, no password)
- {name: dwh, type: redshift, timeout_seconds: 600,
   data_api: {cluster_id: acme-dwh, database: dev, db_user: readonly_agent},   # or workgroup: acme-wg, secret_arn: "env:ACME_SECRET_ARN"
   aws: {profile: readonly, region: us-east-1}}
```
`aws:` can also sit next to `conn:`: then the `cluster` section adds `describe_clusters` / `get_workgroup` facts.
`timeout_seconds` is the budget for the whole source (Redshift sources default to 600 when unset, other types to 120; set it to override);
`options.section_timeout_seconds` (default 60) caps each query. `options`: `top_tables` (50), `top_queries` (15),
`long_txn_seconds` (600), `heavy_ttl_h` (6, see "Speed" below), `heavy_section_timeout_s` (300: the single workload scan, the spill bound probe and the spill query), `light` (false; `true` skips every heavy section, rendered "skipped (light mode)"). Reuse picks, per heavy section, the latest snapshot where that section succeeded.

Every query is classified with the redshift dialect and refused unless it is a read, then runs inside
`BEGIN READ ONLY; SET LOCAL statement_timeout; <query>; ROLLBACK` (Redshift has no `json_agg` and no `lock_timeout`).
The Data API cannot open a read-only transaction, so it sends `SET statement_timeout` plus the query as one
`BatchExecuteStatement`, and the read-only guarantee there is the SQL classification, the session guard (below) and
the database role: use a read-only `db_user`. All three transports return the same rows (a golden test feeds one
result set through each). `dea query <redshift-source> "<select>"` works on these sources.

Sections prefer `SYS_*` views (provisioned and serverless) and fall back to `STL/STV/SVL` when a view is missing or not
granted. The source used is recorded in `data.meta.sections` and shown in the "Collection" table of the page.

| section | content | signal |
|---|---|---|
| `cluster` | version, provisioned or serverless, nodes and slices, plus the AWS describe call | |
| `workload_24h`, `workload_users`, `workload_types`, `top_queries` | exec-time share by user and query type, top 15 normalized statements, queue wait p50/p95 | one user over 70% of exec time (check what it is before attributing); queue wait p95 over 30 s |
| `pressure_24h`, `concurrency_24h`, `alert_events` | spill to disk, commit queue p95, peak concurrency per hour, optimizer alerts | spill over 1 GB; commit queue p95 over 10 s |
| `tables` | top 50 by size: size, rows, unsorted %, stats_off, skew_rows, diststyle, sortkey1 | unsorted over 20%; stats_off over 10; skew_rows over 4 above 1 GB |
| `locks` | blocked sessions and long open transactions | any blocked session |
| `load_errors_24h`, `maintenance` | COPY errors; auto-vacuum / auto-analyze activity | any load error |

### Speed: single pass, heavy-section reuse, `--deep`
On a busy cluster (a CDC/DMS loader runs thousands of statements an hour) `SYS_QUERY_HISTORY` is the slow part, so the
collector reads it once:

- **Single pass.** One query over the last 24 h, aggregated server-side by user x query type x hour x `generic_query_hash`
  (count, sum and max of execution time, sum of elapsed and queue time, first query id), capped at 50,000 groups (largest
  exec time first; hitting the cap is recorded in `data.meta.workload_truncated` and shown on the page as a lower bound).
  `workload_24h`, `workload_users`, `workload_types`, `top_queries` and `concurrency_24h` are derived from it in Python with
  the same output shapes as before. Sample text for the top 15 hashes is one extra lookup; literals are replaced by `?` in
  Python and a failed lookup leaves `(text unavailable)` without failing the section. If `generic_query_hash` or the view is
  missing the collector falls back to one query per section (the old behaviour); a timeout of the single pass is reported
  per section and is not retried as five scans.
- **Two approximations.** Counts, exec-time sums, shares, rankings and `max` are exact. (1) Queue wait p50/p95 are weighted
  percentiles over the per-group *mean* queue time, so a spike inside a group is averaged out and p95 is a lower bound.
  (2) The hourly `peak` in `concurrency_24h` is the hour's *average* concurrency (total elapsed time of the queries that
  started in that hour / 3600 s, rounded up, at least 1), not the instantaneous maximum; the first and last hours are partial.
- **Heavy sections are reused.** The workload family, `concurrency_24h` and the spill part of `pressure_24h` are "heavy".
  `dea snapshot` reuses them from the latest usable earlier snapshot while their *original* computation is younger than
  `options.heavy_ttl_h` hours (default 6); a reused section carries `cached_from: <iso ts>` in `data.meta.sections`, is shown
  as `(from snapshot N h ago)` on the page and `(reused)` in the Collection table, and `dea diff` never reports it as changed
  (nothing new was observed). A section that failed last time is recomputed. `dea snapshot --deep` ignores the cache and
  recomputes everything. Light sections (tables, locks, alerts, commit queue, load errors, maintenance) always run.
- **Spill.** `SYS_QUERY_DETAIL` is step-level and a plain 24 h filter exceeded the section timeout on a DMS-loaded cluster
  (the query is cancelled by `statement_timeout`; the view itself is readable). Spill is now bounded by the first query id of
  the window, taken from the single pass or a probe (ids grow with time): `SVL_QUERY_METRICS_SUMMARY` first, with `SYS_QUERY_DETAIL` as the fallback, capped at 45 s; serverless has no SVL view.
  Without a known bound the original queries are used.

Timings measured once on a DMS-loaded single-node provisioned cluster (about 700k queries a day, a loaded cluster; numbers
vary by a factor of two with load): before, the five history sections took 56, 56, 59, 109 and 150 s each, spill 207 s
(a 120 s timeout of `SYS_QUERY_DETAIL`, then the SVL fallback) and the whole snapshot 884 s. The single pass costs one scan
(95 to 125 s on that cluster, so on a loaded cluster raise `section_timeout_seconds` to 120 or more; it is reported as
timed out per section otherwise). An id-bounded SVL spill query took 46 s; a bounded `SYS_QUERY_DETAIL` still exceeded
120 s there. A snapshot that reuses the heavy sections ran in 101 s (the remaining time is the light sections, which are
10 to 25 s each on that cluster). Spill needs a lower query id: when the single pass did not provide one, a 20 s probe of
`STL_QUERY` supplies it (not available on serverless: the unbounded queries are used).

The SQL was written from the documented view columns and tested against hand-built fixtures with fictional values.
It has not been recorded from a live cluster: see the optional live check in `docs/design/specs/M3.md`.

## AWS (`type: aws`)

```yaml
- name: aws_main
  type: aws
  aws: {profile: readonly, region: us-east-1}    # or {instance_role: true} with exec: ssh:<host>. Never keys in config.
  exec: local                                    # or ssh:<host>
  services:
    dms: {}                                      # all instances and tasks (capped at 200)
    glue: {jobs: ["*"], crawlers: true, catalog_databases: [bronze, silver]}
    s3: {prefixes: ["s3://acme-lake/bronze/", "s3://acme-raw/firehose/"]}   # ONLY these, never an account-wide listing
    athena: {workgroups: [primary]}
    firehose: {}
    cloudwatch: {alarms: true}
  options: {max_metrics: 300, max_age_h: 26, max_scan_gb: 100}
```
`exec: local` calls AWS with a named profile. `exec: ssh:<host>` ships one self-contained module
(`collectors/aws/_aws_core.py`, stdlib plus boto3) over `ssh <host> python3 -`, uses that host's instance role and
returns JSON. `dea doctor` checks that the remote can `import boto3` and that `sts get-caller-identity` works (the account
is masked as `****1234`). boto3 is optional: `pip install 'de-agent[aws]'`. Without it the core CLI still works and
the aws and Data API paths say so.

| service | page sections | signals |
|---|---|---|
| dms | instances (class, storage, multi-AZ, engine version, CPU, freeable memory, swap), tasks (status, last failure, CDC latency p95/max), errored/suspended tables, top 10 tables by CDC changes | task not running; failure in the last 24 h; CDC latency p95 over 300 s; errored or suspended tables; freeable memory under 10% or swap in 3+ of 24 h; CPU p95 over 85% |
| glue | last 5 runs per job, crawlers, catalog tables (location, partition keys, newest partition) | failed run in 24 h; duration over 2x the median of the previous 4; crawler failed; partitioned table with no partition added in 7 days |
| s3 | per named prefix: objects, bytes, newest object | newest object older than `max_age_h` (26) |
| athena | 24 h query counts, data scanned, top 5 by scan, failures | failed queries; one query scanning over `max_scan_gb` (100) |
| firehose | status, destination, buffering, success, data freshness, throttled records | not ACTIVE; freshness p95 over 2x the buffer interval; success under 1.0 |
| cloudwatch | alarms in ALARM | each alarm |

Notes on what the numbers mean:
- DMS table statistics are cumulative since the task (re)started, not a 24 h window; "CDC changes" is
  inserts + updates + deletes + DDLs from that counter.
- A `s3` prefix that is a whole bucket uses CloudWatch daily storage metrics (size, object count). Other prefixes use a
  capped `ListObjectsV2` (10 pages). When the cap is hit the row is marked incomplete and the freshness signal stays
  silent, because the newest object may be beyond the cap (date-partitioned keys sort oldest first).
- A page over 300 lines splits into `platform/<source>.md` (index and signals) plus `platform/<source>/<service>.md`.

### Read-only enforcement
Every boto3 session built by the collectors carries a botocore `before-call` handler that raises before any network call
unless the operation starts with `Describe`, `List`, `Get`, `BatchGet` or `Head` (plus an explicit allowlist:
`GetMetricData`, `BatchGetQueryExecution`, `DescribeTableStatistics`). Data-reading `Get` operations (`GetObject`,
`GetSecretValue`, `GetQueryResults`, ...) are refused anyway. The Redshift Data API operations are allowed only when
every SQL string passes the read-only classifier. `docs/aws-iam-readonly.json` is the matching minimal IAM policy,
generated from the operation list the collectors use; a test fails when the two drift. Regenerate with
`python -W ignore -m de_agent.collectors.aws._aws_core > docs/aws-iam-readonly.json`. Narrow `Resource` to your
buckets, workgroups and clusters. The last statement of the policy is needed only for the Data API; its credential
actions are scoped to placeholder ARNs (`<region>`, `<account>`, `<your-redshift-secret>`, `<cluster>`,
`<readonly_user>`, ...) that you must replace with your own before attaching it, never `Resource: "*"`.

`redshift-data:BatchExecuteStatement` is not read-only at the IAM level: IAM cannot restrict the SQL it carries.
On the Data API path, read-only depends on the SQL classifier, the session guard and a read-only database user,
so use one.
`secret_guard` additionally blocks reads of `~/.aws/credentials`, `~/.aws/config`, cached SSO tokens and
`aws configure get aws_secret_access_key`.

### Cost per snapshot
CloudWatch is read through batched `GetMetricData` (at most 500 metrics per call, 1 h period over 24 h; S3 daily metrics
use a 72 h window). The number of metrics is reported on the page ("Collection") and capped by `options.max_metrics`
(default 300; entities past the cap simply have no metrics and the skipped count is shown). Rough cost:
GetMetricData is billed per 1,000 metrics requested, so a full 300-metric snapshot is about $0.003 and a daily snapshot
about $1 a year; DMS, Glue, Athena and Firehose describe/list calls are free; S3 listing is $0.005 per 1,000 requests, so
a prefix capped at 10 pages costs about $0.00005. Check current AWS pricing for your region.

## Lineage edges
`dea scan` reads the latest usable `type: aws` snapshot and adds to `catalog/edges.md` / `edges.json`:
- DMS: `source <schema.table> -> dms:<task> -> target <schema.table>` (or an `s3://` node for S3 targets) from the task's
  table-mapping selection rules and `rename` transformations. A wildcard rule is kept as a pattern node (`app.%` ->
  `raw.%`, `pattern: true`) and never expanded. Other transformation actions are listed under Unresolved, not guessed.
- Firehose: `firehose:<stream> -> s3://bucket/prefix` (dynamic `!{...}` parts of the prefix are cut).
- Glue: a catalog table whose `location` overlaps a prefix that DMS or Firehose writes becomes a reader
  `glue:<db.table>` of that prefix, and the table itself becomes a node (`db.table`) other readers can hang off.

`dea edges <table-or-s3-prefix>` and `dea impact <table>` walk these, e.g. `app.orders` -> `dms:...` -> `s3://...` ->
`glue:...`, and `app.orders` -> `dms:...` -> `raw.orders` -> a dbt model in another project.
