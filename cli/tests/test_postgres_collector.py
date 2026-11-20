import functools
import json
import os
import secrets
import shutil
import subprocess
import threading
import time

import pytest
from de_agent.collectors import postgres as pgc
from de_agent.collectors.base import CollectorError, RunContext
from de_agent.config import parse_config
from de_agent.render import render, signals_for
from de_agent.runner import result_status, run_source
from de_agent.sqlexec import check_read_only

# ---------------------------------------------------------------- unit tests (no database)


def _source(**opts):
    options = ", ".join(f"{k}: {v}" for k, v in opts.items())
    cfg = parse_config(
        "sources:\n  - name: wh\n    type: postgres\n"
        "    conn: {dbname: shop, user: readonly_agent}\n"
        f"    options: {{{options}}}\n")
    return cfg.sources[0]


def test_every_collector_query_is_read_only():
    qs = [pgc.q_server(), pgc.q_databases(), pgc.q_schemas([]), pgc.q_schemas(["shop", "public"]),
          pgc.q_tables([], 25), pgc.q_tables(["shop"], 5), pgc.q_unused_indexes([]), pgc.q_long_queries(300),
          pgc.q_blocking(), pgc.q_slots(), pgc.q_settings(), pgc.Q_HAS_PSS,
          pgc.q_statements("total_exec_time"), pgc.q_statements("total_time")]
    for q in qs:
        check_read_only(q)


def test_routines_query_is_read_only_for_every_kind_mix():
    for kinds in (["procedure"], ["function"], ["view"], ["materialized_view"], ["trigger"], list(pgc.KINDS)):
        check_read_only(pgc.q_routines([], kinds, 10))
        check_read_only(pgc.q_routines(["shop"], kinds, 10))


def test_routines_section_unit(tmp_path):
    from de_agent.routines import RoutineStore

    raw = [{"schema": "shop", "name": "f", "kind": "function", "args": "(integer)", "language": "sql",
            "returns": "integer", "owner": "o", "body_bytes": 12, "definition": "select 1  \r\n"}]
    res = run_source(_source(), query_fn=_fake([("pg_get_functiondef", raw)]), routine_store=RoutineStore(tmp_path))
    (row,) = res["data"]["routines"]
    assert row["kind"] == "function" and row["body_bytes"] == 12 and "definition" not in row
    assert RoutineStore(tmp_path).get(row["body_hash"]) == "select 1"


def test_routines_view_bodies_fall_back_on_lock_timeout():
    calls = []

    def fn(conn, sql, exec_target, timeout_s):
        if "pg_get_functiondef" in sql:
            calls.append("pg_get_viewdef" in sql)
            if "pg_get_viewdef" in sql:
                raise CollectorError("canceling statement due to lock timeout")
            return [{"schema": "s", "name": "v", "kind": "view", "definition": ""}]
        return []

    res = run_source(_source(), query_fn=fn)
    assert calls == [True, False] and not [e for e in res["errors"] if e.startswith("routines")]
    assert res["data"]["meta"]["routine_view_bodies_skipped"] is True and len(res["data"]["routines"]) == 1


def test_routines_option_off_and_cap_and_validation():
    log = []
    res = run_source(_source(routines="false"), query_fn=_fake([], log))
    assert "routines" not in res["data"] and not any("pg_get_functiondef" in q for q in log)
    over = [{"schema": "s", "name": f"n{i}", "kind": "view", "definition": "select 1"} for i in range(3)]
    res = run_source(_source(routines_max=2), query_fn=_fake([("pg_get_functiondef", over)]))
    assert "routines" not in res["data"] and any(e.startswith("routines:") and "routines_max" in e for e in res["errors"])
    res = run_source(_source(), query_fn=_fake([("pg_get_functiondef", CollectorError("permission denied"))]))
    assert "routines: permission denied" in res["errors"]
    with pytest.raises(Exception, match="routine_kinds"):
        _source(routine_kinds="[bogus]")


def test_long_queries_only_counts_active_sessions():
    sql = pgc.q_long_queries(300)
    assert "state = 'active'" in sql and "state <> 'idle'" not in sql
    check_read_only(sql)


def test_options_validation():
    with pytest.raises(Exception, match="unknown postgres options"):
        _source(bogus=1)
    with pytest.raises(Exception, match="plain schema names"):
        parse_config("sources:\n  - {name: w, type: postgres, conn: {dbname: d, user: u}, "
                     "options: {schemas: [\"a'; drop table x; --\"]}}\n")


def test_schema_filter_quotes_names():
    assert "n.nspname in ('shop', 'public')" in pgc.q_schemas(["shop", "public"])


def _fake(responses, log=None):
    def fn(conn, sql, exec_target, timeout_s):
        if log is not None:
            log.append(sql)
        for needle, val in responses:
            if needle in sql:
                if isinstance(val, Exception):
                    raise val
                return val
        return []
    return fn


def test_partial_results_when_one_section_fails():
    resp = [
        ("version()", [{"version": "PostgreSQL 16.1", "uptime_s": 10, "max_connections": 100, "connections_total": 3,
                        "connections": {"idle": 3}, "cache_hit_pct": 99.0, "oldest_xid_age": 214748364}]),
        ("pg_stat_user_tables", CollectorError("permission denied for view pg_stat_user_tables")),
        ("pg_database_size", [{"name": "shop", "size_bytes": 1000}]),
    ]
    res = run_source(_source(), query_fn=_fake(resp))
    assert res["errors"] == ["tables: permission denied for view pg_stat_user_tables"]
    assert res["data"]["databases"] == [{"name": "shop", "size_bytes": 1000}]
    assert res["data"]["server"]["xid_wraparound_pct"] == 10.0
    assert "tables" not in res["data"]
    assert result_status(res) == "partial"
    md = render(res)
    assert "## Errors" in md and "permission denied" in md


def test_all_sections_failing_is_failed_status():
    res = run_source(_source(), query_fn=_fake([("", CollectorError("connection refused"))]))
    assert result_status(res) == "failed" and len(res["errors"]) >= 9


def test_pg_stat_statements_optional():
    log = []
    base = [("version()", [{"version": "PostgreSQL 16.1", "oldest_xid_age": 1}])]
    res = run_source(_source(), query_fn=_fake(base, log))
    assert "top_statements" not in res["data"] and not res["errors"]
    assert any("pg_extension" in q for q in log) and not any("from pg_stat_statements" in q for q in log)

    stm = base + [("pg_extension", [{"present": 1}]),
           ("total_exec_time", [{"query": "select 1", "calls": 3, "total_exec_ms": 5.0, "mean_exec_ms": 1.67, "rows": 3}])]
    res = run_source(_source(), query_fn=_fake(stm))
    assert res["data"]["top_statements"][0]["calls"] == 3

    pg12 = base + [("pg_extension", [{"present": 1}]), ("total_exec_time", CollectorError("column does not exist")),
            ("total_time", [{"query": "select 2", "calls": 1, "total_exec_ms": 1.0, "mean_exec_ms": 1.0, "rows": 1}])]
    assert run_source(_source(), query_fn=_fake(pg12))["data"]["top_statements"][0]["query"] == "select 2"

    not_loaded = base + [("pg_extension", [{"present": 1}]),
                  ("pg_stat_statements", CollectorError("pg_stat_statements must be loaded"))]
    res = run_source(_source(), query_fn=_fake(not_loaded))
    assert "top_statements" not in res["data"] and not res["errors"]


def test_source_timeout_budget():
    ctx = RunContext(timeout_seconds=0.0)
    with pytest.raises(CollectorError, match="budget exhausted"):
        ctx.query(_source(), "select 1")


# ---------------------------------------------------------------- integration (docker postgres:16)


@functools.lru_cache(maxsize=1)
def _docker_ok():
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


PG_IMAGES = [i.strip() for i in os.environ.get("DEA_PG_IMAGES", "postgres:16").split(",") if i.strip()]


@pytest.fixture(scope="module", params=PG_IMAGES)
def pg(request):
    image = request.param
    psycopg = pytest.importorskip("psycopg")
    if not _docker_ok():
        pytest.skip("docker is not available")
    name = "dea-test-" + secrets.token_hex(4)
    su_pw, ro_pw = secrets.token_hex(8), secrets.token_hex(8)
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-e", f"POSTGRES_PASSWORD={su_pw}", "-e", "POSTGRES_DB=shop",
         "-p", "127.0.0.1::5432", image, "-c", "wal_level=logical", "-c", "autovacuum=off"],
        capture_output=True, text=True, timeout=300)
    if run.returncode != 0:
        pytest.skip(f"cannot start {image}: {run.stderr.strip()[-200:]}")
    try:
        port = subprocess.run(["docker", "port", name, "5432/tcp"], capture_output=True, text=True,
                              timeout=20).stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        kw = dict(host="127.0.0.1", port=int(port), dbname="shop", user="postgres", password=su_pw, autocommit=True)
        for _ in range(60):
            try:
                psycopg.connect(**kw, connect_timeout=2).close()
                break
            except Exception:
                time.sleep(1)
        else:
            pytest.skip(f"{image} did not become ready")
        with psycopg.connect(**kw) as c:
            cur = c.cursor()
            cur.execute(f"create role readonly_agent login password '{ro_pw}'")
            cur.execute("grant pg_read_all_stats to readonly_agent")
            major = int(image.split(":")[1].split(".")[0].split("-")[0])
            if major >= 14:
                cur.execute("grant pg_read_all_data to readonly_agent")
            cur.execute("create schema shop")
            if major < 14:  # no pg_read_all_data before PG14
                cur.execute("grant usage on schema shop to readonly_agent")
                cur.execute("alter default privileges in schema shop grant select on tables to readonly_agent")
            cur.execute("create table shop.customers (id serial primary key, name text)")
            cur.execute("create table shop.orders (id serial primary key, customer_id int, amount numeric, note text) "
                        "with (autovacuum_enabled = false)")
            cur.execute("insert into shop.customers(name) select 'c' || g from generate_series(1, 20000) g")
            cur.execute("insert into shop.orders(customer_id, amount, note) select g % 20000 + 1, g, "
                        "md5(random()::text) || md5(random()::text) || md5(random()::text) "
                        "from generate_series(1, 300000) g")
            cur.execute("create index orders_note_idx on shop.orders(note)")  # ~20 MB, never scanned
            cur.execute("delete from shop.orders where id % 5 in (0, 1)")      # 40% dead tuples
            cur.execute("create function shop.order_total(int) returns numeric language sql as "
                        "$$ select sum(amount) from shop.orders where customer_id = $1 $$")
            cur.execute("create function shop.order_total(int, text) returns numeric language sql as "
                        "$$ select 1::numeric $$")
            cur.execute("create function shop.leaky() returns text language sql as "
                        "$$ select 'postgres://svc:s3cretpw@db.internal/x' $$")
            cur.execute("create procedure shop.refresh_stats() language plpgsql as "
                        "$$ begin delete from shop.customers where id < 0; end $$")
            cur.execute("create function shop.trg_fn() returns trigger language plpgsql as "
                        "$$ begin return new; end $$")
            cur.execute("create trigger orders_note_trg before insert on shop.orders "
                        "for each row execute function shop.trg_fn()")
            cur.execute("create view shop.big_orders as select id, amount from shop.orders where amount > 100")
            cur.execute("create materialized view shop.mv_customers as select count(*) as c from shop.customers")
            cur.execute("select pg_create_logical_replication_slot('cdc_slot', 'test_decoding')")
            for _ in range(30):  # wait for the stats to flush
                cur.execute("select n_dead_tup from pg_stat_user_tables where relname = 'orders'")
                if (cur.fetchone() or [0])[0] > 0:
                    break
                time.sleep(1)
        sleeper = psycopg.connect(**kw)

        def _sleep():
            try:
                sleeper.execute("select pg_sleep(400)")
            except Exception:  # closed at teardown
                pass

        threading.Thread(target=_sleep, daemon=True).start()
        # an idle-in-transaction session (what the Airflow metastore connection looks like): NOT a running query
        idler = psycopg.connect(**{**kw, "autocommit": False})
        idler.execute("select pg_advisory_lock(1)")
        time.sleep(4)  # long_query_seconds is 2 in the test source
        yield {"port": int(port), "ro_pw": ro_pw, "su_pw": su_pw, "major": major}
        sleeper.close()
        idler.close()
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


def _pg_source(pg, monkeypatch):
    monkeypatch.setenv("DEA_TEST_PG_PW", pg["ro_pw"])
    cfg = parse_config(
        "sources:\n  - name: wh\n    type: postgres\n    timeout_seconds: 60\n"
        f"    conn: {{host: 127.0.0.1, port: {pg['port']}, dbname: shop, user: readonly_agent, "
        "password: \"env:DEA_TEST_PG_PW\", sslmode: disable}\n"
        "    options: {schemas: [shop], long_query_seconds: 2}\n")
    return cfg.sources[0]


@pytest.mark.integration
def test_collector_all_sections_populated(pg, monkeypatch):
    res = run_source(_pg_source(pg, monkeypatch))
    assert res["errors"] == [], res["errors"]
    d = res["data"]
    assert d["server"]["version"].startswith(f"PostgreSQL {pg['major']}") and d["server"]["max_connections"] > 0
    assert d["server"]["connections_total"] >= 1 and 0 <= d["server"]["xid_wraparound_pct"] < 50
    assert any(x["name"] == "shop" for x in d["databases"])
    assert {s["name"]: s["table_count"] for s in d["schemas"]}["shop"] == 2
    orders = next(t for t in d["tables"] if t["name"] == "orders")
    assert orders["n_dead_tup"] >= 100_000 and orders["dead_pct"] >= 20
    assert any(i["index"] == "orders_note_idx" and i["size_bytes"] > 10 * 1024 * 1024 for i in d["unused_indexes"])
    assert any(q["duration_s"] >= 2 and "pg_sleep" in q["query"] for q in d["long_queries"])
    assert all(q["state"] == "active" for q in d["long_queries"]), d["long_queries"]  # idle-in-transaction is not "running"
    assert any(s["slot_name"] == "cdc_slot" and s["active"] is False for s in d["replication_slots"])
    assert {s["name"] for s in d["settings"]} == set(pgc.SETTINGS)
    assert d["blocking"] == []
    sig = signals_for(res)
    assert "table shop.orders 40% dead tuples" in sig
    assert any(s.startswith("1 query running >") for s in sig)
    assert any(s.startswith("slot cdc_slot inactive") for s in sig)
    assert any("unused index" in s for s in sig)
    json.dumps(res)  # JSON-clean


@pytest.mark.integration
def test_routines_section_live(pg, monkeypatch, tmp_path):
    from de_agent.routines import RoutineStore

    src = _pg_source(pg, monkeypatch)
    res = run_source(src, routine_store=RoutineStore(tmp_path))
    assert res["errors"] == [], res["errors"]
    rows = {(r["name"], r["args"]): r for r in res["data"]["routines"]}
    assert set(rows) == {
        ("order_total", "(integer)"), ("order_total", "(integer, text)"), ("leaky", "()"), ("refresh_stats", "()"),
        ("trg_fn", "()"), ("orders_note_trg", "on orders"), ("big_orders", ""), ("mv_customers", ""),
    }, sorted(rows)
    assert rows[("refresh_stats", "()")]["kind"] == "procedure" and rows[("refresh_stats", "()")]["language"] == "plpgsql"
    assert rows[("order_total", "(integer)")]["returns"] == "numeric"
    assert rows[("trg_fn", "()")]["returns"] == "trigger"
    assert rows[("big_orders", "")]["kind"] == "view" and rows[("mv_customers", "")]["kind"] == "materialized_view"
    assert rows[("orders_note_trg", "on orders")]["kind"] == "trigger"
    assert all(r["owner"] == "postgres" for r in rows.values())
    store = RoutineStore(tmp_path)
    body = store.get(rows[("refresh_stats", "()")]["body_hash"])
    assert "CREATE OR REPLACE PROCEDURE shop.refresh_stats()" in body and "delete from shop.customers" in body
    assert "shop.orders" in store.get(rows[("big_orders", "")]["body_hash"])
    assert "EXECUTE FUNCTION shop.trg_fn()" in store.get(rows[("orders_note_trg", "on orders")]["body_hash"])
    leaky = rows[("leaky", "()")]
    assert leaky["redacted"] and "s3cretpw" not in store.get(leaky["body_hash"])
    assert "s3cretpw" not in json.dumps(res)
    # system and extension objects are excluded: nothing outside the shop schema
    assert {r["schema"] for r in res["data"]["routines"]} == {"shop"}
    again = run_source(src, routine_store=RoutineStore(tmp_path))["data"]["routines"]
    assert [r["body_hash"] for r in again] == [r["body_hash"] for r in res["data"]["routines"]]  # deterministic


@pytest.mark.integration
def test_routines_all_schemas_excludes_system(pg, monkeypatch):
    cfg = parse_config(
        "sources:\n  - name: wh\n    type: postgres\n    timeout_seconds: 60\n"
        f"    conn: {{host: 127.0.0.1, port: {pg['port']}, dbname: shop, user: readonly_agent, "
        "password: \"env:DEA_TEST_PG_PW\", sslmode: disable}\n"
        "    options: {top_tables: 3}\n")
    monkeypatch.setenv("DEA_TEST_PG_PW", pg["ro_pw"])
    res = run_source(cfg.sources[0])
    assert res["errors"] == [], res["errors"]
    schemas = {r["schema"] for r in res["data"]["routines"]}
    assert schemas == {"shop"}, schemas  # no pg_catalog / information_schema / extension objects


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("psql"), reason="psql not installed")
def test_collector_same_result_via_psql_backend(pg, monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "psycopg", None)  # force the psql CLI backend
    res = run_source(_pg_source(pg, monkeypatch))
    assert res["errors"] == [], res["errors"]
    assert any(t["name"] == "orders" for t in res["data"]["tables"])


@pytest.mark.integration
def test_readonly_wrapper_blocks_writes_server_side(pg, monkeypatch):
    """Even if the classifier were bypassed, BEGIN READ ONLY makes the server refuse the write."""
    import psycopg

    with psycopg.connect(host="127.0.0.1", port=pg["port"], dbname="shop", user="readonly_agent",
                         password=pg["ro_pw"], autocommit=True) as c:
        c.execute("BEGIN READ ONLY")
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            c.execute("create table shop.should_fail (x int)")
        c.execute("ROLLBACK")


@pytest.mark.integration
def test_collector_degrades_instead_of_hanging_when_a_table_is_exclusively_locked(pg, monkeypatch):
    """Found by the M1b demo (fault 06): pg_total_relation_size() queues behind ACCESS EXCLUSIVE locks."""
    import psycopg

    kw = dict(host="127.0.0.1", port=pg["port"], dbname="shop", user="postgres")
    admin = psycopg.connect(**kw, password=pg["su_pw"], autocommit=True)
    locker = psycopg.connect(**kw, password=pg["su_pw"], autocommit=False)
    blocked = psycopg.connect(**kw, password=pg["su_pw"], autocommit=True)
    try:
        locker.execute("lock table shop.customers in access exclusive mode")
        t = threading.Thread(target=lambda: _try(blocked, "select count(*) from shop.customers"), daemon=True)
        t.start()
        time.sleep(1.5)
        t0 = time.time()
        res = run_source(_pg_source(pg, monkeypatch))
        took = time.time() - t0
        assert took < 40, f"collector hung for {took:.0f}s"
        assert res["errors"] == [], res["errors"]
        d = res["data"]
        assert d["meta"]["sizes_estimated"] == ["schemas", "tables"]
        # pg_get_viewdef locks what the view reads: the matview over customers is listed without a body, the rest is intact
        assert d["meta"]["routine_view_bodies_skipped"] is True
        rt = {r["name"]: r for r in d["routines"]}
        assert rt["mv_customers"]["body_hash"] == "" and rt["refresh_stats"]["body_hash"] != ""
        assert any(t["name"] == "customers" for t in d["tables"])
        assert len(d["blocking"]) == 1 and "customers" in d["blocking"][0]["blocked_query"]
        sig = signals_for(res)
        assert "1 blocked session(s)" in sig and any("are estimates" in s for s in sig)
    finally:
        locker.rollback()
        locker.close()
        time.sleep(0.5)
        blocked.close()
        admin.close()


def _try(conn, sql):
    try:
        conn.execute(sql)
    except Exception:
        pass


@pytest.mark.integration
def test_dea_query_pg_stat_activity_types_and_column_named_q(pg, monkeypatch):
    """M4.1: `dea query` on pg_stat_activity crashed (TypeError) when a result column was named `q`; every PG type
    (inet, interval, xid, timestamptz, arrays, jsonb, bytea) must serialize through both backends."""
    from de_agent.sqlexec import query_limited

    src = _pg_source(pg, monkeypatch)
    sqls = [
        "select pid, state, xact_start, now() - xact_start as age, left(query, 80) q, client_addr, backend_xid, "
        "backend_xmin, query_start from pg_stat_activity where datname = 'shop'",
        "select ARRAY[1,2,3] a, '10.0.0.1'::inet i, interval '3 min' d, now() t, '{\"a\":1}'::jsonb j, "
        "'\\xdead'::bytea b, '1'::xid x, 'q'::text q",
    ]
    for backend in ("psycopg", "psql"):
        if backend == "psql":
            if not shutil.which("psql"):
                continue
            monkeypatch.setitem(__import__("sys").modules, "psycopg", None)
        for sql in sqls:
            rows, total = query_limited(src.conn, sql, "local", 30, 50)
            assert total == len(rows) >= 1
            json.dumps(rows, default=str)
    rows, _ = query_limited(src.conn, "select 'x'::text q", "local", 30, 5)
    assert rows == [{"q": "x"}]


def test_nolock_size_math_is_bigint():
    """relpages is int4: relpages * 8192 overflows for tables over 2 GB unless cast first."""
    from de_agent.collectors import postgres as pgc

    sql = pgc.q_tables_nolock(["public"], 10)
    assert "relpages * 8192" not in sql
    assert sql.count("c.relpages::bigint * 8192") == 2
