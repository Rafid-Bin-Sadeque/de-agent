# Redshift audit pack

Generic. Written from public Redshift documentation, with fictional examples only (Acme). It works on provisioned
clusters and Serverless workgroups. Run every query with
`dea query <source> "<sql>" --format json --limit 50` (read-only, one statement, row cap). Prefer the `SYS_*` views
(provisioned and serverless); where a view is missing or not granted, use the `STL/STV/SVL` fallback shown, or record
"not available". Never invent a number: tag every claim V (a query or snapshot line you can cite) or I (inferred).

Inputs: the Redshift source name, the AWS source name if one is configured (`platform/<aws-source>.md`), and the
symptom or question ("should we add a node", "why is the nightly load slow").

History matters. `SYS_QUERY_HISTORY` keeps about 7 days; `STL_*` tables keep roughly 2 to 5 days; a snapshot taken
today shows only what is retained today. For a sizing question take a `dea snapshot` daily for a week and read the
`.dea/snapshots/` history as well (the collector's `workload_24h`, `pressure_24h`, `concurrency_24h` sections), so the
verdict rests on at least 7 days.

## R1. Config and infrastructure

```sql
select version() as version
```
```sql
select count(distinct node) as nodes, count(*) as slices from stv_slices
```
(provisioned only; a failure here usually means Serverless. Check `platform/<source>.md`, section "Cluster", for the
kind and the `aws` rows from describe_clusters / get_workgroup: node type, node count, RPU base and max.)

```sql
select service_class, name, num_query_tasks, query_working_mem, max_execution_time
from stv_wlm_service_class_config where service_class > 5 order by 1
```
Queues, slots and working memory per slot. Also `show enable_result_cache_for_session`.

```sql
select sum(capacity) / 1024 as total_gb, sum(used) / 1024 as used_gb
from stv_partitions where part_begin = 0
```
Disk used against capacity (provisioned). On Serverless storage is managed: use `svv_table_info` sizes instead.

```sql
select user_name, count(*) as sessions from stv_sessions group by 1 order by 2 desc limit 15
```
plus `show max_connections`. Many sessions from one user is a pooling question, not a hardware one.

Look at the AWS collector page too: CloudWatch alarms in ALARM, DMS tasks that feed this cluster (CDC latency,
errored tables), Glue jobs and Firehose streams that load it, and whether anything is failing upstream.

Signals: disk over 70% used, a queue with very few slots next to heavy concurrent work, many idle sessions,
alarms already firing.

## R2. Workload attribution (7 days)

Exec time by user, weekday and weekend separately:
```sql
select coalesce(u.usename, 'user#' || h.user_id::varchar) as user_name,
       case when extract(dow from h.start_time) in (0, 6) then 'weekend' else 'weekday' end as daytype,
       count(*) as queries, round(sum(h.execution_time) / 1000000.0) as exec_s
from sys_query_history h left join pg_user u on u.usesysid = h.user_id
where h.start_time >= dateadd(day, -7, getdate()) and h.user_id > 1
group by 1, 2 order by 4 desc limit 40
```
Fallback: `stl_wlm_query w left join pg_user u on u.usesysid = w.userid`, `w.total_exec_time`,
`w.service_class_start_time`, `w.userid > 1 and w.service_class > 5` (about 2 to 5 days only).

Query mix by type (what the work is):
```sql
select coalesce(query_type, 'UNKNOWN') as query_type, count(*) as queries,
       round(sum(execution_time) / 1000000.0) as exec_s
from sys_query_history where start_time >= dateadd(day, -7, getdate()) and user_id > 1
group by 1 order by 3 desc
```

**Establish what the top user IS before attributing anything to it.** A user name says nothing: it can be an ETL
tool, a replication service, a BI tool, a person, or a scheduler shared by ten teams. Find out with three looks:
```sql
select user_name, application_name, count(*) as sessions
from sys_connection_log
where record_time >= dateadd(day, -7, getdate()) and event = 'initiating session'
group by 1, 2 order by 3 desc limit 30
```
(fallback: `stl_connection_log` with `username`, `recordtime`, `application_name`; also `remotehost` to see which host.)
Then the normalized text of that user's heaviest statements:
```sql
select regexp_replace(regexp_replace(left(h.query_text, 300), '''[^'']*''', '?'), '[0-9]+', '?') as query,
       count(*) as n, round(sum(h.execution_time) / 1000000.0) as exec_s, round(max(h.execution_time) / 1000000.0) as max_s
from sys_query_history h join pg_user u on u.usesysid = h.user_id
where u.usename = '<top user>' and h.start_time >= dateadd(day, -7, getdate())
group by 1 order by 3 desc limit 15
```
and the other places it appears: orchestrator task names, scan catalog (`dea find`, `catalog/`), the repo that issues
that SQL. Only then write "X is the <tool> that <does what>".

Hourly profile (is the load a nightly batch or constant?):
```sql
select date_part(hour, start_time) as hour_utc, count(*) as queries, round(sum(execution_time) / 1000000.0) as exec_s
from sys_query_history where start_time >= dateadd(day, -7, getdate()) and user_id > 1
group by 1 order by 1
```
Signals: one user above 70% of exec time (read what it is), thousands of tiny statements (orchestration or per-row
logic, not capacity), the same statement repeated many times (waste), exec time concentrated in a few hours.

## R3. Pressure signals (7 days)

Queue wait (is anything waiting for capacity?):
```sql
select date_trunc('hour', start_time) as hour_utc, count(*) as queries,
       round(percentile_cont(0.5) within group (order by queue_time) / 1000000.0, 2) as queue_p50_s,
       round(percentile_cont(0.95) within group (order by queue_time) / 1000000.0, 2) as queue_p95_s
from sys_query_history where start_time >= dateadd(day, -7, getdate()) and user_id > 1
group by 1 order by 4 desc limit 24
```
(fallback: `stl_wlm_query.total_queue_time`, `service_class_start_time`.)

Spill to disk (memory pressure, usually a plan or a queue-memory problem):
```sql
select date_trunc('day', start_time) as day_utc, count(distinct query_id) as queries,
       sum(spilled_block_local_disk + spilled_block_remote_disk) as spilled_mb
from sys_query_detail
where start_time >= dateadd(day, -7, getdate()) and (spilled_block_local_disk > 0 or spilled_block_remote_disk > 0)
group by 1 order by 1
```
(fallback: `svl_query_metrics_summary.query_temp_blocks_to_disk` joined to `stl_query` on `query`.)

Commit queue (many small commits serialize behind each other):
```sql
select date_trunc('day', startqueue) as day_utc, count(*) as commits,
       round(percentile_cont(0.95) within group (order by datediff(milliseconds, startqueue, startwork)) / 1000.0, 2) as queue_p95_s
from stl_commit_stats where startqueue >= dateadd(day, -7, getdate()) and startwork >= startqueue
group by 1 order by 1
```
Concurrency: use the collector's `concurrency_24h` section and its history across snapshots.

CPU, I/O and memory per query (proxy when cluster CPU is not otherwise visible; mark any conclusion I):
```sql
select m.service_class, count(*) as queries, round(avg(m.query_cpu_usage_percent), 1) as avg_cpu_pct,
       round(avg(m.cpu_skew), 2) as avg_cpu_skew, round(avg(m.io_skew), 2) as avg_io_skew,
       sum(m.query_temp_blocks_to_disk) as spill_mb
from svl_query_metrics_summary m join stl_query q on q.query = m.query
where q.starttime >= dateadd(day, -5, getdate()) group by 1 order by 2 desc
```
High skew points at distribution keys, not at node count.

Alerts the optimizer logged:
```sql
select left(trim(event), 100) as event, count(*) as n, max(event_time) as last_at
from stl_alert_event_log where event_time >= dateadd(day, -7, getdate()) group by 1 order by 2 desc limit 10
```
Signals: queue p95 over 30 s at a repeatable hour, spill over 1 GB a day, commit queue p95 over 10 s, "nested loop
join" or "very selective filter" alerts on the top queries, skew above 4.

## R4. Inventory and hygiene

```sql
select "schema", "table", size as size_mb, tbl_rows, unsorted, stats_off, skew_rows, diststyle, sortkey1, encoded
from svv_table_info order by size desc limit 50
```
Signals: unsorted over 20% (a vacuum or auto-vacuum is not keeping up), `stats_off` over 10 (analyze is stale),
`skew_rows` over 4 on a table above 1 GB (a poor distribution key), no sort key on a large table that is filtered by a
timestamp, `diststyle ALL` on a large table, `encoded = 'N'`.

```sql
select "schema", count(*) as tables, sum(size) as size_mb from svv_table_info group by 1 order by 3 desc limit 20
```
Staging, temp and test schemas that keep growing are storage waste (each removal is a plan).

Tables nobody scans (provisioned, `stl_scan` keeps only a few days: say so, and never call a table unused from it):
```sql
select i."schema", i."table", i.size as size_mb
from svv_table_info i left join (select distinct tbl from stl_scan where starttime >= dateadd(day, -4, getdate())) s
  on s.tbl = i.table_id
where s.tbl is null order by i.size desc limit 20
```

Load errors and maintenance: collector sections `load_errors_24h` and `maintenance` (auto-vacuum and auto-analyze
activity). Users and groups: `select usename, usesuper from pg_user order by 1` (shared logins make attribution hard).

## Verdict rules (apply in this order; stop at the first layer that explains the symptom)

1. **Bottleneck layer first.** Decide which layer the evidence points at before anything else, in this order:
   - *orchestration*: the cluster is mostly idle while the job is slow. Signs: many short statements, a new
     connection per task, serial steps that could be batched, queue wait near zero, a few busy hours. The fix is in the
     scheduler or the load code, not in the warehouse.
   - *fixable waste*: repeated or redundant statements, full rewrites where an increment would do, missing or wrong sort
     and distribution keys, stale statistics, unsorted tables, spill caused by a bad plan, many tiny commits.
   - *CPU*: sustained high CPU usage per query with a healthy plan mix and low skew.
   - *memory*: spill to disk across many different queries, not one bad one; queue slots too small for the work.
   - *I/O*: long scan times on well-sorted tables, high blocks read with low CPU.
   - *connections*: sessions near the limit, idle sessions, one tool opening hundreds of connections.
   - *storage*: disk used above about 70%, or growth that will cross it within the planning window.
   Each layer needs a number from this pack. "Probably CPU" is not a finding.
2. **Sizing claims need at least 7 days of data, and weekday versus weekend are reported separately.** A node count or
   RPU change is justified only by a pattern that repeats across the week. Say how many days you have; if fewer than 7,
   the sizing question is unanswered, not answered with a caveat.
3. **Never attribute load by user name alone.** Establish what the user is (R2: connection log, application name,
   statement shapes, the repo that issues them) before writing a sentence about who causes the load. If you cannot
   establish it, say "unidentified user <name>".
4. **Hardware last.** Recommend more nodes, a bigger node type or a higher RPU base only after the layers above are ruled
   out with numbers AND the evidence shows saturation (sustained CPU, I/O or queue wait on a healthy workload, repeated
   across weekdays). State the numbers that justify it. A resize is an infrastructure change request, never an inline fix.

Any change (vacuum, analyze, WLM change, key redesign, kill a session) is a plan (`plan` skill), never run by the audit.

## Then-versus-now table
Headline metrics to carry between audits: disk used percent, exec time share of the top user, queue wait p95, daily
spill MB, commit queue p95, peak concurrency, count of tables over 20% unsorted, count with `stats_off` over 10, longest
open transaction. Show each as previous, current, delta, and say which days each covers.
