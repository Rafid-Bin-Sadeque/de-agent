import subprocess

import pytest
from de_agent import sqlexec
from de_agent.collectors.base import CollectorError
from de_agent.config import ConnConfig

CONN = ConnConfig(host="db.example", port=5433, dbname="shop", user="readonly_agent", password="env:X")


def test_wrapper_composition():
    script = sqlexec.wrap_readonly("select 1 as a;", 30)
    lines = script.strip().splitlines()
    assert lines[0] == "BEGIN READ ONLY;"
    assert lines[1] == "SET LOCAL statement_timeout = 30000;"
    assert lines[2] == "SET LOCAL lock_timeout = 3000;"
    assert lines[3] == "select coalesce(json_agg(__dea_q), '[]'::json) from (select 1 as a) __dea_q;"
    assert lines[4] == "COMMIT;"


def test_collector_write_refused_before_execution(monkeypatch):
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(1))
    for bad in ("delete from shop.orders", "select 1; drop table shop.orders", "vacuum shop.orders",
                "select pg_terminate_backend(1)", "select * into shop.copy from shop.orders", "explain analyze select 1"):
        with pytest.raises(CollectorError, match="is a write"):
            sqlexec.run_query(CONN, bad, "local", 5)
    assert called == []


def test_side_effect_function_label():
    with pytest.raises(CollectorError, match=r"SELECT calling pg_terminate_backend\(\) is a write"):
        sqlexec.check_read_only("select pg_terminate_backend(1)")


def test_reads_pass_the_check():
    sqlexec.check_read_only("select 1")
    sqlexec.check_read_only("with t as (select 1 as x) select * from t")


def test_psql_argv_never_contains_password_local():
    argv, env, stdin = sqlexec.build_psql(CONN, "select 1", "local", 10, "s3cr3t-pw")
    assert not any("s3cr3t-pw" in a for a in argv)
    assert "s3cr3t-pw" not in stdin
    assert env["PGPASSWORD"] == "s3cr3t-pw"
    assert argv[:7] == ["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1"]
    assert argv[-2:] == ["-f", "-"]


def test_psql_pgpass_sets_no_password_env():
    _, env, _ = sqlexec.build_psql(CONN, "select 1", "local", 10, None)
    assert "PGPASSWORD" not in env


def test_ssh_sql_on_stdin_not_in_remote_command():
    argv, env, stdin = sqlexec.build_psql(CONN, "select 'x; rm -rf /'", "ssh:bastion", 10, None)
    assert argv[:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    assert argv[5] == "bastion"
    remote = argv[6]
    assert "rm -rf" not in remote and "select" not in remote
    assert "psql" in remote and "-f -" in remote
    assert "rm -rf" in stdin and "BEGIN READ ONLY" in stdin
    assert "PGPASSWORD" not in env and "PGPASSWORD" not in remote


def test_redaction():
    assert sqlexec.redact("fail: s3cr3t-pw here", ["s3cr3t-pw"]) == "fail: *** here"
    assert "pw123" not in sqlexec.redact("postgres://u:pw123@host/db failed")
    assert "pw123" not in sqlexec.redact("conn password=pw123 refused")


def test_psql_error_is_redacted(monkeypatch):
    class P:
        returncode = 2
        stdout = ""
        stderr = "psql: error: connection failed: password authentication failed for s3cr3t-pw\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: P())
    monkeypatch.setenv("X", "s3cr3t-pw")
    monkeypatch.setitem(__import__("sys").modules, "psycopg", None)  # force the psql backend
    with pytest.raises(CollectorError) as ei:
        sqlexec.run_query(CONN, "select 1", "local", 5)
    assert "s3cr3t-pw" not in str(ei.value) and "***" in str(ei.value)


def test_psql_backend_parses_json(monkeypatch):
    class P:
        returncode = 0
        stdout = '[{"a": 1}, {"a": 2}]\n'
        stderr = ""

    seen = {}

    def fake(argv, **kw):
        seen["argv"], seen["env"], seen["input"] = argv, kw["env"], kw["input"]
        return P()

    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setenv("X", "pw")
    monkeypatch.setitem(__import__("sys").modules, "psycopg", None)
    assert sqlexec.run_query(CONN, "select 1 as a", "local", 5) == [{"a": 1}, {"a": 2}]
    assert seen["env"]["PGPASSWORD"] == "pw" and "pw" not in " ".join(seen["argv"])
    assert seen["input"].startswith("BEGIN READ ONLY;")


def test_nonjson_output_is_an_error(monkeypatch):
    class P:
        returncode = 0
        stdout = "oops\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: P())
    monkeypatch.setenv("X", "pw")
    monkeypatch.setitem(__import__("sys").modules, "psycopg", None)
    with pytest.raises(CollectorError, match="not JSON"):
        sqlexec.run_query(CONN, "select 1", "local", 5)


def test_missing_secret_env_is_collector_error(monkeypatch):
    monkeypatch.delenv("X", raising=False)
    with pytest.raises(CollectorError, match="not set"):
        sqlexec.run_query(CONN, "select 1", "local", 5)
