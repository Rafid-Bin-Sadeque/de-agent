import json
import shutil
import subprocess
from pathlib import Path

import pytest
from de_agent.collectors import dbt as dbtc
from de_agent.collectors.base import RunContext
from de_agent.config import ConfigError, parse_config
from de_agent.diff import diff_results
from de_agent.render import render, signals_for
from de_agent.runner import result_status, run_source

FIX = Path(__file__).parent / "fixtures" / "dbt"


def _src(target, exec_="local"):
    return parse_config(f"sources:\n  - {{name: dbt, type: dbt, exec: {exec_}, options: {{target_dir: '{target}'}}}}\n").sources[0]


def collect(target):
    """Collect, then pin the clock to 1 h after the recorded run so signals do not depend on today's date."""
    res = run_source(_src(target))
    meta = res["data"].get("meta") or {}
    if meta.get("last_run_at"):
        from datetime import datetime, timedelta, timezone
        t = datetime.fromisoformat(meta["last_run_at"].replace("Z", "+00:00")).astimezone(timezone.utc) + timedelta(hours=1)
        meta["collected_at"] = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        res["collected_at"] = meta["collected_at"]
    return res


@pytest.fixture
def healthy(tmp_path):
    d = tmp_path / "target"
    shutil.copytree(FIX / "healthy", d)
    return d


def _edit(path, fn):
    doc = json.loads(path.read_text())
    fn(doc)
    path.write_text(json.dumps(doc))


def test_healthy_demo_project_sections_and_no_signals():
    res = collect(FIX / "healthy")
    assert res["errors"] == [] and result_status(res) == "ok"
    d = res["data"]
    assert d["project"]["name"] == "acme" and d["project"]["adapter"] == "postgres"
    assert d["project"]["manifest_schema"] == 12 and d["project"]["dbt_version"].startswith("1.8")
    mats = {(m["folder"], m["materialization"]): m["count"] for m in d["models"]}
    assert mats == {("staging", "view"): 4, ("marts", "incremental"): 1, ("marts", "table"): 2}
    assert d["last_run"]["command"] == "build" and "n_error" not in d["last_run"]
    assert d["failing_nodes"] == [] and d["tests"] == []
    assert len(d["slowest"]) == 10 and d["slowest"][0]["execution_s"] >= d["slowest"][-1]["execution_s"]
    fr = {f["unique_id"]: f for f in d["freshness"]}
    assert fr["source.acme.raw.orders"]["status"] == "pass"
    assert signals_for(res) == []
    json.dumps(res)


def test_failing_run_from_demo_fault_05():
    res = collect(FIX / "failing")
    sig = signals_for(res)
    assert any(s.startswith("dbt last run:") or "dbt test(s) failing" in s for s in sig)
    failing = [t for t in res["data"]["tests"] if t["status"] in ("fail", "error")]
    assert any("unique_stg_order_items_order_item_id" in t["unique_id"] for t in failing)
    t = next(t for t in failing if "unique_stg_order_items" in t["unique_id"])
    assert t["model"] == "model.acme.stg_order_items" and t["failures"] >= 1
    assert "model.acme.stg_order_items" in " ".join(sig)
    assert res["data"]["last_run"].get("n_skipped", 0) > 0  # downstream marts were skipped
    old = collect(FIX / "healthy")
    d = diff_results(old, res)
    assert d["signals_new"] and any("unique_stg_order_items" in r["key"] for r in d["lists"]["tests"]["added"])


def test_failing_model_run_results_fixture(healthy):
    def fail_model(doc):
        for r in doc["results"]:
            if r["unique_id"] == "model.acme.fct_orders":
                r["status"] = "error"
                r["message"] = "Database Error: " + "x" * 400
    _edit(healthy / "run_results.json", fail_model)
    res = collect(healthy)
    node = res["data"]["failing_nodes"][0]
    assert node["unique_id"] == "model.acme.fct_orders" and len(node["message"]) == 300
    assert any("1 error/fail result(s) (model.acme.fct_orders)" in s for s in signals_for(res))


def test_freshness_warn_and_error_signals(healthy):
    def stale(doc):
        doc["results"][0]["status"] = "error"
        doc["results"][0]["max_loaded_at_time_ago_in_s"] = 7.2 * 3600
    _edit(healthy / "sources.json", stale)
    assert "source freshness error: source.acme.raw.orders (7.2 h old)" in signals_for(collect(healthy))


def test_stale_last_run_signal(healthy):
    def old(doc):
        doc["metadata"]["generated_at"] = "2020-01-01T00:00:00Z"
    _edit(healthy / "run_results.json", old)
    sig = signals_for(run_source(_src(healthy)))  # real clock: a 2020 run is far older than 24 h
    assert any(s.startswith("dbt last run is") and s.endswith("old") for s in sig)


def test_missing_files_are_section_errors_not_crashes(tmp_path):
    d = tmp_path / "t"
    d.mkdir()
    res = collect(d)
    assert result_status(res) == "failed" and len(res["errors"]) == 3
    assert all("not found" in e for e in res["errors"])
    shutil.copy(FIX / "healthy" / "manifest.json", d / "manifest.json")
    res = collect(d)
    assert result_status(res) == "partial" and "project" in res["data"] and "last_run" not in res["data"]
    assert "run_results.json: not found" in res["errors"][0]
    md = render(res)
    assert "## Errors" in md


def test_corrupt_json_is_a_section_error(healthy):
    (healthy / "run_results.json").write_text("{not json")
    res = collect(healthy)
    assert res["errors"] == ["run_results.json: not valid JSON"] and "project" in res["data"]


@pytest.mark.parametrize("ver,warned", [(10, False), (11, False), (12, False), (9, True), (13, True)])
def test_manifest_schema_versions(healthy, ver, warned):
    def setv(doc):
        doc["metadata"]["dbt_schema_version"] = f"https://schemas.getdbt.com/dbt/manifest/v{ver}.json"
    _edit(healthy / "manifest.json", setv)
    res = collect(healthy)
    assert bool(res["errors"]) == warned
    if warned:
        assert f"unknown schema version {ver}" in res["errors"][0] and res["data"]["models"]  # best effort


def test_relative_target_dir_resolves_against_hub(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    (hub / "t").mkdir(parents=True)
    for f in ("manifest.json", "run_results.json", "sources.json"):
        shutil.copy(FIX / "healthy" / f, hub / "t" / f)
    (hub / "de-agent.yaml").write_text("version: 1\nsources: []\n")
    monkeypatch.setenv("DEA_HUB", str(hub))
    assert result_status(collect("t")) == "ok"


def test_ssh_reads_with_cat(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        name = argv[-1].strip("'").rsplit("/", 1)[1]
        return subprocess.CompletedProcess(argv, 0, stdout=(FIX / "healthy" / name).read_text(), stderr="")

    monkeypatch.setattr(dbtc.subprocess, "run", fake_run)
    res = run_source(_src("/srv/dbt/target", exec_="ssh:box"))
    assert result_status(res) == "ok" and len(calls) == 3
    assert calls[0][:4] == ["ssh", "-o", "BatchMode=yes", "-o"] and "box" in calls[0] and "cat" in calls[0]


def test_config_validation():
    with pytest.raises(ConfigError, match="target_dir"):
        parse_config("sources:\n  - {name: d, type: dbt, options: {}}\n")
    with pytest.raises(ConfigError, match="unknown dbt options"):
        parse_config("sources:\n  - {name: d, type: dbt, options: {target_dir: t, x: 1}}\n")
    with pytest.raises(ConfigError, match="take no `conn:`"):
        parse_config("sources:\n  - {name: d, type: dbt, conn: {dbname: a, user: b}, options: {target_dir: t}}\n")


def test_ping_and_list_keys(tmp_path):
    c = dbtc.DbtCollector()
    assert "dbt 1.8" in c.ping(_src(FIX / "healthy"), RunContext(10))
    assert c.list_keys["failing_nodes"] == ["unique_id"] and c.list_keys["tests"] == ["unique_id"]
    assert c.list_keys["freshness"] == ["unique_id"]


def test_diff_flags_slower_model(healthy, tmp_path):
    other = tmp_path / "slow"
    shutil.copytree(healthy, other)

    def slow(doc):
        for r in doc["results"]:
            if r["unique_id"] == "model.acme.fct_orders":
                r["execution_time"] = 400.0
    _edit(other / "run_results.json", slow)
    _edit(healthy / "run_results.json", lambda d: [r.update(execution_time=100.0) for r in d["results"]
                                                   if r["unique_id"] == "model.acme.fct_orders"])
    d = diff_results(collect(healthy), collect(other))
    ch = d["lists"]["slowest"]["changed"][0]
    assert ch["key"] == "model.acme.fct_orders" and "execution_s" in ch["changes"][0]["text"]
