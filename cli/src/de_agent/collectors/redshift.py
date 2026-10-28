"""Redshift collector (M3 section 1): provisioned and serverless, read-only, three transports, SYS_ views first.

Transports (see sqlexec.run_redshift): the PG wire protocol via psycopg or `psql --csv` (the same secret rules as
postgres), or the Redshift Data API via boto3 (IAM, no password). Every query is classified with the redshift dialect and
runs inside `BEGIN READ ONLY; SET LOCAL statement_timeout; <query>; ROLLBACK` (Data API: one batch with a session-level
SET). Each section prefers a `SYS_*` view (provisioned and serverless) and falls back to `STL/STV/SVL` when it is missing;
the source used is recorded per section in `data.meta.sections`.

NOTE ON THE SQL: written from the documented view columns, not recorded from a live cluster. The fixtures used by the
tests are hand-built from those columns with fictional values (see tests/test_redshift_collector.py).
"""
from __future__ import annotations

import copy
import hashlib
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from de_agent.collectors.base import CollectorError, RunContext, utc_iso

OPTION_KEYS = {"top_tables", "top_queries", "section_timeout_seconds", "long_txn_seconds", "heavy_ttl_h",
               "heavy_section_timeout_s", "light"}
DEFAULT_SECTION_TIMEOUT_S = 60
DEFAULT_HEAVY_TTL_H = 6
SINGLE_PASS_CAP = 50_000
SPILL_DETAIL_TIMEOUT_S = 45
DEFAULT_HEAVY_TIMEOUT_S = 300
# sections that rescan big system tables: reused from the previous snapshot while younger than options.heavy_ttl_h
WORKLOAD_SECTIONS = ("workload_24h", "workload_users", "workload_types", "top_queries", "concurrency_24h")
DEFAULT_LONG_TXN_S = 600
WINDOW_H = 24
SINCE = f"dateadd(hour, -{WINDOW_H}, getdate())"
# a missing view fails with one of these messages; anything else (permission, timeout) is reported, not "fallen back"
_NORM = (
    "regexp_replace(regexp_replace(regexp_replace(left({col}, 500), '''[^'']*''', '?'), '[0-9]+', '?'), "
    "'[[:space:]]+', ' ')"
)


def validate_options(options: dict[str, Any]) -> None:
    unknown = set(options) - OPTION_KEYS
    if unknown:
        raise ValueError(f"unknown redshift options: {', '.join(sorted(unknown))}")
    if "light" in options and not isinstance(options["light"], bool):
        raise ValueError("options.light must be true or false")
    for key in OPTION_KEYS - {"light"}:
        if key in options and not (isinstance(options[key], int) and not isinstance(options[key], bool)
                                   and options[key] > 0):
            raise ValueError(f"options.{key} must be a positive integer")


def validate_source(source: Any) -> None:
    from de_agent.collectors.aws import validate_aws_block

    if source.services:
        raise ValueError("redshift sources take no `services:`; use a separate `type: aws` source")
    if (source.conn is None) == (source.data_api is None):
        raise ValueError("redshift sources need exactly one of `conn:` (PG wire) or `data_api:` (Redshift Data API)")
    if source.data_api is not None:
        da = source.data_api
        if not isinstance(da, dict):
            raise ValueError("`data_api:` must be a mapping")
        unknown = set(da) - {"cluster_id", "workgroup", "database", "db_user", "secret_arn"}
        if unknown:
            raise ValueError(f"unknown data_api keys: {', '.join(sorted(unknown))}")
        if bool(da.get("cluster_id")) == bool(da.get("workgroup")):
            raise ValueError("data_api needs exactly one of cluster_id (provisioned) or workgroup (serverless)")
        if not da.get("database"):
            raise ValueError("data_api.database is required")
        if da.get("db_user") and da.get("secret_arn"):
            raise ValueError("data_api: give db_user or secret_arn, not both")
        if da.get("secret_arn") and not str(da["secret_arn"]).startswith(("env:", "arn:")):
            raise ValueError("data_api.secret_arn must be env:NAME (holding the ARN) or an arn:")
        if source.aws is None:
            raise ValueError("data_api needs an `aws:` block ({profile, region})")
        if source.exec != "local":
            raise ValueError("data_api runs from this machine (exec: local); use `conn:` with exec ssh:<host> instead")
    if source.aws is not None:
        validate_aws_block(source.aws, source.exec)


# ---- queries: (name of the source view, sql, declared column types) ---------------------------------------------

@dataclass
class Variant:
    source: str
    sql: str
    types: dict[str, str] = field(default_factory=dict)
    timeout: float | None = None  # per-variant cap (a slow primary must leave time for its fallback)


Q_VERSION = Variant("version()", "select version() as version")
Q_SLICES = Variant("stv_slices", "select count(distinct node) as nodes, count(*) as slices from stv_slices",
                   {"nodes": "int", "slices": "int"})
Q_SERVERLESS = Variant("sys_serverless_usage", "select count(*) as n from sys_serverless_usage", {"n": "int"})

_SYS_FILTER = f"h.start_time >= {SINCE} and h.user_id > 1"
_STL_FILTER = f"w.service_class_start_time >= {SINCE} and w.userid > 1 and w.service_class > 5"

TYPES_USERS = {"user_id": "int", "query_count": "int", "exec_s": "float"}
# pg_user lives on the leader node only: joining it to a compute-node view fails on a live cluster, so names are
# resolved from a separate query and merged here
Q_USERNAMES = Variant("pg_user", "select usesysid as user_id, usename as user_name from pg_user",
                      {"user_id": "int"})
TYPES_TYPES = {"query_count": "int", "exec_s": "float"}
TYPES_TOP = {"n": "int", "total_exec_s": "float", "avg_exec_s": "float", "max_exec_s": "float"}
TYPES_TOTALS = {"queries": "int", "exec_s": "float", "queue_p50_s": "float", "queue_p95_s": "float"}


def v_users() -> list[Variant]:
    return [
        Variant("sys_query_history", f"""
select h.user_id as user_id, count(*) as query_count,
       round(coalesce(sum(h.execution_time), 0) / 1000000.0, 1) as exec_s
from sys_query_history h
where {_SYS_FILTER}
group by 1 order by 3 desc, 1 limit 20""", TYPES_USERS),
        Variant("stl_wlm_query", f"""
select w.userid as user_id, count(*) as query_count,
       round(coalesce(sum(w.total_exec_time), 0) / 1000000.0, 1) as exec_s
from stl_wlm_query w
where {_STL_FILTER}
group by 1 order by 3 desc, 1 limit 20""", TYPES_USERS),
    ]


def v_types() -> list[Variant]:
    return [
        Variant("sys_query_history", f"""
select coalesce(h.query_type, 'UNKNOWN') as query_type, count(*) as query_count,
       round(coalesce(sum(h.execution_time), 0) / 1000000.0, 1) as exec_s
from sys_query_history h
where {_SYS_FILTER}
group by 1 order by 3 desc, 1 limit 20""", TYPES_TYPES),
        Variant("stl_query+stl_wlm_query", f"""
select upper(split_part(ltrim(q.querytxt), ' ', 1)) as query_type, count(*) as query_count,
       round(coalesce(sum(w.total_exec_time), 0) / 1000000.0, 1) as exec_s
from stl_wlm_query w join stl_query q on q.query = w.query
where {_STL_FILTER}
group by 1 order by 3 desc, 1 limit 20""", TYPES_TYPES),
    ]


def v_totals() -> list[Variant]:
    return [
        Variant("sys_query_history", f"""
select count(*) as queries, round(coalesce(sum(h.execution_time), 0) / 1000000.0, 1) as exec_s,
       round(percentile_cont(0.5) within group (order by h.queue_time) / 1000000.0, 2) as queue_p50_s,
       round(percentile_cont(0.95) within group (order by h.queue_time) / 1000000.0, 2) as queue_p95_s
from sys_query_history h
where {_SYS_FILTER}""", TYPES_TOTALS),
        Variant("stl_wlm_query", f"""
select count(*) as queries, round(coalesce(sum(w.total_exec_time), 0) / 1000000.0, 1) as exec_s,
       round(percentile_cont(0.5) within group (order by w.total_queue_time) / 1000000.0, 2) as queue_p50_s,
       round(percentile_cont(0.95) within group (order by w.total_queue_time) / 1000000.0, 2) as queue_p95_s
from stl_wlm_query w
where {_STL_FILTER}""", TYPES_TOTALS),
    ]


def v_top_queries(top: int) -> list[Variant]:
    sys_norm = _NORM.format(col="h.query_text")
    stl_norm = _NORM.format(col="q.querytxt")
    return [
        Variant("sys_query_history", f"""
select {sys_norm} as query, count(*) as n,
       round(coalesce(sum(h.execution_time), 0) / 1000000.0, 1) as total_exec_s,
       round(coalesce(avg(h.execution_time), 0) / 1000000.0, 2) as avg_exec_s,
       round(coalesce(max(h.execution_time), 0) / 1000000.0, 1) as max_exec_s
from sys_query_history h
where {_SYS_FILTER}
group by 1 order by 3 desc, 1 limit {int(top)}""", TYPES_TOP),
        Variant("stl_query+stl_wlm_query", f"""
select {stl_norm} as query, count(*) as n,
       round(coalesce(sum(w.total_exec_time), 0) / 1000000.0, 1) as total_exec_s,
       round(coalesce(avg(w.total_exec_time), 0) / 1000000.0, 2) as avg_exec_s,
       round(coalesce(max(w.total_exec_time), 0) / 1000000.0, 1) as max_exec_s
from stl_wlm_query w join stl_query q on q.query = w.query
where {_STL_FILTER}
group by 1 order by 3 desc, 1 limit {int(top)}""", TYPES_TOP),
    ]


TYPES_SPILL = {"spilled_queries": "int", "spilled_mb": "float"}


def v_spill(min_query_id: int | None = None) -> list[Variant]:
    """Spill over the 24 h window. SYS_QUERY_DETAIL is step-level and huge: a plain 24 h time filter timed out live, so
    when the first query id of the window is known (single pass, or a cheap STL_QUERY probe) the scan is bounded by that id
    (ids grow with time). Without a bound the original (slow) queries are kept."""
    if min_query_id is not None:
        lo = int(min_query_id)
        # measured live: the id-bounded SVL summary answers in under a minute where the bounded SYS_QUERY_DETAIL still
        # exceeded the section timeout, so the SVL view goes first; serverless has no SVL view and falls through to SYS
        return [
            Variant("svl_query_metrics_summary", f"""
select count(*) as spilled_queries, coalesce(sum(m.query_temp_blocks_to_disk), 0) as spilled_mb
from svl_query_metrics_summary m
where m.query >= {lo} and m.userid > 1 and m.query_temp_blocks_to_disk > 0""", TYPES_SPILL),
            Variant("sys_query_detail", f"""
select count(distinct d.query_id) as spilled_queries,
       coalesce(sum(d.spilled_block_local_disk + d.spilled_block_remote_disk), 0) as spilled_mb
from sys_query_detail d
where d.query_id >= {lo} and (d.spilled_block_local_disk > 0 or d.spilled_block_remote_disk > 0)""", TYPES_SPILL,
                    timeout=SPILL_DETAIL_TIMEOUT_S),
        ]
    return [
        Variant("sys_query_detail", f"""
select count(distinct d.query_id) as spilled_queries,
       coalesce(sum(d.spilled_block_local_disk + d.spilled_block_remote_disk), 0) as spilled_mb
from sys_query_detail d
where d.start_time >= {SINCE} and (d.spilled_block_local_disk > 0 or d.spilled_block_remote_disk > 0)""", TYPES_SPILL),
        Variant("svl_query_metrics_summary", f"""
select count(*) as spilled_queries, coalesce(sum(m.query_temp_blocks_to_disk), 0) as spilled_mb
from svl_query_metrics_summary m join stl_query q on q.query = m.query
where q.starttime >= {SINCE} and m.query_temp_blocks_to_disk > 0""", TYPES_SPILL),
    ]


# ---- single pass over SYS_QUERY_HISTORY -------------------------------------------------------------------------
# One scan, aggregated server-side at user x query_type x hour x generic_query_hash. Python derives workload_24h,
# workload_users, workload_types, top_queries and concurrency_24h from it. Four of the five outputs are exact; two are
# approximations (documented in docs/collectors.md): queue wait p50/p95 are weighted percentiles of per-group MEAN queue
# time, and the hourly "peak" concurrency is the hour's average concurrency (sum of elapsed time of the queries that
# started in that hour / 3600 s) rounded up, not the instantaneous maximum.
TYPES_SINGLE = {"user_id": "int", "hour": "ts", "n": "int", "exec_s": "float", "max_exec_s": "float",
                "elapsed_s": "float", "queue_s": "float", "max_queue_s": "float", "min_query_id": "int"}


def v_single_pass() -> Variant:
    return Variant("sys_query_history (single pass)", f"""
select h.user_id as user_id, coalesce(h.query_type, 'UNKNOWN') as query_type, date_trunc('hour', h.start_time) as hour,
       h.generic_query_hash as qhash, count(*) as n,
       round(coalesce(sum(h.execution_time), 0) / 1000000.0, 3) as exec_s,
       round(coalesce(max(h.execution_time), 0) / 1000000.0, 3) as max_exec_s,
       round(coalesce(sum(h.elapsed_time), 0) / 1000000.0, 3) as elapsed_s,
       round(coalesce(sum(h.queue_time), 0) / 1000000.0, 3) as queue_s,
       round(coalesce(max(h.queue_time), 0) / 1000000.0, 3) as max_queue_s,
       min(h.query_id) as min_query_id
from sys_query_history h
where {_SYS_FILTER}
group by 1, 2, 3, 4
order by 6 desc, 1, 2, 3, 4
limit {SINGLE_PASS_CAP + 1}""", TYPES_SINGLE)


_HASH_OK = re.compile(r"^[A-Za-z0-9_+/=-]{1,100}$")  # hashes may be base64 text


def v_top_text(hashes: list[str]) -> Variant:
    """Sample text (first 500 chars, literals are stripped in Python) for the given generic query hashes."""
    ids = ", ".join("'" + h + "'" for h in hashes if _HASH_OK.match(h))
    return Variant("sys_query_history (query text)", f"""
select h.generic_query_hash as qhash, min(left(h.query_text, 500)) as query
from sys_query_history h
where h.start_time >= {SINCE} and h.generic_query_hash in ({ids or "''"})
group by 1""", {})


def _is_timeout(msg: str) -> bool:
    m = msg.lower()
    return "cancel" in m or "timed out" in m or "timeout" in m


def _norm_text(text: Any) -> str:
    s = re.sub(r"'[^']*'", "?", str(text or "")[:500])
    return re.sub(r"\s+", " ", re.sub(r"[0-9]+", "?", s)).strip()


def _weighted_pct(groups: list[tuple[float, int]], q: float) -> float:
    """Smallest value whose cumulative weight reaches q of the total weight (groups: (value, weight))."""
    total = sum(w for _, w in groups)
    if not total:
        return 0.0
    acc = 0
    for v, w in sorted(groups):
        acc += w
        if acc >= q * total:
            return v
    return groups[-1][0]


def derive_workload(rows: list[dict[str, Any]], top: int) -> dict[str, Any]:
    """Old per-section row shapes (the ones the sections' post-processors expect) from the single-pass groups."""
    n_tot, exec_tot = 0, 0.0
    users: dict[Any, list[float]] = {}
    types: dict[str, list[float]] = {}
    hashes: dict[str, list[float]] = {}
    hours: dict[Any, float] = {}
    queue: list[tuple[float, int]] = []
    min_id: int | None = None
    for r in rows:
        n = int(r.get("n") or 0)
        ex = float(r.get("exec_s") or 0)
        n_tot += n
        exec_tot += ex
        for d, k in ((users, r.get("user_id")), (types, r.get("query_type") or "UNKNOWN")):
            a = d.setdefault(k, [0, 0.0])
            a[0] += n
            a[1] += ex
        h = str(r.get("qhash") or "").strip()
        a = hashes.setdefault(h, [0, 0.0, 0.0])
        a[0] += n
        a[1] += ex
        a[2] = max(a[2], float(r.get("max_exec_s") or 0))
        if r.get("hour") is not None:
            hours[r["hour"]] = hours.get(r["hour"], 0.0) + float(r.get("elapsed_s") or 0)
        if n:
            queue.append((float(r.get("queue_s") or 0) / n, n))
        if r.get("min_query_id") is not None:
            min_id = int(r["min_query_id"]) if min_id is None else min(min_id, int(r["min_query_id"]))

    def rank(d: dict[Any, list[float]], k: int) -> list[tuple[Any, list[float]]]:
        return sorted(d.items(), key=lambda kv: (-kv[1][1], str(kv[0])))[:k]

    return {
        "totals": {"queries": n_tot, "exec_s": round(exec_tot, 1), "queue_p50_s": round(_weighted_pct(queue, 0.5), 2),
                   "queue_p95_s": round(_weighted_pct(queue, 0.95), 2)},
        "users": [{"user_id": k, "query_count": int(v[0]), "exec_s": round(v[1], 1)} for k, v in rank(users, 20)],
        "types": [{"query_type": k, "query_count": int(v[0]), "exec_s": round(v[1], 1)} for k, v in rank(types, 20)],
        "top": [{"qhash": k, "n": int(v[0]), "total_exec_s": round(v[1], 1),
                 "avg_exec_s": round(v[1] / v[0], 2) if v[0] else 0.0, "max_exec_s": round(v[2], 1)}
                for k, v in rank(hashes, top)],
        "concurrency": [{"hour": h, "peak": max(1, math.ceil(e / 3600.0 - 1e-9))} for h, e in sorted(hours.items(), key=lambda kv: str(kv[0]))],
        "min_query_id": min_id,
    }


MINQID_TIMEOUT_S = 20
Q_MINQID = Variant("stl_query", f"select min(q.query) as min_query_id from stl_query q where q.starttime >= {SINCE}",
                   {"min_query_id": "int"}, timeout=MINQID_TIMEOUT_S)


def v_commit_queue() -> list[Variant]:
    # no SYS_ equivalent: commit queue wait lives in STL_COMMIT_STATS (provisioned only)
    return [Variant("stl_commit_stats", f"""
select round(percentile_cont(0.95) within group (order by datediff(milliseconds, c.startqueue, c.startwork)) / 1000.0, 2)
         as commit_queue_p95_s
from stl_commit_stats c
where c.startqueue >= {SINCE} and c.startwork >= c.startqueue""", {"commit_queue_p95_s": "float"})]


TYPES_CONC = {"hour": "ts", "peak": "int"}


def v_concurrency() -> list[Variant]:
    def build(view: str, start: str, end: str, flt: str) -> str:
        return f"""
with ev as (
  select greatest(h.{start}, {SINCE}) as t, 1 as d from {view} h where h.{end} >= {SINCE} and {flt}
  union all
  select least(h.{end}, getdate()) as t, -1 as d from {view} h where h.{end} >= {SINCE} and {flt}),
run as (select t, sum(d) over (order by t, d rows unbounded preceding) as running from ev)
select date_trunc('hour', t) as hour, max(running) as peak from run where t >= {SINCE} group by 1 order by 1"""

    return [
        Variant("sys_query_history", build("sys_query_history", "start_time", "end_time", "h.user_id > 1"), TYPES_CONC),
        Variant("stl_wlm_query", build("stl_wlm_query", "service_class_start_time", "service_class_end_time",
                                       "h.userid > 1 and h.service_class > 5"), TYPES_CONC),
    ]


def v_alerts() -> list[Variant]:
    return [Variant("stl_alert_event_log", f"""
select left(trim(a.event), 100) as event, count(*) as n, max(a.event_time) as last_at
from stl_alert_event_log a
where a.event_time >= {SINCE}
group by 1 order by 2 desc, 1 limit 10""", {"n": "int", "last_at": "ts"})]


def q_tables(top: int) -> list[Variant]:
    return [Variant("svv_table_info", f"""
select t."schema" as "schema", t."table" as name, t.size as size_mb, t.tbl_rows as rows, t.unsorted as unsorted_pct,
       t.stats_off as stats_off, t.skew_rows as skew_rows, t.diststyle as diststyle, t.sortkey1 as sortkey1
from svv_table_info t
order by t.size desc nulls last, 1, 2
limit {int(top)}""", {"size_mb": "int", "rows": "int", "unsorted_pct": "float", "stats_off": "float",
                      "skew_rows": "float"})]


def v_blocked() -> list[Variant]:
    types = {"pid": "int", "blocking_pid": "int", "wait_s": "int"}
    return [
        Variant("svv_transactions", """
select w.pid as pid, h.pid as blocking_pid, w.txn_owner as user_name, trim(w.lockable_object_type) as object_type,
       w.relation as relation_id, datediff(second, w.txn_start, getdate()) as wait_s
from svv_transactions w join svv_transactions h on h.relation = w.relation and h.xid <> w.xid and h.granted
where not w.granted
order by 6 desc, 1 limit 50""", types),
        Variant("stv_locks", """
select w.lock_owner_pid as pid, h.lock_owner_pid as blocking_pid, trim(w.lock_status) as object_type,
       w.table_id as relation_id, datediff(second, w.lock_owner_start_ts, getdate()) as wait_s
from stv_locks w join stv_locks h on h.table_id = w.table_id and h.lock_owner_pid <> w.lock_owner_pid
where w.lock_status like 'Waiting%' and h.lock_status like 'Holding%'
order by 5 desc, 1 limit 50""", types),
    ]


def q_long_txns(min_s: int) -> list[Variant]:
    return [Variant("svv_transactions", f"""
select t.pid as pid, t.txn_owner as user_name, datediff(second, t.txn_start, getdate()) as age_s
from svv_transactions t
where t.txn_start < dateadd(second, -{int(min_s)}, getdate())
group by t.pid, t.txn_owner, t.txn_start
order by 3 desc, 1 limit 20""", {"pid": "int", "age_s": "int"})]


def v_load_errors() -> list[Variant]:
    types = {"n": "int"}
    return [
        Variant("sys_load_error_detail", f"""
select coalesce(trim(i."schema") || '.' || trim(i."table"), 'table#' || e.table_id::varchar) as table_name,
       trim(e.column_name) as column_name, left(trim(e.error_message), 200) as error, count(*) as n
from sys_load_error_detail e left join svv_table_info i on i.table_id = e.table_id
where e.start_time >= {SINCE}
group by 1, 2, 3 order by 4 desc, 1 limit 20""", types),
        Variant("stl_load_errors", f"""
select coalesce(trim(i."schema") || '.' || trim(i."table"), 'table#' || e.tbl::varchar) as table_name,
       trim(e.colname) as column_name, left(trim(e.err_reason), 200) as error, count(*) as n
from stl_load_errors e left join svv_table_info i on i.table_id = e.tbl
where e.starttime >= {SINCE}
group by 1, 2, 3 order by 4 desc, 1 limit 20""", types),
    ]


def v_maintenance() -> list[Variant]:
    types = {"runs": "int", "tables": "int", "last_at": "ts"}
    return [
        Variant("sys_vacuum_history+sys_analyze_history", f"""
select 'vacuum' as operation, count(*) as runs, count(distinct table_id) as tables, max(end_time) as last_at
from sys_vacuum_history where start_time >= {SINCE}
union all
select 'analyze' as operation, count(*) as runs, count(distinct table_id) as tables, max(end_time) as last_at
from sys_analyze_history where start_time >= {SINCE}""", types),
        Variant("stl_vacuum+stl_analyze", f"""
select 'vacuum' as operation, count(*) as runs, count(distinct table_id) as tables, max(eventtime) as last_at
from stl_vacuum where eventtime >= {SINCE} and status like 'Finished%'
union all
select 'analyze' as operation, count(*) as runs, count(distinct table_id) as tables, max(endtime) as last_at
from stl_analyze where starttime >= {SINCE} and status = 'Full'""", types),
    ]


def all_queries(top_tables: int = 50, top_queries: int = 15, long_txn_s: int = DEFAULT_LONG_TXN_S) -> list[Variant]:
    """Every SQL the collector can send (used by the tests: each must parse as redshift and classify as a read)."""
    out = [Q_VERSION, Q_SLICES, Q_SERVERLESS, Q_USERNAMES]
    out += [v_single_pass(), v_top_text(["abc123"]), Q_MINQID]
    for group in (v_users(), v_types(), v_totals(), v_top_queries(top_queries), v_spill(), v_spill(1), v_commit_queue(),
                  v_concurrency(), v_alerts(), q_tables(top_tables), v_blocked(), q_long_txns(long_txn_s),
                  v_load_errors(), v_maintenance()):
        out += group
    return out


def normalize_query(text: str) -> tuple[str, str]:
    """(stable key, text <= 200 chars) from the SQL-normalized query text."""
    norm = " ".join(str(text or "").split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10], norm[:200]


class RedshiftCollector:
    type = "redshift"
    schema_version = 1
    list_keys = {
        "workload_users": ["user"], "workload_types": ["query_type"], "top_queries": ["key"],
        "concurrency_24h": ["hour"], "alert_events": ["event"], "tables": ["schema", "name"],
        "locks": ["kind", "pid"], "load_errors_24h": ["table", "column", "error"], "maintenance": ["operation"],
    }
    windowed_sections = {"concurrency_24h", "alert_events", "load_errors_24h"}
    heavy_sections = set(WORKLOAD_SECTIONS) | {"pressure_spill"}
    ranking_sections = {"top_queries", "workload_users", "workload_types"}
    volatile_fields = {
        "query", "exec_s", "share_pct", "query_count", "n", "total_exec_s", "avg_exec_s", "max_exec_s", "last_at",
        "wait_s", "age_s", "peak", "runs", "unsorted_pct", "stats_off", "skew_rows", "queries", "queue_p50_s",
        "spilled_queries", "spilled_mb", "commit_queue_p95_s",
    }
    validate_options = staticmethod(validate_options)
    validate_source = staticmethod(validate_source)

    @staticmethod
    def reused_data(data: dict[str, Any]) -> tuple[set[str], dict[str, set[str]]]:
        """(data keys, {data key: fields}) that this result copied from an earlier snapshot: nothing new was observed there."""
        secs = (data.get("meta") or {}).get("sections") or {}
        keys = {n for n in WORKLOAD_SECTIONS if (secs.get(n) or {}).get("cached_from")}
        fields = {"pressure_24h": {"spilled_queries", "spilled_mb"}} if (secs.get("pressure_spill") or {}).get("cached_from") else {}
        return keys, fields

    @staticmethod
    def best_prior(results: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Heavy-section reuse source from earlier results (newest first, failed ones already dropped): the newest
        result, with every heavy section that did not succeed there grafted from the latest older result where THAT
        section succeeded, so one partial snapshot never hides an older good result."""
        if not results:
            return None
        base = copy.deepcopy(results[0])
        data = base.setdefault("data", {})
        meta = data.setdefault("meta", {})
        secs = meta.setdefault("sections", {})

        for older in results[1:]:
            od = older.get("data") or {}
            osecs = (od.get("meta") or {}).get("sections") or {}
            for n in WORKLOAD_SECTIONS:
                if not (secs.get(n) or {}).get("source") and (osecs.get(n) or {}).get("source") and n in od:
                    data[n] = od[n]
                    secs[n] = osecs[n]
                    for k in ("min_query_id", "workload_truncated"):
                        if k in (od.get("meta") or {}) and n == "workload_24h":
                            meta[k] = od["meta"][k]
            if not (secs.get("pressure_spill") or {}).get("source") and (osecs.get("pressure_spill") or {}).get("source") \
                    and "spilled_queries" in (od.get("pressure_24h") or {}):
                secs["pressure_spill"] = osecs["pressure_spill"]
                pr = data.setdefault("pressure_24h", {})
                for k in ("spilled_queries", "spilled_mb"):
                    pr[k] = od["pressure_24h"].get(k)
        return base

    def ping(self, source: Any, ctx: RunContext) -> str:
        self._q(source, ctx, Variant("ping", "select 1 as ok", {"ok": "int"}), 15)
        return "select 1 ok" + (" via Data API" if source.data_api else "")

    # -- plumbing ----------------------------------------------------------------------------------------------

    def _q(self, source: Any, ctx: RunContext, v: Variant, section_timeout: float) -> list[dict[str, Any]]:
        left = ctx.remaining()
        if left <= 0:
            raise CollectorError("source timeout budget exhausted")
        t = max(1.0, min(float(section_timeout), left))
        if ctx.query_fn:  # injectable for tests: (conn, sql, exec, timeout) like the postgres collector
            return ctx.query_fn(source.conn, v.sql, source.exec, t)
        from de_agent import sqlexec

        return sqlexec.run_redshift(source, v.sql, v.types, t)

    def collect(self, source: Any, ctx: RunContext) -> dict[str, Any]:
        opts = source.options
        sect_t = int(opts.get("section_timeout_seconds", DEFAULT_SECTION_TIMEOUT_S))
        top_t, top_q = int(opts.get("top_tables", 50)), int(opts.get("top_queries", 15))
        long_txn = int(opts.get("long_txn_seconds", DEFAULT_LONG_TXN_S))
        heavy_t = int(opts.get("heavy_section_timeout_s", DEFAULT_HEAVY_TIMEOUT_S))
        light = bool(opts.get("light", False))
        data: dict[str, Any] = {}
        errors: list[str] = []
        sections: dict[str, dict[str, Any]] = {}

        def run(name: str, variants: list[Variant], post: Any = lambda r: r, store: bool = True,
                base_t: float | None = None) -> Any:
            """First variant that works wins; a missing view falls back, the failure of the last one is reported."""
            t0 = time.monotonic()
            failures: list[str] = []
            for v in variants:
                try:
                    rows = self._q(source, ctx, v, min(base_t or sect_t, v.timeout) if v.timeout else (base_t or sect_t))
                    out = post(rows)
                except CollectorError as ex:
                    failures.append(f"{v.source}: {ex}")
                    continue
                sections[name] = {"source": v.source, "seconds": round(time.monotonic() - t0, 2),
                                  "computed_at": utc_iso(ctx.now),
                                  **({"fallback_from": [f.split(":")[0] for f in failures]} if failures else {})}
                if store:
                    data[name] = out
                return out
            sections[name] = {"source": None, "seconds": round(time.monotonic() - t0, 2)}
            errors.append(f"{name}: " + "; ".join(failures))
            return None

        # cluster
        self._cluster(source, ctx, data, errors, sections, sect_t)

        ttl_h = float(opts.get("heavy_ttl_h", DEFAULT_HEAVY_TTL_H))
        now = ctx.now or datetime.now(timezone.utc)
        prior_data = (ctx.prior or {}).get("data") or {} if not ctx.deep else {}
        prior_secs = (prior_data.get("meta") or {}).get("sections") or {}

        def reusable(names: tuple[str, ...], present: bool) -> dict[str, dict[str, Any]] | None:
            """Prior section meta for every name when all were computed successfully under `ttl_h` ago, else None."""
            if not present:
                return None
            out: dict[str, dict[str, Any]] = {}
            for n in names:
                s = prior_secs.get(n)
                try:
                    comp = datetime.strptime(str((s or {}).get("computed_at")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                except ValueError:
                    return None
                age = (now - comp).total_seconds() / 3600.0
                if not s or s.get("source") is None or age < 0 or age >= ttl_h:
                    return None
                out[n] = {"source": s["source"], "seconds": 0, "computed_at": s["computed_at"],
                          "cached_from": s["computed_at"], "cached_age_h": round(age, 1)}
            return out

        def user_names() -> dict[Any, str]:
            try:
                return {r.get("user_id"): r.get("user_name") for r in self._q(source, ctx, Q_USERNAMES, sect_t)}
            except CollectorError:
                return {}

        def user_label(r: dict[str, Any], names: dict[Any, str]) -> str:
            return r.get("user_name") or names.get(r.get("user_id")) or f"user#{r.get('user_id')}"

        def top(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            out = []
            for r in rows:
                key, text = normalize_query(r.get("query"))
                out.append({"key": key, "query": text, "count": r.get("n"), "total_exec_s": r.get("total_exec_s"),
                            "avg_exec_s": r.get("avg_exec_s"), "max_exec_s": r.get("max_exec_s")})
            return out

        def share(rows: list[dict[str, Any]], key: str, total_exec: float) -> list[dict[str, Any]]:
            names = user_names() if key == "user" and any("user_name" not in r for r in rows) else {}
            return [{key: user_label(r, names) if key == "user" else r.get("query_type"), "query_count": r.get("query_count"),
                     "exec_s": r.get("exec_s"),
                     "share_pct": round(100.0 * float(r.get("exec_s") or 0) / total_exec, 1) if total_exec else None}
                    for r in rows]

        def legacy_workload() -> None:
            """One query per section (used when the single pass cannot run: no generic_query_hash, view missing)."""
            totals = run("workload_24h", v_totals(), post=lambda r: dict(r[0]) if r else {})
            total_exec = float((totals or {}).get("exec_s") or 0)
            run("workload_users", v_users(), post=lambda r: share(r, "user", total_exec))
            run("workload_types", v_types(), post=lambda r: share(r, "query_type", total_exec))
            run("top_queries", v_top_queries(top_q), post=top)
            run("concurrency_24h", v_concurrency())

        def workload() -> int | None:
            t0 = time.monotonic()
            try:
                rows = self._q(source, ctx, v_single_pass(), heavy_t)
            except CollectorError as ex:
                if _is_timeout(str(ex)):  # a timeout is not a missing view: do not pay for five more scans
                    for n in WORKLOAD_SECTIONS:
                        sections[n] = {"source": None, "seconds": round(time.monotonic() - t0, 2)}
                        errors.append(f"{n}: sys_query_history (single pass): {ex}")
                    return None
                meta_extra["single_pass_fallback"] = "missing column or view" if re.search(
                    r"does not exist|not found|undefined|unknown column", str(ex), re.I) else "other error"
                legacy_workload()
                return None
            scan_s = round(time.monotonic() - t0, 2)
            truncated = len(rows) > SINGLE_PASS_CAP
            rows = rows[:SINGLE_PASS_CAP]
            d = derive_workload(rows, top_q)
            total_exec = float(d["totals"]["exec_s"] or 0)
            data["workload_24h"] = d["totals"]
            data["workload_users"] = share(d["users"], "user", total_exec)
            data["workload_types"] = share(d["types"], "query_type", total_exec)
            data["concurrency_24h"] = d["concurrency"]
            texts: dict[str, Any] = {}
            t1 = time.monotonic()
            text_ok = True
            hashes = [r["qhash"] for r in d["top"] if r["qhash"]]
            if hashes:
                try:
                    for r in self._q(source, ctx, v_top_text(hashes), sect_t):
                        texts[str(r.get("qhash") or "").strip()] = r.get("query")
                except CollectorError:
                    text_ok = False
            data["top_queries"] = [
                {"key": hashlib.sha1(r["qhash"].encode("utf-8")).hexdigest()[:10],
                 "query": normalize_query(_norm_text(texts.get(r["qhash"])))[1] if texts.get(r["qhash"]) else "(text unavailable)",
                 "count": r["n"], "total_exec_s": r["total_exec_s"], "avg_exec_s": r["avg_exec_s"],
                 "max_exec_s": r["max_exec_s"]} for r in d["top"]]
            stamp = utc_iso(ctx.now)
            for n in WORKLOAD_SECTIONS:
                sections[n] = {"source": "sys_query_history", "single_pass": True, "computed_at": stamp,
                               "seconds": scan_s if n == "workload_24h" else 0}
            sections["top_queries"]["seconds"] = round(time.monotonic() - t1, 2)
            if not text_ok:
                sections["top_queries"]["text"] = "failed"
            if truncated:
                meta_extra["workload_truncated"] = SINGLE_PASS_CAP
            return d["min_query_id"]

        meta_extra: dict[str, Any] = {}
        min_qid: int | None = None
        wl_cached = None
        if light:  # operator asked for zero heavy reads: no workload / spill scan, and nothing to reuse
            for n in WORKLOAD_SECTIONS:
                sections[n] = {"source": None, "skipped": "light mode", "seconds": 0}
        else:
            wl_cached = reusable(WORKLOAD_SECTIONS, all(k in prior_data for k in (
                "workload_24h", "workload_users", "workload_types", "top_queries", "concurrency_24h")))
        if wl_cached:
            for n in WORKLOAD_SECTIONS:
                data[n] = prior_data[n]
            sections.update(wl_cached)
            min_qid = (prior_data.get("meta") or {}).get("min_query_id")
            if "workload_truncated" in (prior_data.get("meta") or {}):
                meta_extra["workload_truncated"] = prior_data["meta"]["workload_truncated"]
        elif not light:
            min_qid = workload()

        # pressure
        pressure: dict[str, Any] = {}
        spill_cached = None if light else reusable(
            ("pressure_spill",), "spilled_queries" in (prior_data.get("pressure_24h") or {}))
        if light:
            sections["pressure_spill"] = {"source": None, "skipped": "light mode", "seconds": 0}
            spill = None
        elif spill_cached:
            sections.update(spill_cached)
            spill = [{k: prior_data["pressure_24h"].get(k) for k in ("spilled_queries", "spilled_mb")}]
        else:
            if min_qid is None:  # the single pass did not run: a cheap id bound from STL_QUERY keeps the spill scan small
                try:
                    r = self._q(source, ctx, Q_MINQID, heavy_t)
                    min_qid = int(r[0]["min_query_id"]) if r and r[0].get("min_query_id") is not None else None
                except (CollectorError, ValueError, TypeError, KeyError):
                    min_qid = None
            spill = run("pressure_spill", v_spill(min_qid), store=False, base_t=heavy_t)
        commit = run("pressure_commit", v_commit_queue(), store=False)
        if spill:
            pressure.update({"spilled_queries": spill[0].get("spilled_queries"), "spilled_mb": spill[0].get("spilled_mb")})
        if commit:
            pressure["commit_queue_p95_s"] = commit[0].get("commit_queue_p95_s")
        if spill or commit:
            data["pressure_24h"] = pressure
        run("alert_events", v_alerts())

        run("tables", q_tables(top_t), post=lambda r: [
            {"schema": x.get("schema"), "name": x.get("name"), "size_mb": x.get("size_mb"), "rows": x.get("rows"),
             "unsorted_pct": x.get("unsorted_pct"), "stats_off": x.get("stats_off"), "skew_rows": x.get("skew_rows"),
             "diststyle": x.get("diststyle"), "sortkey1": x.get("sortkey1")} for x in r])

        locks: list[dict[str, Any]] = []
        blocked = run("locks_blocked", v_blocked(), store=False)
        longs = run("locks_long_txns", q_long_txns(long_txn), store=False)
        for r in blocked or []:
            locks.append({"kind": "blocked", "pid": r.get("pid"), "blocking_pid": r.get("blocking_pid"),
                          "user": r.get("user_name"), "object": r.get("object_type"), "relation_id": r.get("relation_id"),
                          "wait_s": r.get("wait_s")})
        for r in longs or []:
            locks.append({"kind": "long_txn", "pid": r.get("pid"), "user": r.get("user_name"), "age_s": r.get("age_s")})
        if blocked is not None or longs is not None:
            data["locks"] = sorted(locks, key=lambda r: (r["kind"], -(r.get("wait_s") or r.get("age_s") or 0), r["pid"] or 0))

        run("load_errors_24h", v_load_errors(), post=lambda r: [
            {"table": x.get("table_name"), "column": x.get("column_name"), "error": x.get("error"), "count": x.get("n")}
            for x in r])
        run("maintenance", v_maintenance(), post=lambda r: [
            {"operation": x.get("operation"), "runs": x.get("runs"), "tables": x.get("tables"), "last_at": x.get("last_at")}
            for x in r])

        if "cluster" not in data and not any(
                k != "cluster" and s.get("source") and not s.get("cached_from") for k, s in sections.items()):
            # nothing could be read live (cluster down): reused sections alone must not turn that into a usable snapshot
            keys, _ = self.reused_data({"meta": {"sections": sections}})
            for k in keys:
                data.pop(k, None)
            data.pop("pressure_24h", None)
            sections = {k: v for k, v in sections.items() if not v.get("cached_from")}
        data["meta"] = {"window_h": WINDOW_H, "long_txn_seconds": long_txn, "heavy_ttl_h": ttl_h, **({"light": True} if light else {}), **meta_extra,
                        **({"min_query_id": min_qid} if min_qid is not None else {}),
                        "sections": dict(sorted(sections.items()))}
        return {"data": data, "errors": errors}

    def _cluster(self, source: Any, ctx: RunContext, data: dict[str, Any], errors: list[str],
                 sections: dict[str, dict[str, Any]], sect_t: int) -> None:
        t0 = time.monotonic()
        info: dict[str, Any] = {}
        try:
            rows = self._q(source, ctx, Q_VERSION, sect_t)
            info["version"] = str(rows[0].get("version", "")) if rows else None
        except CollectorError as ex:
            errors.append(f"cluster: {ex}")
        try:
            r = self._q(source, ctx, Q_SLICES, sect_t)
            info.update({"kind": "provisioned", "nodes": r[0].get("nodes"), "slices": r[0].get("slices")})
            sections["cluster"] = {"source": "stv_slices", "seconds": 0}
        except CollectorError:
            try:
                self._q(source, ctx, Q_SERVERLESS, sect_t)
                info["kind"] = "serverless"
                sections["cluster"] = {"source": "sys_serverless_usage", "seconds": 0}
            except CollectorError:
                info["kind"] = "unknown"
                sections["cluster"] = {"source": "version()", "seconds": 0}
        if source.aws is not None:
            try:
                info["aws"] = self._describe(source, ctx, info.get("kind"))
            except Exception as ex:  # AwsUnavailable, ClientError, ReadOnlyViolation: never fatal
                from de_agent.collectors.aws import _aws_core as core

                errors.append(f"cluster.aws: {core.describe_error(ex)}")
        if info.get("version") is not None or info.get("kind") in ("provisioned", "serverless"):
            data["cluster"] = info  # a cluster nothing could be read from has no section (so the snapshot counts as failed)
        if "cluster" in sections:
            sections["cluster"]["seconds"] = round(time.monotonic() - t0, 2)

    def _describe(self, source: Any, ctx: RunContext, kind: str | None) -> dict[str, Any]:
        from de_agent.collectors.aws import _aws_core as core

        session = ctx.aws_session or core.make_session(source.aws)
        da = source.data_api or {}
        host_label = (source.conn.host.split(".")[0] if source.conn else "")
        if kind == "serverless" or (da.get("workgroup") and not da.get("cluster_id")):
            wg = session.client("redshift-serverless").get_workgroup(workgroupName=da.get("workgroup") or host_label)["workgroup"]
            cfgs = {c.get("parameterKey"): c.get("parameterValue") for c in wg.get("configParameters") or []}
            return {"kind": "serverless", "status": wg.get("status"), "base_capacity_rpu": wg.get("baseCapacity"),
                    "max_capacity_rpu": wg.get("maxCapacity"), "publicly_accessible": wg.get("publiclyAccessible"),
                    "enhanced_vpc_routing": wg.get("enhancedVpcRouting"),
                    "max_query_execution_time_s": cfgs.get("max_query_execution_time")}
        cl = session.client("redshift").describe_clusters(
            ClusterIdentifier=da.get("cluster_id") or host_label)["Clusters"][0]
        return {"kind": "provisioned", "status": cl.get("ClusterStatus"), "node_type": cl.get("NodeType"),
                "nodes": cl.get("NumberOfNodes"), "availability_zone": cl.get("AvailabilityZone"),
                "encrypted": cl.get("Encrypted"), "publicly_accessible": cl.get("PubliclyAccessible"),
                "cluster_version": cl.get("ClusterVersion"), "maintenance_track": cl.get("MaintenanceTrackName"),
                "enhanced_vpc_routing": cl.get("EnhancedVpcRouting")}
