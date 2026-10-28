"""Redshift result fixtures, HAND-BUILT from the documented system-view columns with fictional values (Acme).

They are NOT recorded from a live cluster: no cluster was available. The column sets follow the public documentation for
SYS_QUERY_HISTORY, SYS_QUERY_DETAIL, STL_WLM_QUERY, STL_COMMIT_STATS, SVV_TABLE_INFO, SVV_TRANSACTIONS and friends.
Whether the real views return exactly these shapes is what the optional live check (docs/design/specs/M3.md section 7) is for.
"""
from de_agent.collectors import redshift as rs
from de_agent.collectors.base import CollectorError

VERSION = "PostgreSQL 8.0.2 on i686-pc-linux-gnu, compiled by gcc (GCC) 3.4.2, Redshift 1.0.99999"


def healthy():
    """{sql: rows} for the primary (SYS_) variant of every section: a quiet, well-kept fictional cluster."""
    a = {}
    a[rs.Q_VERSION.sql] = [{"version": VERSION}]
    a[rs.Q_SLICES.sql] = [{"nodes": 2, "slices": 4}]
    a[rs.v_totals()[0].sql] = [{"queries": 99000, "exec_s": 5500.0, "queue_p50_s": 0.01, "queue_p95_s": 0.4}]
    a[rs.v_users()[0].sql] = [
        {"user_name": "acme_etl", "query_count": 5200, "exec_s": 3200.0},
        {"user_name": "acme_bi", "query_count": 3800, "exec_s": 1500.0},
        {"user_name": "acme_dms", "query_count": 90000, "exec_s": 800.0}]
    a[rs.v_types()[0].sql] = [
        {"query_type": "INSERT", "query_count": 70000, "exec_s": 2600.0},
        {"query_type": "SELECT", "query_count": 28000, "exec_s": 2400.0},
        {"query_type": "COPY", "query_count": 1000, "exec_s": 500.0}]
    a[rs.v_top_queries(15)[0].sql] = [
        {"query": "select o.id, o.total from acme.orders o where o.created_at > ? and o.status = ?", "n": 900,
         "total_exec_s": 1200.0, "avg_exec_s": 1.33, "max_exec_s": 20.5},
        {"query": "insert into acme.fct_orders select * from acme.stg_orders where load_id = ?", "n": 24,
         "total_exec_s": 900.0, "avg_exec_s": 37.5, "max_exec_s": 80.0}]
    a[rs.v_spill()[0].sql] = [{"spilled_queries": 0, "spilled_mb": 0}]
    a[rs.v_commit_queue()[0].sql] = [{"commit_queue_p95_s": 0.4}]
    a[rs.v_concurrency()[0].sql] = [{"hour": "2026-10-19 10:00:00", "peak": 4}, {"hour": "2026-10-19 11:00:00", "peak": 9}]
    a[rs.v_alerts()[0].sql] = []
    a[rs.q_tables(50)[0].sql] = [
        {"schema": "acme", "name": "orders", "size_mb": 4096, "rows": 12000000, "unsorted_pct": 2.5, "stats_off": 1.0,
         "skew_rows": 1.2, "diststyle": "KEY(customer_id)", "sortkey1": "created_at"},
        {"schema": "acme", "name": "customers", "size_mb": 512, "rows": 800000, "unsorted_pct": 0.0, "stats_off": 0.0,
         "skew_rows": 1.0, "diststyle": "ALL", "sortkey1": "AUTO(SORTKEY)"}]
    a[rs.v_blocked()[0].sql] = []
    a[rs.q_long_txns(600)[0].sql] = []
    a[rs.v_load_errors()[0].sql] = []
    a[rs.v_maintenance()[0].sql] = [
        {"operation": "vacuum", "runs": 14, "tables": 9, "last_at": "2026-10-19 09:30:00"},
        {"operation": "analyze", "runs": 22, "tables": 15, "last_at": "2026-10-19 09:45:00"}]
    return a


def degraded():
    """The same cluster in trouble: every signal in the spec fires."""
    a = healthy()
    a[rs.v_totals()[0].sql] = [{"queries": 99000, "exec_s": 5500.0, "queue_p50_s": 2.0, "queue_p95_s": 45.0}]
    a[rs.v_users()[0].sql] = [
        {"user_name": "acme_etl", "query_count": 5200, "exec_s": 5000.0},
        {"user_name": "acme_bi", "query_count": 3800, "exec_s": 500.0}]
    a[rs.v_spill()[0].sql] = [{"spilled_queries": 7, "spilled_mb": 2048}]
    a[rs.v_commit_queue()[0].sql] = [{"commit_queue_p95_s": 12.5}]
    a[rs.v_alerts()[0].sql] = [{"event": "Nested Loop Join in the query plan - Check the join predicates", "n": 31,
                                "last_at": "2026-10-19 10:12:00"}]
    a[rs.q_tables(50)[0].sql] = [
        {"schema": "acme", "name": "orders", "size_mb": 4096, "rows": 12000000, "unsorted_pct": 35.0, "stats_off": 25.0,
         "skew_rows": 6.2, "diststyle": "KEY(customer_id)", "sortkey1": "created_at"},
        {"schema": "acme", "name": "tiny_lookup", "size_mb": 1, "rows": 100, "unsorted_pct": 80.0, "stats_off": 99.0,
         "skew_rows": 40.0, "diststyle": "EVEN", "sortkey1": None}]
    a[rs.v_blocked()[0].sql] = [{"pid": 31002, "blocking_pid": 30977, "user_name": "acme_bi", "object_type": "relation",
                                 "relation_id": 100234, "wait_s": 420}]
    a[rs.q_long_txns(600)[0].sql] = [{"pid": 30977, "user_name": "acme_etl", "age_s": 5400}]
    a[rs.v_load_errors()[0].sql] = [
        {"table_name": "acme.orders", "column_name": "total_amount", "error": "Invalid digit, Value 'x', Pos 0, Type: Decimal",
         "n": 12},
        {"table_name": "acme.events", "column_name": "payload", "error": "String length exceeds DDL length", "n": 3}]
    return a


def fake_query(answers, log=None, default=None):
    """query_fn(conn, sql, exec, timeout): answers by exact SQL text; unknown SQL behaves like a missing view."""

    def fn(conn, sql, exec_target, timeout_s):
        if log is not None:
            log.append((sql, timeout_s))
        if sql in answers:
            v = answers[sql]
            if isinstance(v, Exception):
                raise v
            return v
        if default is not None:
            return default(sql)
        raise CollectorError('relation "' + sql.split(" from ", 1)[-1].split()[0] + '" does not exist')

    return fn


# ---- single-pass workload (M3.3): fictional groups at user x query_type x hour x generic_query_hash --------------------

H_ORDERS, H_FCT, H_MISC = "a1b2c3d4e5f6a7b8", "0f9e8d7c6b5a4938", "ffeeddccbbaa9988"


def _grp(user_id, query_type, hour, qhash, n, exec_s, max_exec_s, elapsed_s, queue_s, max_queue_s, min_query_id):
    return {"user_id": user_id, "query_type": query_type, "hour": hour, "qhash": qhash, "n": n, "exec_s": exec_s,
            "max_exec_s": max_exec_s, "elapsed_s": elapsed_s, "queue_s": queue_s, "max_queue_s": max_queue_s,
            "min_query_id": min_query_id}


def single_pass_rows():
    h10, h11 = "2026-10-19 10:00:00", "2026-10-19 11:00:00"
    return [
        _grp(101, "INSERT", h10, H_FCT, 12, 450.0, 80.0, 460.0, 0.0, 0.0, 5000),
        _grp(101, "INSERT", h11, H_FCT, 12, 450.0, 80.0, 7300.0, 0.0, 0.0, 5100),
        _grp(102, "SELECT", h10, H_ORDERS, 450, 600.0, 20.5, 650.0, 4.5, 1.0, 5010),
        _grp(102, "SELECT", h11, H_ORDERS, 450, 600.0, 20.5, 650.0, 4.5, 1.0, 5110),
        _grp(103, "COPY", h11, H_MISC, 30, 100.0, 10.0, 110.0, 60.0, 9.0, 5120),
        _grp(103, "SELECT", h11, None, 5, 1.0, 0.5, 1.0, 0.0, 0.0, 5130),
    ]


SINGLE_TEXTS = [{"qhash": H_ORDERS, "query": "select o.id from acme.orders o where o.id = 42 and o.status = 'open'"},
                {"qhash": H_FCT, "query": "insert into acme.fct_orders select * from acme.stg_orders"}]


def single_pass(a=None, rows=None, texts=None):
    """healthy() plus answers for the single-pass query, the sample-text query and the bounded spill query."""
    a = dict(a or healthy())
    rows = single_pass_rows() if rows is None else rows
    a[rs.v_single_pass().sql] = rows
    top_hashes = [H_ORDERS, H_FCT, H_MISC]
    a[rs.v_top_text(top_hashes).sql] = SINGLE_TEXTS if texts is None else texts
    a[rs.v_spill(5000)[0].sql] = [{"spilled_queries": 0, "spilled_mb": 0}]
    return a
