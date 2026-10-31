"""Every built-in pack: fixtures -> collector -> rendered page matches a committed golden file; signals fire; diff works.

Fixtures are hand-built from documented shapes with fictional values (see packs_fixtures.py); only postgres-lite and mysql are
`validated: live` (test_packs_live.py runs them against real PG16 / mysql:8.4 containers)."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from de_agent.collectors.base import RunContext
from de_agent.config import parse_config
from de_agent.diff import diff_results
from de_agent.packs import check_text, list_packs, load_pack
from de_agent.packs.loader import builtin_names
from de_agent.render import render, signals_for
from de_agent.runner import result_status
from packs_fixtures import FIXTURES, NOW

GOLDEN = Path(__file__).parent / "golden"
NOW_DT = datetime.strptime(NOW, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
KIND_TYPE = {"sql": "sql", "cli": "cli", "http": "http"}


def source_for(name, options):
    pack = load_pack(name)
    lines = ["sources:", f"  - name: {name}", f"    type: {pack.kind}", f"    pack: {name}", "    timeout_seconds: 60"]
    if pack.kind == "sql":
        lines.append("    conn: {url: 'x://u@h/d'}")
    elif pack.kind == "http":
        lines += ["    base_url: http://example.invalid", "    auth: none"]
    # url scheme must match the dialect
    text = "\n".join(lines)
    if pack.kind == "sql":
        scheme = {"postgres": "postgresql+psycopg", "mysql": "mysql+pymysql", "snowflake": "snowflake", "bigquery": "bigquery",
                  "tsql": "mssql+pyodbc", "databricks": "databricks"}[pack.dialect]
        text = text.replace("x://u@h/d", f"{scheme}://u@h/d")
    cfg = parse_config(text + "\n    options: " + json.dumps(options) + "\n")
    return cfg.sources[0]


def fixture_fn(name):
    responses = FIXTURES[name]["responses"]
    kind = load_pack(name).kind

    def fn(section, item, payload):
        key = f"{section.name}@{item}" if item is not None else section.name
        out = responses[key]
        return json.dumps(out) if kind == "cli" else out

    return fn


def collect(name):
    src = source_for(name, FIXTURES[name]["options"])
    from de_agent.collectors import get_collector

    ctx = RunContext(timeout_seconds=60, pack_fn=fixture_fn(name), now=NOW_DT)
    out = get_collector(src.type).collect(src, ctx)
    from de_agent.collectors.base import make_result

    return make_result(name, src.type, 1, NOW, 0.1, out["data"], out["errors"])


def test_there_are_eleven_builtin_packs_with_the_documented_kinds():
    kinds = {n: load_pack(n).kind for n in builtin_names()}
    assert kinds == {
        "postgres-lite": "sql", "mysql": "sql", "snowflake": "sql", "bigquery": "sql", "databricks": "cli",
        "databricks-system": "sql", "mssql": "sql", "gcp": "cli", "kubernetes": "cli", "dagster": "http", "fivetran": "http"}
    assert {n for n in builtin_names()} == set(FIXTURES)


def test_validated_live_only_where_a_live_run_exists():
    live = {n for n in builtin_names() if load_pack(n).validated == "live"}
    assert live == {"postgres-lite", "mysql"}


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_builtin_pack_passes_pack_check(name):
    ref = next(r for r in list_packs() if r.name == name)
    pack, errs = check_text(ref.text)
    assert errs == [] and pack.pack == name


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_builtin_pack_states_its_validation_level(name):
    ref = next(r for r in list_packs() if r.name == name)
    pack = ref.parse()
    if pack.validated == "fixtures":
        assert "validated: fixtures" in ref.text and "fictional" in ref.text
    else:
        assert "container" in ref.text


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_golden_render(name):
    res = collect(name)
    assert res["errors"] == [], res["errors"]
    assert result_status(res) == "ok"
    page = render(res)
    golden = GOLDEN / f"pack_{name}.md"
    if os.environ.get("UPDATE_GOLDEN"):
        golden.write_text(page, encoding="utf-8")
    assert page == golden.read_text(encoding="utf-8"), f"{golden.name} drifted (UPDATE_GOLDEN=1 to refresh after review)"
    json.dumps(res)  # JSON-clean


EXPECTED_SIGNALS = {
    "postgres-lite": ["2 session(s) running for more than 5 min", "[error] pid 4101 has been running for 5400 s", "1 blocked session(s)",
                      "table shop.orders: 40% dead tuples (120000)"],
    "mysql": ["1 statement(s) running for more than 5 min", "1 InnoDB lock wait(s)",
              "[error] replication from db-primary.example.internal is not running (IO Yes, SQL No)"],
    "snowflake": ["warehouse LOAD_WH auto-suspend is 0 s", "2 failed queries in 24h", "[error] query 01b2-0001: quota error",
                  "warehouse ANALYTICS_WH: queries wait 42.5 s on average for compute", "1 task failure(s) in 24h", "1 pipe load failure(s) in 24h"],
    "bigquery": ["2 failed job(s) in 24h", "[error] job bquxjob_1a2b: quotaExceeded"],
    "databricks-system": ["1 failed job run segment(s) in 24h", "1 failed SQL statement(s) in 24h"],
    "mssql": ["1 blocked request(s)", "[error] session 61 blocked by 55 for 420 s", "1 request(s) running for more than 5 min", "1 SQL Agent job failure(s) in 24h"],
    "databricks": ["1 failed job run(s) in 24h", "run 90003 of job 701 has been active for more than 6 h", "[error] cluster etl-shared is TERMINATED (INSTANCE_UNREACHABLE)"],
    "gcp": ["[error] composer environment projects/example-shop-prod/locations/europe-west1/environments/shop-airflow-test is ERROR",
            "1 failed Dataflow job(s) in 24h"],
    "kubernetes": [],  # checked individually below
    "dagster": ["1 failed run(s) in 24h", "[info] schedule legacy_export_schedule is stopped"],
    "fivetran": ["[error] connector new_src (mysql): setup incomplete", "[error] connector ads_spend (google_ads): last sync failed at 2026-09-30T10:00:00Z",
                 "[info] connector crm (salesforce) is paused"],
}


@pytest.mark.parametrize("name", sorted(EXPECTED_SIGNALS))
def test_expected_signals_fire(name):
    sigs = signals_for(collect(name))
    for want in EXPECTED_SIGNALS[name]:
        assert want in sigs, (want, sigs)


def test_kubernetes_signals_and_namespace_column():
    res = collect("kubernetes")
    sigs = signals_for(res)
    assert "[error] pod data/spark-driver-1 is Failed (Evicted)" in sigs
    assert "pod shop/worker-5c4b-xyz12 is Pending" in sigs
    assert "[error] pod shop/worker-5c4b-xyz12 is in CrashLoopBackOff (14 restarts)" in sigs
    assert "[error] job shop/reindex-20261001 has 3 failed pod(s)" in sigs
    assert "[error] cronjob shop/weekly-report has been scheduled but never succeeded" in sigs
    assert "cronjob shop/nightly-export: last run after 2026-09-30T02:00:30Z did not succeed" in sigs  # scheduled since the last success
    assert {r["namespace"] for r in res["data"]["pods_not_running"]} == {"shop", "data"}


def test_http_stale_connector_signal_uses_a_param():
    sigs = signals_for(collect("fivetran"))
    assert "connector crm (salesforce): no successful sync for more than 24 h" not in sigs  # paused: handled separately
    assert not any("new_src" in s and "no successful sync" in s for s in sigs)


def test_diff_between_two_pack_results_reports_row_changes():
    old = collect("snowflake")
    new = json.loads(json.dumps(old))
    new["data"]["failed_queries_24h"].append(
        {"query_id": "01b2-0099", "user_name": "X", "warehouse_name": "LOAD_WH", "error_code": "1", "error_message": "boom", "start_time": "2026-10-01T11:00:00Z"})
    new["data"]["failed_queries_24h"] = [r for r in new["data"]["failed_queries_24h"] if r["query_id"] != "01b2-0002"]
    d = diff_results(old, new)
    added = [r["key"] for r in d["lists"]["failed_queries_24h"]["added"]]
    removed = [r["key"] for r in d["lists"]["failed_queries_24h"]["removed"]]
    assert added == ["01b2-0099"] and removed == ["01b2-0002"]


def test_a_failing_section_lands_in_errors_and_the_rest_stays():
    from de_agent.collectors import get_collector
    from de_agent.collectors.base import CollectorError

    src = source_for("snowflake", FIXTURES["snowflake"]["options"])
    real = fixture_fn("snowflake")

    def fn(section, item, payload):
        if section.name == "credits_24h":
            raise CollectorError("warehouse suspended")
        return real(section, item, payload)

    out = get_collector("sql").collect(src, RunContext(timeout_seconds=60, pack_fn=fn, now=NOW_DT))
    assert out["errors"] == ["credits_24h: warehouse suspended"]
    assert "failed_queries_24h" in out["data"] and "credits_24h" not in out["data"]


def test_row_cap_and_truncation_are_recorded():
    from de_agent.collectors import get_collector

    src = source_for("postgres-lite", {})
    rows = [{"schema": "s", "table": f"t{i}", "est_bytes": 1000 - i, "est_rows": 1} for i in range(300)]
    real = fixture_fn("postgres-lite")

    def fn(section, item, payload):
        return rows if section.name == "table_sizes" else real(section, item, payload)

    out = get_collector("sql").collect(src, RunContext(timeout_seconds=60, pack_fn=fn, now=NOW_DT))
    assert len(out["data"]["table_sizes"]) == 200 and out["data"]["meta"]["truncated"] == {"table_sizes": 300}
