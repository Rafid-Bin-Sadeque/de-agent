# Postgres audit pack

Run each query with `dea query <source> "<sql>" --format json --limit 50` (read-only). Where a view needs an extension
or privilege that is missing, record "not available" and use the fallback.

## R1. Workload

Preferred (needs `pg_stat_statements`):
```sql
select left(query, 120) as query, calls, round(total_exec_time::numeric) as total_ms,
       round(mean_exec_time::numeric, 2) as mean_ms, rows
from pg_stat_statements order by total_exec_time desc limit 15
```
Fallback (sampling `pg_stat_activity`; take 3 samples about 10 s apart and compare):
```sql
select state, wait_event_type, wait_event, count(*) as sessions,
       max(extract(epoch from now() - query_start))::int as longest_s
from pg_stat_activity where backend_type = 'client backend' group by 1, 2, 3 order by 4 desc
```
Also: `select datname, xact_commit, xact_rollback, blks_hit, blks_read, temp_files, temp_bytes, deadlocks
from pg_stat_database where datname = current_database()`.
Signals: a few statements dominating total time (fix the query), high `temp_bytes` (work_mem or a bad plan),
cache hit under 99% on an OLTP workload (memory or a scan pattern).

## R2. Tables

```sql
select schemaname || '.' || relname as tbl, n_live_tup, n_dead_tup,
       round(100.0 * n_dead_tup / nullif(n_live_tup + n_dead_tup, 0), 1) as dead_pct,
       seq_scan, seq_tup_read, idx_scan, last_autovacuum, pg_size_pretty(pg_total_relation_size(relid)) as size
from pg_stat_user_tables order by n_dead_tup desc limit 20
```
```sql
select schemaname || '.' || relname as tbl, indexrelname, idx_scan,
       pg_size_pretty(pg_relation_size(indexrelid)) as size
from pg_stat_user_indexes where idx_scan = 0 order by pg_relation_size(indexrelid) desc limit 20
```
Seq-scan heavy: tables with `seq_tup_read` large and `idx_scan` low relative to size.
Signals: dead_pct over 20% with a stale `last_autovacuum` (vacuum is not keeping up), big tables read by seq scan
(missing index), unused indexes (candidates to drop, each is a plan, never an inline DROP).

## R3. Locks, connections, long transactions

```sql
select count(*) filter (where state = 'active') as active, count(*) filter (where state = 'idle in transaction') as idle_in_txn,
       count(*) as total, current_setting('max_connections')::int as max_conn
from pg_stat_activity where backend_type = 'client backend'
```
```sql
select pid, state, wait_event_type, now() - xact_start as xact_age, left(query, 100) as query
from pg_stat_activity where xact_start is not null and backend_type = 'client backend'
order by xact_start limit 10
```
```sql
select blocked.pid as blocked_pid, blocking.pid as blocking_pid, left(blocked.query, 80) as blocked_query
from pg_stat_activity blocked join pg_stat_activity blocking on blocking.pid = any(pg_blocking_pids(blocked.pid))
```
Signals: blocked sessions (find the blocker, not more CPU), `idle in transaction` sessions holding old snapshots,
connections near `max_connections` (pooling, not hardware).

## R4. Settings versus size

```sql
select name, setting, unit from pg_settings where name in
 ('shared_buffers','work_mem','maintenance_work_mem','effective_cache_size','max_connections',
  'autovacuum','autovacuum_vacuum_scale_factor','random_page_cost','max_wal_size','checkpoint_timeout')
```
```sql
select pg_size_pretty(pg_database_size(current_database())) as db_size
```
Compare `shared_buffers` and `effective_cache_size` with database size and the instance memory the human reports.
Signals: defaults left on a large instance, `autovacuum` off or too lazy for big tables.

## Verdict rules (apply in this order; stop at the first layer that explains the symptom)
1. **Workload / queries**: one or a few statements dominate time, or temp spill is high. Action: fix the query or its
   index. Hardware will not help.
2. **Tables / indexes**: bloat, stale vacuum, seq scans on large tables, missing indexes. Action: a maintenance or
   index plan.
3. **Locks / connections**: blockers, idle-in-transaction, connection saturation. Action: fix the holder, add pooling.
4. **Settings**: memory or planner settings far from the data size. Action: a config plan.
5. **Hardware last**: recommend more CPU or memory only if layers 1 to 4 are clean AND the evidence shows saturation
   (sustained CPU or IO wait with a healthy plan mix, cache hit falling because the working set outgrew memory).
   State the numbers that justify it.

## Then-versus-now table
Headline metrics to carry between audits: database size, dead_pct on the top 3 tables, top query mean_ms, cache hit
percent, connections used / max, longest transaction age, unused index count. Show each as previous, current, delta.
