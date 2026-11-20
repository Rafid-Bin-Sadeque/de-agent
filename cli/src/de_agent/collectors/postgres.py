"""Postgres collector (M1a section 5). PG 12-17, pure SELECTs over pg_catalog / pg_stat_*.

Each section is its own query; a failing section lands in `errors` and the others still return.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from de_agent.collectors.base import CollectorError, RunContext
from de_agent.routines import (
    KINDS,
    ROUTINE_OPTION_KEYS,
    finalize_rows,
    routine_settings,
    validate_routine_options,
)
from de_agent.scanners.routines import make_parser

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
XID_MAX = 2_147_483_647
SETTINGS = (
    "shared_buffers", "work_mem", "maintenance_work_mem", "autovacuum", "statement_timeout",
    "idle_in_transaction_session_timeout",
)
DEFAULT_LONG_QUERY_S = 300
OPTION_KEYS = {"schemas", "top_tables", "long_query_seconds"} | ROUTINE_OPTION_KEYS


def validate_options(options: dict[str, Any]) -> None:
    unknown = set(options) - OPTION_KEYS
    if unknown:
        raise ValueError(f"unknown postgres options: {', '.join(sorted(unknown))}")
    schemas = options.get("schemas", [])
    if not isinstance(schemas, list) or not all(isinstance(s, str) and IDENT_RE.match(s) for s in schemas):
        raise ValueError("options.schemas must be a list of plain schema names")
    for key in ("top_tables", "long_query_seconds"):
        if key in options and not (isinstance(options[key], int) and options[key] > 0):
            raise ValueError(f"options.{key} must be a positive integer")
    validate_routine_options(options)


def validate_source(source: Any) -> None:
    if source.conn is None:
        raise ValueError("postgres sources need a `conn:` block (host, port, dbname, user, password)")


def _schema_filter(col: str, schemas: list[str]) -> str:
    if schemas:
        return f"{col} in ({', '.join(repr(s) for s in schemas)})"
    return f"left({col}, 3) <> 'pg_' and {col} <> 'information_schema'"


def q_server() -> str:
    return """
select version() as version,
       extract(epoch from now() - pg_postmaster_start_time())::bigint as uptime_s,
       current_setting('max_connections')::int as max_connections,
       (select count(*) from pg_stat_activity where backend_type = 'client backend') as connections_total,
       (select coalesce(json_object_agg(st, c), '{}'::json)
          from (select coalesce(state, 'unknown') as st, count(*) as c
                  from pg_stat_activity where backend_type = 'client backend' group by 1) s) as connections,
       (select round(100.0 * sum(blks_hit) / nullif(sum(blks_hit) + sum(blks_read), 0), 2)
          from pg_stat_database) as cache_hit_pct,
       (select max(age(datfrozenxid)) from pg_database) as oldest_xid_age
"""


def q_databases() -> str:
    return """
select datname as name, pg_database_size(oid)::bigint as size_bytes
from pg_database where not datistemplate
order by 2 desc, 1
"""


def q_schemas(schemas: list[str]) -> str:
    return f"""
select n.nspname as name,
       count(c.oid) filter (where c.relkind in ('r', 'p')) as table_count,
       coalesce(sum(pg_total_relation_size(c.oid)) filter (where c.relkind in ('r', 'm')), 0)::bigint
         as total_bytes
from pg_namespace n
left join pg_class c on c.relnamespace = n.oid
where {_schema_filter('n.nspname', schemas)}
group by n.nspname
order by 3 desc, 1
"""


def q_tables(schemas: list[str], top: int) -> str:
    return f"""
select s.schemaname as "schema", s.relname as name,
       pg_total_relation_size(s.relid)::bigint as total_bytes,
       pg_table_size(s.relid)::bigint as table_bytes,
       pg_indexes_size(s.relid)::bigint as index_bytes,
       s.n_live_tup, s.n_dead_tup,
       round(100.0 * s.n_dead_tup / nullif(s.n_live_tup + s.n_dead_tup, 0), 1) as dead_pct,
       s.last_autovacuum, s.last_autoanalyze, s.seq_scan, s.idx_scan
from pg_stat_user_tables s
where {_schema_filter('s.schemaname', schemas)}
order by 3 desc, 1, 2
limit {int(top)}
"""


def q_schemas_nolock(schemas: list[str]) -> str:
    """Estimated sizes from pg_class.relpages (no relation locks): used when a table is ACCESS EXCLUSIVE locked."""
    return f"""
select n.nspname as name,
       count(c.oid) filter (where c.relkind in ('r', 'p')) as table_count,
       (coalesce(sum(c.relpages) filter (where c.relkind in ('r', 'm')), 0) * 8192)::bigint as total_bytes
from pg_namespace n
left join pg_class c on c.relnamespace = n.oid
where {_schema_filter('n.nspname', schemas)}
group by n.nspname
order by 3 desc, 1
"""


def q_tables_nolock(schemas: list[str], top: int) -> str:
    return f"""
select s.schemaname as "schema", s.relname as name,
       (c.relpages::bigint * 8192) as total_bytes,
       (c.relpages::bigint * 8192) as table_bytes,
       null::bigint as index_bytes,
       s.n_live_tup, s.n_dead_tup,
       round(100.0 * s.n_dead_tup / nullif(s.n_live_tup + s.n_dead_tup, 0), 1) as dead_pct,
       s.last_autovacuum, s.last_autoanalyze, s.seq_scan, s.idx_scan
from pg_stat_user_tables s
join pg_class c on c.oid = s.relid
where {_schema_filter('s.schemaname', schemas)}
order by 3 desc, 1, 2
limit {int(top)}
"""


def q_unused_indexes(schemas: list[str]) -> str:
    return f"""
select s.schemaname as "schema", s.relname as "table", s.indexrelname as "index",
       pg_relation_size(s.indexrelid)::bigint as size_bytes
from pg_stat_user_indexes s
join pg_index i on i.indexrelid = s.indexrelid
where s.idx_scan = 0 and not i.indisunique and not i.indisprimary
  and pg_relation_size(s.indexrelid) > 10 * 1024 * 1024
  and {_schema_filter('s.schemaname', schemas)}
order by 4 desc, 1, 2, 3
"""


def q_long_queries(min_seconds: int) -> str:
    # state = active only: an idle-in-transaction session (e.g. the Airflow metastore's advisory-lock connection) is not a running query
    return f"""
select pid, usename as "user",
       extract(epoch from now() - query_start)::bigint as duration_s,
       state, wait_event, left(query, 200) as query
from pg_stat_activity
where state = 'active' and backend_type = 'client backend' and pid <> pg_backend_pid()
  and now() - query_start > interval '{int(min_seconds)} seconds'
order by 3 desc, 1
"""


def q_blocking() -> str:
    return """
select b.pid as blocked_pid, left(b.query, 200) as blocked_query,
       k.pid as blocker_pid, left(k.query, 200) as blocker_query,
       extract(epoch from now() - b.state_change)::bigint as wait_s
from pg_stat_activity b
join pg_stat_activity k on k.pid = any (pg_blocking_pids(b.pid))
order by 5 desc, 1, 3
"""


def q_slots() -> str:
    return """
select slot_name, plugin, active,
       case when restart_lsn is null then null
            when pg_is_in_recovery() then pg_wal_lsn_diff(pg_last_wal_replay_lsn(), restart_lsn)::bigint
            else pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)::bigint end as lag_bytes
from pg_replication_slots
order by 1
"""


def q_settings() -> str:
    names = ", ".join(repr(n) for n in SETTINGS)
    # reset_val, not setting: the sqlexec wrapper SET LOCALs statement_timeout/lock_timeout in this very transaction
    return f"select name, reset_val as setting, unit from pg_settings where name in ({names}) order by 1"


def q_routines(schemas: list[str], kinds: list[str], max_rows: int, view_bodies: bool = True) -> str:
    """Procedures, functions, views, materialized views and triggers in one UNION ALL (v0.3 section 2.1).

    Aggregates and window functions are skipped (prokind p/f only); extension-owned objects (pg_depend deptype e)
    are excluded; `internal`/`c` functions keep language and an empty body. Reads definitions only, never calls them.
    pg_get_viewdef() locks the relations a view reads, so it queues behind an ACCESS EXCLUSIVE lock on them:
    `view_bodies=False` (the lock-timeout fallback) lists views without their text."""
    ext = "not exists (select 1 from pg_depend d where d.classid = '{cls}'::regclass and d.objid = {oid} and d.deptype = 'e')"
    parts: list[str] = []
    if "procedure" in kinds or "function" in kinds:
        codes = ", ".join(repr(c) for k, c in (("procedure", "p"), ("function", "f")) if k in kinds)
        parts.append(f"""
select n.nspname::text as "schema", p.proname::text as name,
       case p.prokind when 'p' then 'procedure' else 'function' end as kind,
       '(' || oidvectortypes(p.proargtypes) || ')' as args,
       l.lanname::text as language,
       coalesce(pg_get_function_result(p.oid), '') as returns,
       pg_get_userbyid(p.proowner)::text as owner,
       case when l.lanname in ('internal', 'c') then '' else pg_get_functiondef(p.oid) end as def
from pg_proc p
join pg_namespace n on n.oid = p.pronamespace
join pg_language l on l.oid = p.prolang
where p.prokind in ({codes}) and {_schema_filter('n.nspname', schemas)}
  and {ext.format(cls='pg_proc', oid='p.oid')}""")
    if "view" in kinds or "materialized_view" in kinds:
        rk = ", ".join(repr(c) for k, c in (("view", "v"), ("materialized_view", "m")) if k in kinds)
        parts.append(f"""
select n.nspname::text, c.relname::text,
       case c.relkind when 'm' then 'materialized_view' else 'view' end,
       '', 'sql', '', pg_get_userbyid(c.relowner)::text, {'pg_get_viewdef(c.oid)' if view_bodies else "''"}
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where c.relkind in ({rk}) and {_schema_filter('n.nspname', schemas)}
  and {ext.format(cls='pg_class', oid='c.oid')}""")
    if "trigger" in kinds:
        parts.append(f"""
select n.nspname::text, t.tgname::text, 'trigger', 'on ' || c.relname::text, '', '',
       pg_get_userbyid(c.relowner)::text, pg_get_triggerdef(t.oid)
from pg_trigger t
join pg_class c on c.oid = t.tgrelid
join pg_namespace n on n.oid = c.relnamespace
where not t.tgisinternal and {_schema_filter('n.nspname', schemas)}""")
    body = "\nunion all".join(parts)
    return f"""
select "schema", name, kind, args, language, returns, owner, octet_length(def) as body_bytes, def as definition
from ({body}
) r("schema", name, kind, args, language, returns, owner, def)
order by 1, 2, 4, 3
limit {int(max_rows) + 1}
"""


Q_HAS_PSS = "select 1 as present from pg_extension where extname = 'pg_stat_statements'"


def q_statements(column: str) -> str:
    return f"""
select left(query, 200) as query, calls, round({column}::numeric, 1) as total_exec_ms,
       round(({column} / nullif(calls, 0))::numeric, 2) as mean_exec_ms, rows
from pg_stat_statements
order by {column} desc, queryid
limit 10
"""


def _derive_server(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise CollectorError("no row returned")
    d = dict(rows[0])
    age = d.get("oldest_xid_age")
    d["xid_wraparound_pct"] = round(100.0 * age / XID_MAX, 2) if isinstance(age, (int, float)) else None
    return d


class PostgresCollector:
    type = "postgres"
    schema_version = 1
    list_keys = {
        "databases": ["name"],
        "schemas": ["name"],
        "tables": ["schema", "name"],
        "unused_indexes": ["schema", "table", "index"],
        "long_queries": ["pid"],
        "blocking": ["blocked_pid", "blocker_pid"],
        "replication_slots": ["slot_name"],
        "settings": ["name"],
        "routines": ["schema", "name", "args"],
    }
    volatile_fields = {
        "last_autovacuum", "last_autoanalyze", "seq_scan", "idx_scan", "duration_s", "wait_s",
        "state", "wait_event", "query", "blocked_query", "blocker_query", "owner",
        "reads", "writes", "calls", "parse", "body_bytes",
    }
    validate_options = staticmethod(validate_options)
    validate_source = staticmethod(validate_source)

    def ping(self, source: Any, ctx: RunContext) -> str:
        ctx.query(source, "select 1 as ok")
        return "select 1 ok"

    def collect(self, source: Any, ctx: RunContext) -> dict[str, Any]:
        opts = source.options
        schemas = list(opts.get("schemas", []))
        top = int(opts.get("top_tables", 25))
        long_s = int(opts.get("long_query_seconds", DEFAULT_LONG_QUERY_S))
        data: dict[str, Any] = {"meta": {"long_query_seconds": long_s, "top_tables": top}}
        errors: list[str] = []

        def run(section: str, sql: str, post: Callable[[list[dict[str, Any]]], Any] = lambda r: r) -> None:
            try:
                data[section] = post(ctx.query(source, sql))
            except CollectorError as ex:
                errors.append(f"{section}: {ex}")

        run("server", q_server(), _derive_server)
        run("databases", q_databases())
        estimated: list[str] = []

        def run_nolock_fallback(section: str, sql: str, fallback: str) -> None:
            """pg_total_relation_size() queues behind an ACCESS EXCLUSIVE lock: fall back to relpages estimates."""
            try:
                data[section] = ctx.query(source, sql)
            except CollectorError as ex:
                if "lock timeout" not in str(ex):
                    errors.append(f"{section}: {ex}")
                    return
                try:
                    data[section] = ctx.query(source, fallback)
                    estimated.append(section)
                except CollectorError as ex2:
                    errors.append(f"{section}: {ex2}")

        run_nolock_fallback("schemas", q_schemas(schemas), q_schemas_nolock(schemas))
        run_nolock_fallback("tables", q_tables(schemas, top), q_tables_nolock(schemas, top))
        if estimated:
            data["meta"]["sizes_estimated"] = estimated
        run("unused_indexes", q_unused_indexes(schemas))
        run("long_queries", q_long_queries(long_s))
        run("blocking", q_blocking())
        run("replication_slots", q_slots())
        run("settings", q_settings())
        self._statements(source, ctx, data)
        self._routines(source, ctx, data, errors, schemas)
        return {"data": data, "errors": errors}

    def _routines(self, source: Any, ctx: RunContext, data: dict[str, Any], errors: list[str], schemas: list[str]) -> None:
        cfg = routine_settings(source.options)
        if not cfg["enabled"]:
            return
        kinds = [k for k in KINDS if k in cfg["kinds"]]
        try:
            try:
                raw = ctx.query(source, q_routines(schemas, kinds, cfg["max_rows"]))
            except CollectorError as ex:
                if "lock timeout" not in str(ex) or not ({"view", "materialized_view"} & set(kinds)):
                    raise
                raw = ctx.query(source, q_routines(schemas, kinds, cfg["max_rows"], view_bodies=False))
                data["meta"]["routine_view_bodies_skipped"] = True
            data["routines"] = finalize_rows(raw, source.options, ctx.routine_store, parser=make_parser("postgres"))
        except (CollectorError, ValueError) as ex:
            errors.append(f"routines: {ex}")

    def _statements(self, source: Any, ctx: RunContext, data: dict[str, Any]) -> None:
        """Optional section: silently skipped when pg_stat_statements is absent or not loaded."""
        try:
            if not ctx.query(source, Q_HAS_PSS):
                return
            for column in ("total_exec_time", "total_time"):  # PG13+ / PG12
                try:
                    data["top_statements"] = ctx.query(source, q_statements(column))
                    return
                except CollectorError:
                    continue
        except CollectorError:
            return
