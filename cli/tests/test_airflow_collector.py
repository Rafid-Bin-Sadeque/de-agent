import http.server
import json
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import pytest
from de_agent.collectors import _airflow_core as core
from de_agent.collectors import airflow as afc
from de_agent.collectors.base import CollectorError, RunContext
from de_agent.config import ConfigError, parse_config
from de_agent.diff import diff_results
from de_agent.render import airflow_signals, render, signals_for
from de_agent.runner import result_status, run_source

FIX = Path(__file__).parent / "fixtures"


def _key(path, params):
    return path + ("?" + urlencode(sorted((str(a), str(b)) for a, b in params)) if params else "")


def load(rel):
    doc = json.loads((FIX / rel).read_text())
    return doc["cassette"], datetime.strptime(doc["now"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def replay(cassette, calls=None):
    def get(path, params=None, accept="application/json"):
        if calls is not None:
            calls.append((path, list(params or []), accept))
        hit = cassette.get(_key(path, params))
        return (hit["status"], hit["body"]) if hit else (404, {"detail": "not recorded"})
    return get


def run(rel, **cfg):
    cassette, now = load(rel)
    res = core.gather({"base_url": "http://x", **cfg}, replay(cassette), now=now)
    return {"source": "af", "type": "airflow", "schema_version": 1, "collected_at": res["data"]["meta"]["collected_at"],
            "duration_s": 0.1, **res}


# ---------------------------------------------------------------- recorded Airflow 2.10 (from the demo stack)

def test_healthy_2_10_has_all_sections_and_no_signals():
    res = run("airflow_2_10/healthy.json")
    assert res["errors"] == [] and result_status(res) == "ok"
    d = res["data"]
    assert d["version"] == {"version": "2.10.5", "api": "v1"}
    assert {x["dag_id"] for x in d["dags"]} == {"dbt_build", "export_daily_orders", "extract_shop", "warehouse_maintenance"}
    assert d["failures_24h"] == [] and d["import_errors"] == []
    assert any(p["name"] == "default_pool" for p in d["pools"])
    uris = {x["uri"]: x for x in d["datasets"]}
    assert uris["warehouse://raw/orders"]["consuming_dags"] == ["dbt_build"]
    assert uris["warehouse://raw/orders"]["producing_tasks"] == ["extract_shop.extract_orders"]
    assert signals_for(res) == []
    json.dumps(res)


def test_failing_2_10_signals_and_log_tail():
    res = run("airflow_2_10/failing.json")
    sig = signals_for(res)
    assert any(s.startswith("dag extract_shop: 1 failed run(s) in 24h (tasks: extract_orders)") for s in sig)
    f = res["data"]["failures_24h"][0]
    assert f["task_id"] == "extract_orders" and f["try"] == 1
    assert "UndefinedColumn" in f["log_tail"] and len(f["log_tail"].splitlines()) <= core.LOG_TAIL_LINES
    md = render(res)
    assert "### log tail: extract_shop / extract_orders" in md


def test_diff_healthy_to_failing_reports_new_failure_only():
    old, new = run("airflow_2_10/healthy.json"), run("airflow_2_10/failing.json")
    d = diff_results(old, new)
    assert any("extract_shop" in s for s in d["signals_new"])
    assert d["lists"]["failures_24h"]["added"][0]["key"] == "extract_shop.extract_orders.1"
    assert diff_results(old, old)["lists"] == {} and diff_results(old, old)["signals_new"] == []


def test_fetch_logs_false_skips_log_calls():
    cassette, now = load("airflow_2_10/failing.json")
    calls = []
    res = core.gather({"fetch_logs": False}, replay(cassette, calls), now=now)
    assert "log_tail" not in res["data"]["failures_24h"][0]
    assert not any("/logs/" in c[0] for c in calls)


def test_only_get_requests_and_accept_text_for_logs():
    cassette, now = load("airflow_2_10/failing.json")
    calls = []
    core.gather({}, replay(cassette, calls), now=now)
    assert any(c[0].endswith("/logs/1") and c[2] == "text/plain" for c in calls)
    # the collector has no write path at all: get() is the only network function and it only issues GET
    import inspect
    src = inspect.getsource(core.make_urllib_get)
    assert 'method="GET"' in src and "POST" not in src and "PATCH" not in src and "DELETE" not in src


# ---------------------------------------------------------------- hand-built Airflow 3.x (/api/v2)

def test_airflow_3_normalisation_and_signals():
    res = run("airflow_3_0/basic.json")
    d = res["data"]
    assert d["version"] == {"version": "3.0.2", "api": "v2"}
    assert [x["dag_id"] for x in d["dags"]] == ["orders_etl", "stuck_hourly"]  # stale dag dropped
    assert d["dags"][0]["schedule"] == "0 2 * * *" and d["dags"][0]["next_dagrun"] == "2026-10-28T02:00:00Z"
    f = d["failures_24h"][0]
    assert f["try"] == 2 and "***" in f["log_tail"] and "hunter2secret" not in f["log_tail"]
    assert "duplicate key" in f["log_tail"]
    assert d["import_errors"][0]["stack"].count("\n") == core.STACK_LINES - 1
    assert {x["uri"] for x in d["datasets"]} == {"s3://lake/orders", "s3://lake/items"}
    sig = signals_for(res)
    assert any(s.startswith("dag orders_etl: 1 failed run(s) in 24h (tasks: load_orders)") for s in sig)
    assert any("1 DAG import error(s): /dags/broken.py" in s for s in sig)
    assert "dag stuck_hourly is late (schedule @hourly)" in sig
    assert "dataset s3://lake/orders has consumers but no event in 24h" in sig
    assert not any("s3://lake/items" in s for s in sig)
    assert "pool default_pool: 2 task(s) queued" in sig
    assert not any("orders_etl is late" in s for s in sig)


def test_detect_prefers_v2_then_falls_back_to_v1():
    assert core.detect_api(lambda p, q=None, a=None: (200, {"version": "3.1.0"}) if p == "/api/v2/version" else (404, {})) \
        == ("/api/v2", "3.1.0")
    assert core.detect_api(lambda p, q=None, a=None: (200, {"version": "2.9.0"}) if p == "/api/v1/version" else (404, {})) \
        == ("/api/v1", "2.9.0")
    with pytest.raises(core.ApiError, match="no Airflow API"):
        core.detect_api(lambda p, q=None, a=None: (404, {}))


# ---------------------------------------------------------------- pagination, caps, partial failure

def test_pagination_walks_all_pages_and_caps_at_max_dags():
    pages = []

    def get(path, params=None, accept="application/json"):
        if path.endswith("/version"):
            return (200, {"version": "2.10.5"}) if "v1" in path else (404, {})
        p = dict(params or [])
        if path == "/api/v1/dags":
            off = int(p["offset"])
            pages.append(off)
            rows = [{"dag_id": f"d{i:03d}", "is_paused": True} for i in range(off, min(off + 100, 250))]
            return 200, {"dags": rows, "total_entries": 250}
        return 200, {"dag_runs": [], "task_instances": [], "import_errors": [], "datasets": [], "pools": []}

    res = core.gather({}, get)
    assert len(res["data"]["dags"]) == 250 and pages == [0, 100, 200]
    pages.clear()
    res = core.gather({"max_dags": 120}, get)
    assert len(res["data"]["dags"]) == 120 and pages == [0, 100]


def test_one_failing_endpoint_gives_partial_result():
    cassette, now = load("airflow_2_10/healthy.json")
    del cassette["/api/v1/pools?limit=100"]
    res = core.gather({}, replay(cassette), now=now)
    assert res["errors"] == ["pools: GET /api/v1/pools -> HTTP 404"] and "dags" in res["data"]


# ---------------------------------------------------------------- auth, against a throwaway local HTTP server

class _Handler(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):  # noqa: N802
        _Handler.seen.append((self.command, self.path, self.headers.get("Authorization"), self.headers.get("Accept")))
        if self.path.startswith("/api/v2/version"):
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({"version": "2.10.5"} if self.path.startswith("/api/v1/version") else
                          {"dags": [], "total_entries": 0, "dag_runs": [], "task_instances": [], "import_errors": [],
                           "datasets": [], "pools": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    _Handler.seen = []
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _src(url, **opts):
    parts = ", ".join(f"{k}: {v}" for k, v in opts.items())
    return parse_config(f"sources:\n  - name: af\n    type: airflow\n    options: {{base_url: \"{url}\", {parts}}}\n").sources[0]


def test_basic_auth_header_and_get_only(server, monkeypatch):
    monkeypatch.setenv("DEA_TEST_AF_PW", "s3cret-pw")
    res = run_source(_src(server, auth="basic", user="agent_ro", password='"env:DEA_TEST_AF_PW"'))
    assert res["errors"] == [] and res["data"]["version"]["api"] == "v1"
    import base64
    want = "Basic " + base64.b64encode(b"agent_ro:s3cret-pw").decode()
    assert {m for m, *_ in _Handler.seen} == {"GET"}
    assert all(h == want for _, p, h, _ in _Handler.seen if not p.startswith("/api/v2/version") or h)
    assert any(h == want for _, _, h, _ in _Handler.seen)


def test_token_auth_header(server, monkeypatch):
    monkeypatch.setenv("DEA_TEST_AF_TOKEN", "tok-123456789")
    run_source(_src(server, auth="token", token='"env:DEA_TEST_AF_TOKEN"'))
    assert any(h == "Bearer tok-123456789" for _, _, h, _ in _Handler.seen)


def test_missing_secret_env_is_a_clean_error(server, monkeypatch):
    monkeypatch.delenv("DEA_TEST_AF_PW", raising=False)
    res = run_source(_src(server, auth="basic", user="u", password='"env:DEA_TEST_AF_PW"'))
    assert result_status(res) == "failed" and "DEA_TEST_AF_PW is not set" in res["errors"][0]


def test_unreachable_host_is_a_clean_error():
    res = run_source(_src("http://127.0.0.1:9", auth="none"))
    assert result_status(res) == "failed" and "cannot reach" in res["errors"][0]


# ---------------------------------------------------------------- log redaction

@pytest.mark.parametrize("raw,leak", [
    ("conn postgresql://etl:topsecret99@db:5432/x", "topsecret99"),
    ("password=hunter2 user=x", "hunter2"),
    ("PGPASSWORD=abc123xyz psql", "abc123xyz"),
    ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI"),
    ("key AKIAIOSFODNN7EXAMPLE seen", "AKIAIOSFODNN7EXAMPLE"),
    ("Authorization: Bearer eyJhbGciOi.abcdef.ghijkl", "eyJhbGciOi"),
    ("token: ghp_abcdefghijklmnop", "ghp_abcdefghijklmnop"),
])
def test_redact_text(raw, leak):
    out = core.redact_text(raw)
    assert leak not in out and "***" in out


def test_log_tail_is_last_40_lines_and_redacted():
    lines = "\n".join(f"line {i} password=pw{i}" for i in range(100))

    def get(path, params=None, accept=None):
        return 200, lines

    tail = core._log_tail(get, "/api/v1", {"dag_id": "d", "dag_run_id": "r", "task_id": "t", "try_number": 1})
    out = tail.splitlines()
    assert len(out) == 40 and out[-1].startswith("line 99") and "pw99" not in tail


# ---------------------------------------------------------------- signals

def _now():
    return "2026-10-27T12:00:00Z"


def _runs(dag, states, interval=3600, dur=60, start="2026-10-27T11:00:00Z"):
    t0 = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    out = []
    for i, (st, d) in enumerate(zip(states, dur if isinstance(dur, list) else [dur] * len(states))):
        s = t0.timestamp() - i * interval
        sdt = datetime.fromtimestamp(s, timezone.utc)
        e = datetime.fromtimestamp(s + d, timezone.utc)
        out.append({"dag_id": dag, "run_id": f"scheduled__{i}", "state": st, "logical_date": sdt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "start": sdt.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": e.strftime("%Y-%m-%dT%H:%M:%SZ"), "duration_s": d})
    return out


def _dag(dag_id, schedule="@hourly", nxt="2026-10-27T13:00:00Z", paused=False):
    return {"dag_id": dag_id, "is_paused": paused, "schedule": schedule, "next_dagrun": nxt}


def test_signal_duration_regression_needs_2x_and_5_minutes():
    runs = _runs("d", ["success"] * 5, dur=[1200, 300, 300, 300, 300])
    assert any("2x the median" in s for s in airflow_signals({"meta": {"collected_at": _now()}, "dags": [_dag("d")], "runs": runs}))
    small = _runs("d", ["success"] * 5, dur=[200, 20, 20, 20, 20])  # 10x but under 5 min
    assert not any("median" in s for s in airflow_signals({"meta": {"collected_at": _now()}, "dags": [_dag("d")], "runs": small}))
    flat = _runs("d", ["success"] * 5, dur=[700, 600, 600, 600, 600])  # over 5 min but under 2x
    assert not any("median" in s for s in airflow_signals({"meta": {"collected_at": _now()}, "dags": [_dag("d")], "runs": flat}))


def test_signal_late_dag_rules():
    base = {"meta": {"collected_at": _now()}}
    # next run more than one interval in the past
    d = {**base, "dags": [_dag("d", nxt="2026-10-27T09:00:00Z")], "runs": _runs("d", ["success"], start="2026-10-27T11:00:00Z")}
    assert "dag d is late (schedule @hourly)" in airflow_signals(d)
    # no success within 2 intervals
    d = {**base, "dags": [_dag("d")], "runs": _runs("d", ["success"], start="2026-10-27T08:00:00Z")}
    assert "dag d is late (schedule @hourly)" in airflow_signals(d)
    # healthy, paused, unscheduled: not late
    ok = {**base, "dags": [_dag("d")], "runs": _runs("d", ["success"], start="2026-10-27T11:30:00Z")}
    assert airflow_signals(ok) == []
    paused = {**base, "dags": [_dag("d", paused=True, nxt="2026-10-01T00:00:00Z")], "runs": []}
    assert airflow_signals(paused) == []
    ds = {**base, "dags": [_dag("d", schedule=None, nxt=None)], "runs": _runs("d", ["success"], start="2026-10-01T00:00:00Z")}
    assert airflow_signals(ds) == []


def test_schedule_interval_heuristics():
    f = core.schedule_interval_s
    assert f("@hourly", []) == 3600 and f("@daily", []) == 86400 and f("0 2 * * *", []) == 86400
    assert f("0 * * * *", []) == 3600 and f("*/15 * * * *", []) == 900 and f("0 6 * * 1", []) == 604800
    assert f(None, []) is None and f("@once", []) is None
    runs = [{"run_id": f"scheduled__{i}", "logical_date": f"2026-10-27T0{i}:00:00Z"} for i in range(5)]
    assert f("weird expr", runs) == 3600


def test_signal_failed_run_old_failure_not_counted():
    runs = _runs("d", ["failed"], start="2026-10-20T00:00:00Z")
    assert airflow_signals({"meta": {"collected_at": _now()}, "dags": [_dag("d", paused=True)], "runs": runs}) == []


# ---------------------------------------------------------------- config validation and ssh

def test_config_validation():
    ok = parse_config("sources:\n  - {name: a, type: airflow, options: {base_url: 'http://x:8080', auth: token, token: 'env:T'}}\n")
    assert ok.sources[0].conn is None
    with pytest.raises(ConfigError, match="secret literal"):
        parse_config("sources:\n  - {name: a, type: airflow, options: {base_url: 'http://x', auth: basic, user: u, password: hunter2}}\n")
    with pytest.raises(ConfigError, match="unknown airflow options"):
        parse_config("sources:\n  - {name: a, type: airflow, options: {base_url: 'http://x', bogus: 1}}\n")
    with pytest.raises(ConfigError, match="base_url"):
        parse_config("sources:\n  - {name: a, type: airflow, options: {base_url: 'ftp://x'}}\n")
    with pytest.raises(ConfigError, match="take no `conn:`"):
        parse_config("sources:\n  - {name: a, type: airflow, conn: {dbname: d, user: u}, options: {base_url: 'http://x', auth: none}}\n")
    with pytest.raises(ConfigError, match="env: or file:"):
        parse_config("sources:\n  - {name: a, type: airflow, exec: 'ssh:box', options: {base_url: 'http://localhost:8080', "
                     "user: u, password: pgpass}}\n")
    parse_config("sources:\n  - {name: a, type: airflow, exec: 'ssh:box', options: {base_url: 'http://localhost:8080', "
                 "user: u, password: 'env:AF_PW'}}\n")


def test_postgres_still_requires_conn():
    with pytest.raises(ConfigError, match="need a `conn:`"):
        parse_config("sources:\n  - {name: p, type: postgres}\n")


def test_ssh_sends_script_not_secret(monkeypatch):
    monkeypatch.setenv("AF_PW", "local-value-must-not-travel")
    captured = {}

    def fake_run(argv, input=None, **kw):
        captured["argv"], captured["input"] = argv, input
        out = json.dumps({"data": {"version": {"version": "2.10.5", "api": "v1"}}, "errors": []})
        return subprocess.CompletedProcess(argv, 0, stdout=out + "\n", stderr="")

    monkeypatch.setattr(afc.subprocess, "run", fake_run)
    src = parse_config("sources:\n  - {name: a, type: airflow, exec: 'ssh:box', options: {base_url: 'http://localhost:8080', "
                       "user: u, password: 'env:AF_PW'}}\n").sources[0]
    res = afc.AirflowCollector().collect(src, RunContext(timeout_seconds=30))
    assert res["data"]["version"]["api"] == "v1"
    assert captured["argv"][-3:] == ["box", "python3", "-"] and captured["argv"][:2] == ["ssh", "-o"]
    assert "local-value-must-not-travel" not in captured["input"] and "env:AF_PW" in captured["input"]
    assert "def remote_main" in captured["input"]


def test_remote_script_round_trip_resolves_secret_remotely(server, monkeypatch):
    """Execute the exact script that would be piped over ssh, with python3 -, against a local server."""
    cfg = {"base_url": server, "auth": "basic", "user": "agent_ro", "password": "env:DEA_REMOTE_PW"}
    script = afc._remote_script(cfg)
    p = subprocess.run([sys.executable, "-I", "-"], input=script, capture_output=True, text=True, timeout=60,
                       env={"DEA_REMOTE_PW": "remote-side-secret", "PATH": "/usr/bin:/bin"})
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out["errors"] == [] and out["data"]["version"]["api"] == "v1"
    import base64
    assert any(h == "Basic " + base64.b64encode(b"agent_ro:remote-side-secret").decode() for _, _, h, _ in _Handler.seen)
    # a missing remote secret is reported, not crashed
    p = subprocess.run([sys.executable, "-I", "-"], input=script, capture_output=True, text=True, timeout=60,
                       env={"PATH": "/usr/bin:/bin"})
    assert "DEA_REMOTE_PW is not set on the remote host" in json.loads(p.stdout.strip().splitlines()[-1])["errors"][0]


def test_collector_error_message_never_contains_secret(server, monkeypatch):
    monkeypatch.setenv("DEA_TEST_AF_PW", "sup3rsecret")
    res = run_source(_src("http://127.0.0.1:9", auth="basic", user="u", password='"env:DEA_TEST_AF_PW"'))
    assert "sup3rsecret" not in json.dumps(res)


def test_collector_protocol_shape():
    c = afc.AirflowCollector()
    assert c.type == "airflow" and c.list_keys["runs"] == ["dag_id", "run_id"]
    assert c.list_keys["failures_24h"] == ["dag_id", "task_id", "try"]
    with pytest.raises(CollectorError):
        afc._local_get(_src("http://x", auth="basic", user="u", password='"env:DEA_DEFINITELY_UNSET"'), RunContext(10))
