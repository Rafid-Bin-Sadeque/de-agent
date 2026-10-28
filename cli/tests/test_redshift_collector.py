"""Redshift collector (M3 section 1). Fixtures are hand-built from documented view columns, NOT recorded from a cluster."""
import json
import re
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
import redshift_fixtures as fx
import sqlglot
from aws_fixtures import REGION, SeenCalls, guarded_session, stubbed
from de_agent import sqlexec
from de_agent.collectors import redshift as rs
from de_agent.collectors.aws import _aws_core as core
from de_agent.collectors.base import CollectorError, RunContext
from de_agent.config import ConfigError, parse_config
from de_agent.diff import diff_results
from de_agent.render import redshift_signals, render, signals_for
from de_agent.runner import result_status
from de_agent.sqlclass import classify
from moto import mock_aws

GOLDEN = Path(__file__).parent / "golden"


def _src(extra="    conn: {host: acme-dwh.example.internal, port: 5439, dbname: dev, user: readonly_agent, password: pgpass}\n",
         options="{}"):
    return parse_config(f"sources:\n  - name: dwh\n    type: redshift\n    timeout_seconds: 600\n{extra}    options: {options}\n").sources[0]


def collect(answers, log=None, source=None, **ctx_kw):
    src = source or _src()
    ctx = RunContext(timeout_seconds=600, query_fn=fx.fake_query(answers, log), **ctx_kw)
    out = rs.RedshiftCollector().collect(src, ctx)
    return {"source": "dwh", "type": "redshift", "schema_version": 1, "collected_at": "2026-10-19T12:00:00Z",
            "duration_s": 1.0, **out}


def pin_meta(res):
    """Timings differ per run: pin them so renders are byte-stable."""
    for v in res["data"]["meta"]["sections"].values():
        v["seconds"] = 0.1
    return res


# ---------------------------------------------------------------- SQL hygiene

@pytest.mark.parametrize("variant", rs.all_queries(), ids=lambda v: v.source)
def test_every_sql_parses_as_redshift_and_is_a_read(variant):
    assert len(sqlglot.parse(variant.sql, read="redshift")) == 1
    stmts = classify(variant.sql, "redshift")
    assert [s.kind for s in stmts] == ["read"], variant.sql
    sqlexec.check_read_only(variant.sql, "redshift")


def test_redshift_has_no_json_agg_or_lock_timeout():
    for v in rs.all_queries():
        assert "json_agg" not in v.sql.lower() and "lock_timeout" not in v.sql.lower()


def test_every_section_has_a_sys_primary_and_stl_fallback_where_one_exists():
    pairs = {"users": rs.v_users(), "types": rs.v_types(), "totals": rs.v_totals(), "top": rs.v_top_queries(15),
             "spill": rs.v_spill(), "concurrency": rs.v_concurrency(), "blocked": rs.v_blocked(),
             "load_errors": rs.v_load_errors(), "maintenance": rs.v_maintenance()}
    for name, variants in pairs.items():
        assert variants[0].source.startswith("sys_") or name == "blocked", name
        assert len(variants) == 2, name
    assert [v.source for v in rs.v_commit_queue()] == ["stl_commit_stats"]  # no SYS_ equivalent


# ---------------------------------------------------------------- collector: healthy, degraded, golden

def test_healthy_cluster_has_all_sections_and_no_signals():
    res = collect(fx.healthy())
    assert res["errors"] == [] and result_status(res) == "ok"
    d = res["data"]
    assert d["cluster"] == {"version": fx.VERSION, "kind": "provisioned", "nodes": 2, "slices": 4}
    assert d["workload_24h"]["queries"] == 99000
    users = {u["user"]: u for u in d["workload_users"]}
    assert users["acme_etl"]["share_pct"] == 58.2 and users["acme_dms"]["query_count"] == 90000
    assert d["top_queries"][0]["count"] == 900 and len(d["top_queries"][0]["key"]) == 10
    assert d["pressure_24h"] == {"spilled_queries": 0, "spilled_mb": 0, "commit_queue_p95_s": 0.4}
    assert d["tables"][0]["name"] == "orders" and d["locks"] == [] and d["load_errors_24h"] == []
    assert {m["operation"] for m in d["maintenance"]} == {"vacuum", "analyze"}
    assert signals_for(res) == []
    json.dumps(res)  # JSON-clean
    meta = d["meta"]["sections"]
    assert meta["workload_users"]["source"] == "sys_query_history" and "fallback_from" not in meta["workload_users"]
    assert all(v["seconds"] >= 0 for v in meta.values())  # per-section timings are reported


def test_degraded_cluster_fires_every_signal():
    sig = signals_for(collect(fx.degraded()))
    joined = "\n".join(sig)
    assert "workload is dominated by acme_etl (91% of exec time in 24h); check what acme_etl is before attributing" in joined
    assert "queue wait p95 45 s (> 30 s)" in joined
    assert "7 queries spilled 2.0 GB to disk in 24h" in joined
    assert "commit queue wait p95 12.5 s (> 10 s)" in joined
    assert "2 table(s) over 20% unsorted: acme.orders, acme.tiny_lookup" in joined
    assert "2 table(s) with stats_off > 10: acme.orders, acme.tiny_lookup" in joined
    assert "1 table(s) over 1 GB with skew_rows > 4: acme.orders" in joined  # tiny_lookup is below 1 GB
    assert "1 blocked session(s)" in joined
    assert "15 load error(s) in 24h (acme.events, acme.orders)" in joined


def test_signal_thresholds_are_strict_boundaries():
    base = {"workload_users": [{"user": "u", "share_pct": 70.0}], "workload_24h": {"queue_p95_s": 30.0},
            "pressure_24h": {"spilled_mb": 1024.0, "commit_queue_p95_s": 10.0},
            "tables": [{"schema": "s", "name": "t", "size_mb": 1024, "unsorted_pct": 20.0, "stats_off": 10.0, "skew_rows": 4.0}]}
    assert redshift_signals(base) == []  # exactly at the thresholds: no signal
    base["workload_users"][0]["share_pct"] = 70.1
    assert len(redshift_signals(base)) == 1


def test_render_golden_redshift_page():
    md = render(pin_meta(collect(fx.degraded())))
    golden = GOLDEN / "redshift_degraded.md"
    if not golden.exists():  # first run writes it; review the diff in the commit
        golden.write_text(md)
    assert md == golden.read_text()
    assert render(pin_meta(collect(fx.healthy()))) == render(pin_meta(collect(fx.healthy())))  # deterministic


def test_healthy_golden():
    md = render(pin_meta(collect(fx.healthy())))
    golden = GOLDEN / "redshift_healthy.md"
    if not golden.exists():
        golden.write_text(md)
    assert md == golden.read_text()


def test_diff_between_healthy_and_degraded_reports_new_signals_and_blocked():
    d = diff_results(collect(fx.healthy()), collect(fx.degraded()))
    assert any("dominated by acme_etl" in s for s in d["signals_new"])
    assert any("blocked session" in s for s in d["signals_new"])
    assert "locks" in d["lists"] and d["lists"]["locks"]["added"][0]["key"].startswith("blocked.31002")
    assert diff_results(collect(fx.healthy()), collect(fx.healthy()))["lists"] == {}


# ---------------------------------------------------------------- fallbacks and failures

def _missing(view):
    return CollectorError(f'relation "{view}" does not exist')


@pytest.mark.parametrize("name,primary,fallback_rows,check", [
    ("workload_users", rs.v_users, [{"user_name": "acme_bi", "query_count": 3, "exec_s": 9.0}],
     lambda d: d["workload_users"][0]["user"] == "acme_bi"),
    ("workload_types", rs.v_types, [{"query_type": "SELECT", "query_count": 3, "exec_s": 9.0}],
     lambda d: d["workload_types"][0]["query_type"] == "SELECT"),
    ("workload_24h", rs.v_totals, [{"queries": 3, "exec_s": 9.0, "queue_p50_s": 0.1, "queue_p95_s": 0.2}],
     lambda d: d["workload_24h"]["queries"] == 3),
    ("top_queries", lambda: rs.v_top_queries(15), [{"query": "select ?", "n": 3, "total_exec_s": 9.0, "avg_exec_s": 3.0, "max_exec_s": 4.0}],
     lambda d: d["top_queries"][0]["query"] == "select ?"),
    ("pressure_spill", rs.v_spill, [{"spilled_queries": 2, "spilled_mb": 5000}],
     lambda d: d["pressure_24h"]["spilled_mb"] == 5000),
    ("concurrency_24h", rs.v_concurrency, [{"hour": "2026-10-19 10:00:00", "peak": 3}],
     lambda d: d["concurrency_24h"][0]["peak"] == 3),
    ("locks_blocked", rs.v_blocked, [{"pid": 1, "blocking_pid": 2, "object_type": "Waiting for write lock", "relation_id": 9, "wait_s": 5}],
     lambda d: d["locks"][0]["kind"] == "blocked" and d["locks"][0]["pid"] == 1),
    ("load_errors_24h", rs.v_load_errors, [{"table_name": "acme.t", "column_name": "c", "error": "bad", "n": 1}],
     lambda d: d["load_errors_24h"][0]["table"] == "acme.t"),
    ("maintenance", rs.v_maintenance, [{"operation": "vacuum", "runs": 1, "tables": 1, "last_at": "2026-10-19 01:00:00"}],
     lambda d: d["maintenance"][0]["runs"] == 1),
])
def test_missing_sys_view_falls_back_to_stl_and_records_the_source(name, primary, fallback_rows, check):
    a = fx.healthy()
    variants = primary()
    a[variants[0].sql] = _missing(variants[0].source.split("+")[0])
    a[variants[1].sql] = fallback_rows
    res = collect(a)
    assert res["errors"] == [], res["errors"]
    assert check(res["data"])
    info = res["data"]["meta"]["sections"][name]
    assert info["source"] == variants[1].source and info["fallback_from"] == [variants[0].source]


def test_both_variants_failing_reports_the_section_and_the_rest_continue():
    a = fx.healthy()
    u = rs.v_users()
    a[u[0].sql] = CollectorError("permission denied for view sys_query_history")
    a[u[1].sql] = _missing("stl_wlm_query")
    res = collect(a)
    assert result_status(res) == "partial"
    assert len(res["errors"]) == 1 and res["errors"][0].startswith("workload_users: sys_query_history: permission denied")
    assert "stl_wlm_query" in res["errors"][0]
    assert "workload_users" not in res["data"] and "tables" in res["data"] and "top_queries" in res["data"]
    assert res["data"]["meta"]["sections"]["workload_users"]["source"] is None


def test_timeout_in_one_section_does_not_stop_the_others():
    a = fx.healthy()
    a[rs.q_tables(50)[0].sql] = CollectorError("query timed out after 60s")
    res = collect(a)
    assert any(e.startswith("tables:") and "timed out" in e for e in res["errors"])
    assert "workload_users" in res["data"] and "pressure_24h" in res["data"]


def test_section_timeout_defaults_to_60s_and_never_exceeds_the_source_budget():
    log = []
    collect(fx.healthy(), log)
    ts = [t for s, t in log if s != rs.Q_MINQID.sql]  # the spill-bound probe runs under the heavy cap
    assert min(ts) == 60 and max(ts) == rs.DEFAULT_HEAVY_TIMEOUT_S
    assert {t for _, t in log} == {60, rs.DEFAULT_HEAVY_TIMEOUT_S}
    log.clear()
    src = _src(options="{section_timeout_seconds: 5, heavy_section_timeout_s: 5}")
    collect(fx.healthy(), log, source=src)
    assert {t for _, t in log} == {5}
    ctx = RunContext(timeout_seconds=30, query_fn=fx.fake_query(fx.healthy(), log))
    log.clear()
    rs.RedshiftCollector().collect(_src(), ctx)
    assert max(t for _, t in log) <= 30


def test_source_budget_exhausted_is_reported_per_section():
    ctx = RunContext(timeout_seconds=0.0001, query_fn=fx.fake_query(fx.healthy()))
    import time

    time.sleep(0.01)
    out = rs.RedshiftCollector().collect(_src(), ctx)
    assert any("source timeout budget exhausted" in e for e in out["errors"])


def test_serverless_probe_when_stv_slices_is_missing():
    a = fx.healthy()
    a[rs.Q_SLICES.sql] = _missing("stv_slices")
    a[rs.Q_SERVERLESS.sql] = [{"n": 3}]
    res = collect(a)
    assert res["data"]["cluster"]["kind"] == "serverless" and "nodes" not in res["data"]["cluster"]
    a[rs.Q_SERVERLESS.sql] = _missing("sys_serverless_usage")
    assert collect(a)["data"]["cluster"]["kind"] == "unknown"


def test_options_validation():
    with pytest.raises(ConfigError, match="unknown redshift options"):
        _src(options="{bogus: 1}")
    with pytest.raises(ConfigError, match="positive integer"):
        _src(options="{top_tables: 0}")


def test_config_validation_of_transports():
    with pytest.raises(ConfigError, match="exactly one of `conn:`"):
        parse_config("sources:\n  - {name: r, type: redshift}\n")
    with pytest.raises(ConfigError, match="exactly one of cluster_id"):
        parse_config("sources:\n  - {name: r, type: redshift, aws: {region: x}, data_api: {database: dev, db_user: u}}\n")
    with pytest.raises(ConfigError, match="needs an `aws:` block"):
        parse_config("sources:\n  - {name: r, type: redshift, data_api: {cluster_id: c, database: dev, db_user: u}}\n")
    with pytest.raises(ConfigError, match="exec: local"):
        parse_config("sources:\n  - {name: r, type: redshift, exec: 'ssh:bastion', aws: {region: x}, "
                     "data_api: {cluster_id: c, database: dev, db_user: u}}\n")
    ok = parse_config("sources:\n  - {name: r, type: redshift, aws: {profile: readonly, region: x}, "
                      "data_api: {workgroup: acme-wg, database: dev, secret_arn: 'env:ACME_SECRET_ARN'}}\n").sources[0]
    assert ok.data_api["workgroup"] == "acme-wg"
    with pytest.raises(ConfigError, match="secret"):  # a literal password is still rejected anywhere in the config
        parse_config("sources:\n  - {name: r, type: redshift, conn: {dbname: d, user: u, password: hunter2}}\n")
    ssh = parse_config("sources:\n  - {name: r, type: redshift, exec: 'ssh:bastion', "
                       "conn: {host: h, port: 5439, dbname: d, user: u, password: pgpass}}\n").sources[0]
    assert ssh.exec == "ssh:bastion"


# ---------------------------------------------------------------- cluster description via the AWS API (moto)

@mock_aws
def test_cluster_describe_provisioned_via_moto():
    boto3.client("redshift", region_name=REGION).create_cluster(
        ClusterIdentifier="acme-dwh", NodeType="ra3.xlplus", MasterUsername="admin", MasterUserPassword="Passw0rd1234",
        NumberOfNodes=2, ClusterType="multi-node")
    src = _src(extra="    conn: {host: acme-dwh.abc.us-east-1.redshift.amazonaws.com, port: 5439, dbname: dev, user: ro, password: pgpass}\n"
                     "    aws: {profile: readonly, region: us-east-1}\n")
    seen = SeenCalls()
    res = collect(fx.healthy(), source=src, aws_session=guarded_session(seen=seen))
    assert res["errors"] == []
    aws = res["data"]["cluster"]["aws"]
    assert aws["kind"] == "provisioned" and aws["node_type"] == "ra3.xlplus" and aws["nodes"] == 2
    assert ("redshift", "DescribeClusters") in seen.ops


def test_cluster_describe_serverless_via_stubber():
    """moto has no redshift-serverless: botocore Stubber with the documented GetWorkgroup shape."""
    from aws_fixtures import StubSession

    base = guarded_session()
    client, stub = stubbed(base, "redshift-serverless")
    stub.add_response("get_workgroup", {"workgroup": {
        "workgroupName": "acme-wg", "status": "AVAILABLE", "baseCapacity": 32, "maxCapacity": 64,
        "publiclyAccessible": False, "enhancedVpcRouting": True,
        "configParameters": [{"parameterKey": "max_query_execution_time", "parameterValue": "3600"}]}},
        {"workgroupName": "acme-wg"})
    stub.activate()
    a = fx.healthy()
    a[rs.Q_SLICES.sql] = _missing("stv_slices")
    a[rs.Q_SERVERLESS.sql] = [{"n": 1}]
    src = parse_config("sources:\n  - {name: dwh, type: redshift, timeout_seconds: 600, aws: {profile: readonly, region: us-east-1}, "
                       "data_api: {workgroup: acme-wg, database: dev}}\n").sources[0]
    res = collect(a, source=src, aws_session=StubSession(base, {"redshift-serverless": client}))
    assert res["errors"] == [], res["errors"]
    stub.assert_no_pending_responses()
    aws = res["data"]["cluster"]["aws"]
    assert aws["kind"] == "serverless" and aws["base_capacity_rpu"] == 32 and aws["max_capacity_rpu"] == 64
    assert aws["max_query_execution_time_s"] == "3600"


def test_cluster_describe_failure_is_reported_not_fatal():
    src = _src(extra="    conn: {host: acme-dwh.abc.us-east-1.redshift.amazonaws.com, port: 5439, dbname: dev, user: ro, password: pgpass}\n"
                     "    aws: {profile: readonly, region: us-east-1}\n")
    base = guarded_session()
    client, stub = stubbed(base, "redshift")
    stub.add_client_error("describe_clusters", "AccessDenied", "User: arn:aws:iam::123456789012:user/ro is not authorized")
    stub.activate()
    from aws_fixtures import StubSession

    res = collect(fx.healthy(), source=src, aws_session=StubSession(base, {"redshift": client}))
    assert len(res["errors"]) == 1 and res["errors"][0].startswith("cluster.aws: AccessDenied")
    assert "123456789012" not in res["errors"][0]
    assert res["data"]["cluster"]["nodes"] == 2  # the SQL-side facts are still there


# ---------------------------------------------------------------- transports: one result set, three paths, one shape

COLS = ["name", "n", "exec_s", "ratio", "last_at", "ok", "note", "moved_at"]
EXPECTED = [{"name": "acme_etl", "n": 5200, "exec_s": 3200.5, "ratio": 12, "last_at": "2026-10-19T09:30:00Z", "ok": True,
             "note": None, "moved_at": "2026-10-19T09:30:00Z"},
            {"name": "acme_bi", "n": 0, "exec_s": 0.25, "ratio": 1.5, "last_at": None, "ok": False, "note": "x,y \"q\"",
             "moved_at": None}]
TYPES = {"n": "int", "exec_s": "float", "ratio": "float", "last_at": "ts", "ok": "bool", "moved_at": "ts"}


class _Col:
    def __init__(self, name):
        self.name = name


class FakeCursor:
    def __init__(self, rows, log, fail=None):
        self.rows, self.log, self.fail, self.description = rows, log, fail, [_Col(c) for c in COLS]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql):
        self.log.append(sql)
        if self.fail and sql == self.fail[0]:
            raise RuntimeError(self.fail[1])

    def fetchall(self):
        return self.rows


class FakePsycopg:
    def __init__(self, rows, fail=None):
        self.rows, self.log, self.kwargs, self.fail = rows, [], None, fail

    def connect(self, **kw):
        self.kwargs = kw
        outer = self

        class Conn:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

            def cursor(self_):
                return FakeCursor(outer.rows, outer.log, outer.fail)

        return Conn()


PG_ROWS = [("acme_etl", 5200, Decimal("3200.50"), Decimal("12.0"), datetime(2026, 10, 19, 9, 30), True, None,
            datetime(2026, 10, 19, 11, 30, tzinfo=timezone(__import__("datetime").timedelta(hours=2)))),
           ("acme_bi", 0, Decimal("0.25"), 1.5, None, False, 'x,y "q"', None)]
CSV = ('name,n,exec_s,ratio,last_at,ok,note,moved_at\n'
       'acme_etl,5200,3200.50,12.0,2026-10-19 09:30:00,t,,2026-10-19 11:30:00+02\n'
       'acme_bi,0,0.25,1.5,,f,"x,y ""q""",\n')
API_COLS = [{"label": "name", "typeName": "varchar"}, {"label": "n", "typeName": "int4"},
            {"label": "exec_s", "typeName": "numeric"}, {"label": "ratio", "typeName": "numeric"},
            {"label": "last_at", "typeName": "timestamp"}, {"label": "ok", "typeName": "bool"},
            {"label": "note", "typeName": "varchar"}, {"label": "moved_at", "typeName": "timestamptz"}]
API_RECORDS = [
    [{"stringValue": "acme_etl"}, {"longValue": 5200}, {"stringValue": "3200.50"}, {"stringValue": "12.0"},
     {"stringValue": "2026-10-19 09:30:00"}, {"booleanValue": True}, {"isNull": True}, {"stringValue": "2026-10-19 11:30:00+02"}],
    [{"stringValue": "acme_bi"}, {"longValue": 0}, {"stringValue": "0.25"}, {"doubleValue": 1.5}, {"isNull": True},
     {"booleanValue": False}, {"stringValue": 'x,y "q"'}, {"isNull": True}]]


def test_golden_one_result_set_through_all_three_transports():
    fake = FakePsycopg(PG_ROWS)
    cur_rows = sqlexec._rs_psycopg(fake, _src().conn, "select 1", 60, None, None)
    csv_rows = sqlexec.parse_csv_rows(CSV, TYPES)
    api_rows = sqlexec.rows_from_data_api(API_COLS, API_RECORDS)
    assert cur_rows == EXPECTED
    assert csv_rows == EXPECTED
    assert api_rows == EXPECTED
    assert json.dumps(cur_rows, sort_keys=True) == json.dumps(csv_rows, sort_keys=True) == json.dumps(api_rows, sort_keys=True)


def test_psycopg_path_wraps_in_a_read_only_transaction_and_always_rolls_back():
    fake = FakePsycopg(PG_ROWS)
    sqlexec._rs_psycopg(fake, _src().conn, "select 1;", 60, "s3cret", None)
    assert fake.log == ["BEGIN READ ONLY", "SET LOCAL statement_timeout TO 60000", "select 1", "ROLLBACK"]
    assert fake.kwargs["autocommit"] is True and fake.kwargs["prepare_threshold"] is None
    failing = FakePsycopg(PG_ROWS, fail=("select 1", "canceling statement due to statement timeout, password=s3cret"))
    with pytest.raises(CollectorError) as ex:
        sqlexec._rs_psycopg(failing, _src().conn, "select 1", 60, "s3cret", None)
    assert failing.log[-1] == "ROLLBACK" and "s3cret" not in str(ex.value) and "statement timeout" in str(ex.value)


def test_psql_csv_path_argv_env_and_script(monkeypatch):
    seen = {}

    def fake_run(argv, input=None, capture_output=None, text=None, env=None, timeout=None):
        seen.update(argv=argv, input=input, env=env, timeout=timeout)
        return subprocess.CompletedProcess(argv, 0, stdout=CSV, stderr="")

    monkeypatch.setattr(sqlexec.subprocess, "run", fake_run)
    rows = sqlexec._rs_psql(_src().conn, "select 1", "local", 60, "s3cret", TYPES)
    assert rows == EXPECTED
    assert "--csv" in seen["argv"] and "s3cret" not in " ".join(seen["argv"]) and seen["env"]["PGPASSWORD"] == "s3cret"
    assert seen["input"].splitlines()[0] == "BEGIN READ ONLY;" and seen["input"].strip().endswith("ROLLBACK;")
    assert "SET LOCAL statement_timeout TO 60000;" in seen["input"]
    # over ssh: the password is never shipped (libpq on the remote host reads ~/.pgpass)
    sqlexec._rs_psql(_src().conn, "select 1", "ssh:bastion", 60, None, TYPES)
    assert seen["argv"][-2] == "bastion" and "PGPASSWORD" not in seen["argv"][-1] and "--csv" in seen["argv"][-1]
    monkeypatch.setattr(sqlexec.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 2, stdout="", stderr="FATAL: password authentication failed for user x, password=s3cret"))
    with pytest.raises(CollectorError) as ex:
        sqlexec._rs_psql(_src().conn, "select 1", "local", 60, "s3cret", TYPES)
    assert "s3cret" not in str(ex.value)


def test_run_redshift_refuses_writes_and_multiple_statements_before_connecting(monkeypatch):
    monkeypatch.setattr(sqlexec, "_rs_psycopg", lambda *a, **k: pytest.fail("connected"))
    monkeypatch.setattr(sqlexec, "_rs_psql", lambda *a, **k: pytest.fail("connected"))
    for sql in ("drop table acme.orders", "unload ('select 1') to 's3://x/y'", "select 1; select 2", "vacuum"):
        with pytest.raises(CollectorError):
            sqlexec.run_redshift(_src(), sql, None, 10)


def test_csv_parser_edge_cases():
    assert sqlexec.parse_csv_rows("a,b\n", {}) == []
    assert sqlexec.parse_csv_rows("", {}) == []
    assert sqlexec.parse_csv_rows("a\nx\n", {"a": "str"}) == [{"a": "x"}]
    assert sqlexec.parse_csv_rows("a,b\n1,2.5\n", {"*": "infer"}) == [{"a": 1, "b": 2.5}]
    with pytest.raises(CollectorError, match="unknown column type"):
        sqlexec.parse_csv_rows("a\n1\n", {"a": "decimal"})
    assert sqlexec.parse_csv_rows("a\nnot-a-number\n", {"a": "int"}) == [{"a": "not-a-number"}]
    assert sqlexec.normalize_value(float("nan")) is None
    assert sqlexec.normalize_value(datetime(2026, 10, 19).date()) == "2026-10-19"
    assert sqlexec.normalize_value("2026-10-19T09:30:00.123456Z") == "2026-10-19T09:30:00Z"
    assert sqlexec.normalize_value(b"x") == "b'x'"


# ---------------------------------------------------------------- Data API end to end (botocore Stubber, documented shapes)

def _data_api_src():
    return parse_config("sources:\n  - {name: dwh, type: redshift, aws: {profile: readonly, region: us-east-1}, "
                        "data_api: {cluster_id: acme-dwh, database: dev, db_user: readonly_agent}}\n").sources[0]


def _api_stub(monkeypatch, seen=None):
    base = guarded_session(lambda s: sqlexec.check_read_only(s, "redshift"), seen)
    client, stub = stubbed(base, "redshift-data")
    monkeypatch.setattr(sqlexec, "data_api_client", lambda aws: client)
    monkeypatch.setattr(sqlexec, "_sleep", lambda s: None)
    return stub


def test_data_api_batch_poll_and_paged_results(monkeypatch):
    stub = _api_stub(monkeypatch)
    stub.add_response("batch_execute_statement", {"Id": "abc-123"}, {
        "Sqls": ["SET statement_timeout TO 60000", "select 1 as ok"], "ClusterIdentifier": "acme-dwh", "Database": "dev",
        "DbUser": "readonly_agent"})
    stub.add_response("describe_statement", {"Id": "abc-123", "Status": "STARTED"}, {"Id": "abc-123"})
    stub.add_response("describe_statement", {"Id": "abc-123", "Status": "FINISHED", "SubStatements": [
        {"Id": "abc-123:1", "Status": "FINISHED", "HasResultSet": False},
        {"Id": "abc-123:2", "Status": "FINISHED", "HasResultSet": True, "ResultRows": 2}]}, {"Id": "abc-123"})
    stub.add_response("get_statement_result", {"ColumnMetadata": API_COLS, "Records": API_RECORDS[:1], "NextToken": "t1",
                                               "TotalNumRows": 2}, {"Id": "abc-123:2"})
    stub.add_response("get_statement_result", {"ColumnMetadata": API_COLS, "Records": API_RECORDS[1:], "TotalNumRows": 2},
                      {"Id": "abc-123:2", "NextToken": "t1"})
    stub.activate()
    rows = sqlexec.run_redshift(_data_api_src(), "select 1 as ok", None, 60)
    stub.assert_no_pending_responses()
    assert rows == EXPECTED


def test_data_api_failed_statement_and_timeout_cancel(monkeypatch):
    stub = _api_stub(monkeypatch)
    stub.add_response("batch_execute_statement", {"Id": "x1"})
    stub.add_response("describe_statement", {"Id": "x1", "Status": "FAILED",
                                             "Error": "ERROR: permission denied for relation sys_query_history"})
    stub.activate()
    with pytest.raises(CollectorError, match="permission denied for relation sys_query_history"):
        sqlexec.run_redshift(_data_api_src(), "select 1", None, 60)
    # a statement that never finishes is cancelled after the budget
    stub2 = _api_stub(monkeypatch)
    clock = iter(range(0, 10_000, 4))
    monkeypatch.setattr(sqlexec, "_clock", lambda: next(clock))
    stub2.add_response("batch_execute_statement", {"Id": "x2"})
    for _ in range(3):  # clock: deadline 8, polls at 4, 8, 12 -> cancel on the third look
        stub2.add_response("describe_statement", {"Id": "x2", "Status": "STARTED"})
    stub2.add_response("cancel_statement", {"Status": True}, {"Id": "x2"})
    stub2.activate()
    with pytest.raises(CollectorError, match="timed out after 3s"):
        sqlexec.run_redshift(_data_api_src(), "select 1", None, 3)
    stub2.assert_no_pending_responses()


def test_data_api_client_error_is_masked(monkeypatch):
    stub = _api_stub(monkeypatch)
    stub.add_client_error("batch_execute_statement", "AccessDeniedException",
                          "User: arn:aws:sts::123456789012:assumed-role/ro/x is not authorized to perform redshift-data:BatchExecuteStatement")
    stub.activate()
    with pytest.raises(CollectorError) as ex:
        sqlexec.run_redshift(_data_api_src(), "select 1", None, 60)
    assert "AccessDeniedException" in str(ex.value) and "123456789012" not in str(ex.value)


@mock_aws
def test_data_api_cannot_be_used_to_write():
    """Guard behaviour, without Stubber (Stubber answers before the guard runs): moto never sees a refused call."""
    seen = SeenCalls()
    session = guarded_session(lambda s: sqlexec.check_read_only(s, "redshift"), seen)
    client = session.client("redshift-data")
    for sql in ("delete from acme.orders", "drop table acme.orders", "unload ('select 1') to 's3://x/y'"):
        with pytest.raises(core.ReadOnlyViolation):
            client.batch_execute_statement(Sqls=["SET statement_timeout TO 1000", sql], ClusterIdentifier="c",
                                           Database="d", DbUser="u")
    assert seen.ops == []  # nothing got past the guard
    with pytest.raises(sqlexec.CollectorError):
        sqlexec.run_redshift(_data_api_src(), "delete from acme.orders", None, 60)  # refused before any client exists
    # an allowed batch passes the guard: the (moto) service, not the guard, is what answers it
    with pytest.raises(Exception) as ex:
        client.batch_execute_statement(Sqls=["SET statement_timeout TO 1000", "select 1"], ClusterIdentifier="c",
                                       Database="d", DbUser="u")
    assert not isinstance(ex.value, core.ReadOnlyViolation)
    assert seen.ops == [("redshift-data", "BatchExecuteStatement")]


def test_data_api_secret_arn_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("ACME_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:123456789012:secret:acme-ro")
    stub = _api_stub(monkeypatch)
    src = parse_config("sources:\n  - {name: dwh, type: redshift, aws: {region: us-east-1}, "
                       "data_api: {workgroup: acme-wg, database: dev, secret_arn: 'env:ACME_SECRET_ARN'}}\n").sources[0]
    stub.add_response("batch_execute_statement", {"Id": "s1"}, {
        "Sqls": ["SET statement_timeout TO 60000", "select 1"], "WorkgroupName": "acme-wg", "Database": "dev",
        "SecretArn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:acme-ro"})
    stub.add_response("describe_statement", {"Id": "s1", "Status": "FINISHED"})
    stub.add_response("get_statement_result", {"ColumnMetadata": [{"label": "ok", "typeName": "int4"}],
                                               "Records": [[{"longValue": 1}]], "TotalNumRows": 1}, {"Id": "s1"})
    stub.activate()
    assert sqlexec.run_redshift(src, "select 1", None, 60) == [{"ok": 1}]


def test_collector_runs_end_to_end_over_the_data_api(monkeypatch):
    """Every section through the Data API path (fake rows by SQL); one section's permission error stays contained."""
    answers = fx.healthy()
    answers[rs.q_tables(50)[0].sql] = CollectorError("permission denied for view svv_table_info")

    def fake_run_redshift(source, sql, types, timeout_s):
        assert source.data_api and not source.conn
        if sql in answers:
            v = answers[sql]
            if isinstance(v, Exception):
                raise v
            return v
        raise CollectorError('relation "x" does not exist')

    monkeypatch.setattr(sqlexec, "run_redshift", fake_run_redshift)
    ctx = RunContext(timeout_seconds=600, aws_session=guarded_session())
    out = rs.RedshiftCollector().collect(_data_api_src(), ctx)
    assert any(e.startswith("tables: svv_table_info: permission denied") for e in out["errors"])
    assert out["data"]["workload_users"]


# ---------------------------------------------------------------- dea query on a redshift source

def test_query_limited_redshift_caps_rows_and_refuses_non_selects(monkeypatch):
    got = {}

    def fake(source, sql, types, timeout_s):
        got["sql"], got["types"] = sql, types
        return [{"a": 1, "__dea_total": 5}, {"a": 2, "__dea_total": 5}]

    monkeypatch.setattr(sqlexec, "run_redshift", fake)
    rows, total = sqlexec.query_limited_redshift(_src(), "select a from t;", 60, 2)
    assert rows == [{"a": 1}, {"a": 2}] and total == 5
    assert "limit 2" in got["sql"] and got["types"] == {"*": "infer"}
    for bad in ("delete from t", "select 1; select 2", "explain select 1", "unload ('select 1') to 's3://x/y'"):
        with pytest.raises(CollectorError):
            sqlexec.query_limited_redshift(_src(), bad, 60, 5)


def test_dea_query_cli_on_a_redshift_source(tmp_path, monkeypatch):
    from de_agent.cli import app
    from de_agent.initcmd import init_hub
    from typer.testing import CliRunner

    hub = tmp_path / "hub"
    init_hub(hub, "Demo", with_example=False)
    (hub / "de-agent.yaml").write_text(
        "version: 1\nsources:\n  - {name: dwh, type: redshift, conn: {host: h, port: 5439, dbname: dev, user: ro, password: pgpass}}\n")
    monkeypatch.setenv("DEA_HUB", str(hub))
    monkeypatch.setattr(sqlexec, "run_redshift", lambda s, sql, t, to: [{"n": 7, "__dea_total": 1}])
    r = CliRunner().invoke(app, ["query", "dwh", "select count(*) as n from stv_slices", "--format", "json"])
    assert r.exit_code == 0 and json.loads(r.stdout)["rows"] == [{"n": 7}], r.output
    r = CliRunner().invoke(app, ["query", "dwh", "unload ('select 1') to 's3://acme-lake/x/'"])
    assert r.exit_code == 2 and "refused" in r.output
    r = CliRunner().invoke(app, ["query", "dwh", "vacuum acme.orders"])
    assert r.exit_code == 2 and "refused" in r.output


def test_normalization_keeps_one_key_for_literal_variants():
    k1, t1 = rs.normalize_query("select  * from t where id = ?")
    k2, _ = rs.normalize_query("select * from t where id = ?\n")
    assert k1 == k2 and len(t1) <= 200 and re.fullmatch(r"[0-9a-f]{10}", k1)
    assert len(rs.normalize_query("x" * 900)[1]) == 200


def test_user_names_are_resolved_from_a_separate_query_not_a_join():
    for v in rs.v_users():
        assert "pg_user" not in v.sql
    a = fx.healthy()
    a[rs.v_users()[0].sql] = [{"user_id": 101, "query_count": 5, "exec_s": 9.0}, {"user_id": 102, "query_count": 1, "exec_s": 1.0}]
    a[rs.Q_USERNAMES.sql] = [{"user_id": 101, "user_name": "acme_etl"}]
    res = collect(a)
    assert [u["user"] for u in res["data"]["workload_users"]] == ["acme_etl", "user#102"]
    a.pop(rs.Q_USERNAMES.sql)  # name lookup unavailable: ids are shown, the section still succeeds
    res = collect(a)
    assert [u["user"] for u in res["data"]["workload_users"]] == ["user#101", "user#102"] and res["errors"] == []
