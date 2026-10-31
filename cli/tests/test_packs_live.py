"""Live validation of the two `validated: live` packs against throwaway containers (docker postgres:16, mysql:8.4).

Skipped when docker or the drivers are unavailable. Every secret here is generated per run and lives in a container that is
removed at teardown."""
import functools
import secrets
import shutil
import subprocess
import threading
import time

import pytest
from de_agent.collectors import pack_sql
from de_agent.config import parse_config
from de_agent.packs import load_pack
from de_agent.packs.loader import set_hub_hint
from de_agent.render import render, signals_for
from de_agent.runner import result_status, run_source

pytestmark = pytest.mark.integration


@functools.lru_cache(maxsize=1)
def _docker_ok():
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _port(name, inner):
    return subprocess.run(["docker", "port", name, f"{inner}/tcp"], capture_output=True, text=True,
                          timeout=20).stdout.strip().splitlines()[0].rsplit(":", 1)[1]


@pytest.fixture(autouse=True)
def _hint_reset():
    set_hub_hint(None)
    yield
    set_hub_hint(None)


def _hub_with_fast_threshold(tmp_path, name, old, new):
    """A hub-local copy of a built-in pack with a shorter 'long running' threshold (also proves hub packs override built-ins)."""
    from de_agent.packs.loader import find_pack

    text = find_pack(name).text
    assert old in text
    hub = tmp_path / "hub"
    (hub / "packs").mkdir(parents=True)
    (hub / "packs" / f"{name}.yaml").write_text(text.replace(old, new))
    (hub / "de-agent.yaml").write_text("version: 1\n")
    set_hub_hint(hub)
    return hub


# ------------------------------------------------------------------------------------------------ PostgreSQL 16

@pytest.fixture(scope="module")
def pg16():
    psycopg = pytest.importorskip("psycopg")
    pytest.importorskip("sqlalchemy")
    if not _docker_ok():
        pytest.skip("docker is not available")
    name = "dea-pack-pg-" + secrets.token_hex(4)
    su_pw, ro_pw = secrets.token_hex(8), secrets.token_hex(8)
    run = subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-e", f"POSTGRES_PASSWORD={su_pw}", "-e", "POSTGRES_DB=shop",
                          "-p", "127.0.0.1::5432", "postgres:16", "-c", "autovacuum=off"], capture_output=True, text=True, timeout=600)
    if run.returncode != 0:
        pytest.skip(f"cannot start postgres:16: {run.stderr.strip()[-200:]}")
    holders = []
    try:
        port = int(_port(name, 5432))
        kw = {"host": "127.0.0.1", "port": port, "dbname": "shop", "user": "postgres", "password": su_pw, "autocommit": True}
        for _ in range(60):
            try:
                psycopg.connect(**kw, connect_timeout=2).close()
                break
            except Exception:
                time.sleep(1)
        else:
            pytest.skip("postgres:16 did not become ready")
        with psycopg.connect(**kw) as c:
            cur = c.cursor()
            cur.execute(f"create role readonly_agent login password '{ro_pw}'")
            cur.execute("grant pg_read_all_stats, pg_read_all_data to readonly_agent")  # a monitoring role: reads only
            cur.execute("create schema shop")
            cur.execute("create table shop.orders (id serial primary key, amount numeric, note text) with (autovacuum_enabled = false)")
            cur.execute("insert into shop.orders(amount, note) select g, md5(g::text) from generate_series(1, 150000) g")
            cur.execute("create table shop.locked_t (id int primary key, note text)")
            cur.execute("insert into shop.locked_t values (1, 'a'), (2, 'b')")
            cur.execute("delete from shop.orders where id % 5 in (0, 1)")  # 40% dead tuples
            cur.execute("analyze shop.orders")
            for _ in range(30):
                cur.execute("select n_dead_tup from pg_stat_user_tables where relname = 'orders'")
                if (cur.fetchone() or [0])[0] > 0:
                    break
                time.sleep(1)
        sleeper = psycopg.connect(**kw)
        threading.Thread(target=lambda: _quiet(lambda: sleeper.execute("select pg_sleep(300)")), daemon=True).start()
        locker = psycopg.connect(**{**kw, "autocommit": False})
        locker.execute("lock table shop.locked_t in access exclusive mode")  # sizes must still work (the pack takes no relation locks)
        blocked = psycopg.connect(**kw)
        threading.Thread(target=lambda: _quiet(lambda: blocked.execute("update shop.locked_t set note = 'x' where id = 2")), daemon=True).start()
        holders += [sleeper, locker, blocked]
        time.sleep(4)
        yield {"port": port, "ro_pw": ro_pw, "su_pw": su_pw}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)  # first: a blocked/sleeping session would stall close()
        for h in holders:
            _quiet(h.close)


def _quiet(fn):
    try:
        fn()
    except Exception:
        pass


def _pg_source(pg, monkeypatch):
    monkeypatch.setenv("DEA_TEST_PG_PW", pg["ro_pw"])
    return parse_config(
        "sources:\n  - name: wh\n    type: sql\n    pack: postgres-lite\n    timeout_seconds: 60\n"
        f"    conn: {{url: 'postgresql+psycopg://readonly_agent@127.0.0.1:{pg['port']}/shop?sslmode=disable', password: 'env:DEA_TEST_PG_PW'}}\n").sources[0]


def test_postgres_lite_runs_every_section_against_pg16(pg16, monkeypatch):
    res = run_source(_pg_source(pg16, monkeypatch))
    assert res["errors"] == [], res["errors"]
    d = res["data"]
    assert set(d) - {"meta"} == {"long_running_sessions", "blocked_sessions", "dead_tuples", "table_sizes"}
    orders = next(t for t in d["dead_tuples"] if t["table"] == "orders")
    assert orders["n_dead_tup"] >= 50000 and orders["dead_pct"] >= 20
    assert any(t["table"] == "locked_t" for t in d["table_sizes"])  # an ACCESS EXCLUSIVE lock does not block the size section
    assert all(t["est_bytes"] >= 0 for t in d["table_sizes"])
    assert any(b["blocker_pid"] and b["blocked_pid"] for b in d["blocked_sessions"]), d["blocked_sessions"]
    assert "1 blocked session(s)" in signals_for(res)
    assert "validated: live" in render(res)


def test_postgres_lite_long_running_sessions_with_a_short_threshold(pg16, monkeypatch, tmp_path):
    _hub_with_fast_threshold(tmp_path, "postgres-lite", "interval '5 minutes'", "interval '2 seconds'")
    assert load_pack("postgres-lite").sections[0].sql.count("2 seconds") == 1
    res = run_source(_pg_source(pg16, monkeypatch))
    assert res["errors"] == [], res["errors"]
    rows = res["data"]["long_running_sessions"]
    assert any("pg_sleep" in r["query"] and r["duration_s"] >= 2 for r in rows), rows
    assert any("session(s) running" in s for s in signals_for(res))


def test_the_server_itself_refuses_writes_inside_the_pack_transaction_pg(pg16, monkeypatch):
    """Even if classification were bypassed, the read-only transaction makes PostgreSQL refuse the write."""
    src = _pg_source(pg16, monkeypatch)
    sess = pack_sql.SqlSession(src, load_pack("postgres-lite"), 30)
    try:
        with pytest.raises(Exception, match="(?i)read.only"):
            sess.query("insert into shop.orders(amount) values (1)", {}, {}, 5)
        assert sess.query("select 1 as ok", {}, {}, 5) == [{"ok": 1}]  # the session is still usable after the refusal
    finally:
        sess.close()


# ------------------------------------------------------------------------------------------------ MySQL 8.4

@pytest.fixture(scope="module")
def mysql84():
    pymysql = pytest.importorskip("pymysql")
    pytest.importorskip("sqlalchemy")
    if not _docker_ok():
        pytest.skip("docker is not available")
    name = "dea-pack-my-" + secrets.token_hex(4)
    root_pw, mon_pw = secrets.token_hex(8), secrets.token_hex(8)
    run = subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-e", f"MYSQL_ROOT_PASSWORD={root_pw}", "-e", "MYSQL_DATABASE=shop",
                          "-p", "127.0.0.1::3306", "mysql:8.4"], capture_output=True, text=True, timeout=900)
    if run.returncode != 0:
        pytest.skip(f"cannot start mysql:8.4: {run.stderr.strip()[-200:]}")
    holders = []
    try:
        port = int(_port(name, 3306))
        kw = {"host": "127.0.0.1", "port": port, "user": "root", "password": root_pw, "database": "shop", "autocommit": True}
        for _ in range(120):
            try:
                pymysql.connect(**kw, connect_timeout=2).close()
                break
            except Exception:
                time.sleep(1)
        else:
            pytest.skip("mysql:8.4 did not become ready")
        time.sleep(3)
        root = pymysql.connect(**kw)
        cur = root.cursor()
        cur.execute("create table orders (id int primary key, note varchar(50), amount int) engine=innodb")
        cur.execute("insert into orders values (1, 'a', 10), (2, 'b', 20), (3, 'c', 30)")
        cur.execute(f"create user 'mon'@'%' identified by '{mon_pw}'")
        cur.execute("grant process, replication client on *.* to 'mon'@'%'")
        cur.execute("grant select on shop.* to 'mon'@'%'")
        cur.execute("grant select, execute on sys.* to 'mon'@'%'")
        cur.execute("grant select on performance_schema.* to 'mon'@'%'")
        sleeper = pymysql.connect(**kw)
        threading.Thread(target=lambda: _quiet(lambda: sleeper.cursor().execute("select sleep(300)")), daemon=True).start()
        holder = pymysql.connect(**{**kw, "autocommit": False})
        holder.cursor().execute("update orders set note = 'locked' where id = 1")
        waiter = pymysql.connect(**kw)
        threading.Thread(target=lambda: _quiet(lambda: waiter.cursor().execute("update orders set note = 'w' where id = 1")), daemon=True).start()
        holders += [root, sleeper, holder, waiter]
        time.sleep(4)
        yield {"port": port, "mon_pw": mon_pw, "root_pw": root_pw}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)  # first: a blocked/sleeping session would stall close()
        for h in holders:
            _quiet(h.close)


def _my_source(my, monkeypatch):
    monkeypatch.setenv("DEA_TEST_MY_PW", my["mon_pw"])
    return parse_config(
        "sources:\n  - name: shopdb\n    type: sql\n    pack: mysql\n    timeout_seconds: 60\n"
        f"    conn: {{url: 'mysql+pymysql://mon@127.0.0.1:{my['port']}/shop', password: 'env:DEA_TEST_MY_PW'}}\n").sources[0]


def test_mysql_pack_runs_every_section_against_mysql_84(mysql84, monkeypatch):
    res = run_source(_my_source(mysql84, monkeypatch))
    assert res["errors"] == [], res["errors"]
    d = res["data"]
    assert set(d) - {"meta"} == {"long_processlist", "innodb_lock_waits", "replica_status", "top_tables"}
    assert d["replica_status"] == []  # a primary has no replica status: empty, not an error
    assert any(t["table_name"] == "orders" and t["table_schema"] == "shop" for t in d["top_tables"])
    assert any(w["locked_table"].endswith("`orders`") for w in d["innodb_lock_waits"]), d["innodb_lock_waits"]
    assert "1 InnoDB lock wait(s)" in signals_for(res)
    assert result_status(res) == "ok" and "validated: live" in render(res)


def test_mysql_long_processlist_with_a_short_threshold(mysql84, monkeypatch, tmp_path):
    _hub_with_fast_threshold(tmp_path, "mysql", "time > 300", "time > 2")
    res = run_source(_my_source(mysql84, monkeypatch))
    assert res["errors"] == [], res["errors"]
    assert any("sleep(300)" in (r["info"] or "") for r in res["data"]["long_processlist"]), res["data"]["long_processlist"]


def test_mysql_replica_status_signals_use_the_documented_columns(mysql84, monkeypatch):
    """`SHOW REPLICA STATUS` works through the generic engine (classified as a read in the mysql dialect)."""
    sess = pack_sql.SqlSession(_my_source(mysql84, monkeypatch), load_pack("mysql"), 30)
    try:
        assert sess.query("show replica status", {}, {}, 5) == []
    finally:
        sess.close()


def test_the_server_itself_refuses_writes_inside_the_pack_transaction_mysql(mysql84, monkeypatch):
    """Use ROOT (which may write): only the READ ONLY transaction stands between the statement and the table."""
    monkeypatch.setenv("DEA_TEST_MY_ROOT_PW", mysql84["root_pw"])
    src = parse_config(
        "sources:\n  - name: shopdb\n    type: sql\n    pack: mysql\n    timeout_seconds: 60\n"
        f"    conn: {{url: 'mysql+pymysql://root@127.0.0.1:{mysql84['port']}/shop', password: 'env:DEA_TEST_MY_ROOT_PW'}}\n").sources[0]
    sess = pack_sql.SqlSession(src, load_pack("mysql"), 30)
    try:
        with pytest.raises(Exception, match="(?i)read.only"):
            sess.query("insert into orders values (99, 'x', 1)", {}, {}, 5)
        assert sess.query("select count(*) as n from orders", {}, {}, 5) == [{"n": 3}]  # nothing was written; session still usable
    finally:
        sess.close()
