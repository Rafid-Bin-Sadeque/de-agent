"""Recovered signals (M2.1): a failure the DAG/dbt project has since run clean stays visible but ranks below active ones."""
import json
import shutil
from pathlib import Path

from de_agent.diff import diff_results
from de_agent.render import airflow_signals

NOW = "2026-10-20T12:00:00Z"
FIX = Path(__file__).parent / "fixtures" / "dbt"


def _dag(dag_id):
    return {"dag_id": dag_id, "is_paused": False, "schedule": None, "next_dagrun": None}


def _data(runs, fails):
    return {"meta": {"collected_at": NOW}, "dags": [_dag("a"), _dag("b")], "runs": runs, "failures_24h": fails,
            "import_errors": [], "datasets": [], "pools": []}


def _run(dag, state, start, end):
    return {"dag_id": dag, "run_id": f"manual__{start}", "state": state, "start": start, "end": end, "duration_s": 60}


def _fail(dag, start):
    return {"dag_id": dag, "task_id": "load", "try": 1, "run_id": "x", "state": "failed", "start": start}


def test_success_after_failure_is_recovered_and_ranked_last():
    data = _data(
        [_run("a", "failed", "2026-10-20T05:00:00Z", "2026-10-20T05:05:00Z"),
         _run("a", "success", "2026-10-20T07:00:00Z", "2026-10-20T07:03:00Z"),
         _run("b", "failed", "2026-10-20T06:00:00Z", "2026-10-20T06:02:00Z")],
        [_fail("a", "2026-10-20T05:00:00Z"), _fail("b", "2026-10-20T06:00:00Z")])
    sig = airflow_signals(data)
    assert sig[0].startswith("dag b: 1 failed run(s) in 24h") and "recovered" not in sig[0]
    assert sig[-1] == "dag a: 1 failed run(s) in 24h (tasks: load) (recovered: last run success at 2026-10-20 07:03)"


def test_failure_after_success_is_still_active():
    data = _data([_run("a", "success", "2026-10-20T05:00:00Z", "2026-10-20T05:03:00Z"),
                  _run("a", "failed", "2026-10-20T07:00:00Z", "2026-10-20T07:03:00Z")],
                 [_fail("a", "2026-10-20T07:00:00Z")])
    sig = airflow_signals(data)
    assert len(sig) == 1 and "recovered" not in sig[0]


def test_running_latest_run_is_not_recovery():
    data = _data([_run("a", "failed", "2026-10-20T05:00:00Z", "2026-10-20T05:03:00Z"),
                  _run("a", "running", "2026-10-20T07:00:00Z", None)], [_fail("a", "2026-10-20T05:00:00Z")])
    assert "recovered" not in airflow_signals(data)[0]


def _res(data):
    return {"source": "af", "type": "airflow", "schema_version": 1, "collected_at": NOW, "duration_s": 1, "data": data,
            "errors": []}


def test_diff_reports_recovered_once():
    failing = _data([_run("a", "failed", "2026-10-20T05:00:00Z", "2026-10-20T05:05:00Z")],
                    [_fail("a", "2026-10-20T05:00:00Z")])
    fixed = _data([_run("a", "failed", "2026-10-20T05:00:00Z", "2026-10-20T05:05:00Z"),
                   _run("a", "success", "2026-10-20T07:00:00Z", "2026-10-20T07:03:00Z")],
                  [_fail("a", "2026-10-20T05:00:00Z")])
    d = diff_results(_res(failing), _res(fixed))
    assert len(d["signals_recovered"]) == 1 and d["signals_recovered"][0].startswith("dag a:")
    assert d["signals_new"] == [] and d["signals_cleared"] == []
    # recovered stays recovered: no repeat
    d2 = diff_results(_res(fixed), _res(fixed))
    assert d2["signals_recovered"] == [] and d2["signals_new"] == []


def test_diff_markdown_line(tmp_path):
    from de_agent.diff import render_markdown

    d = {"status": "changed", "old_at": NOW, "age": "1 h", "signals_new": [], "signals_cleared": [],
         "signals_recovered": ["dag a: x (recovered: last run success at 2026-10-20 07:03)"], "scalars": [], "lists": {}}
    assert "- RECOVERED signal: dag a" in render_markdown({"sources": {"af": d}})


# ---- dbt: a later successful run clears failing nodes

def _dbt_res(target):
    from de_agent.config import parse_config
    from de_agent.runner import run_source

    src = parse_config(f"sources:\n  - {{name: dbt, type: dbt, options: {{target_dir: '{target}'}}}}\n").sources[0]
    return run_source(src)


def test_dbt_later_clean_run_is_recovered(tmp_path):
    bad = tmp_path / "bad"
    shutil.copytree(FIX / "failing", bad)
    good = tmp_path / "good"
    shutil.copytree(FIX / "failing", good)
    rr = json.loads((good / "run_results.json").read_text())
    failed = []
    for r in rr["results"]:
        if r["status"] in ("error", "fail", "failed"):
            failed.append(r["unique_id"])
            r["status"] = "success" if not r["unique_id"].startswith("test.") else "pass"
    assert failed
    rr["metadata"]["generated_at"] = "2099-01-01T00:00:00Z"
    (good / "run_results.json").write_text(json.dumps(rr))
    old, new = _dbt_res(bad), _dbt_res(good)
    d = diff_results(old, new)
    assert d["signals_recovered"] and "recovered: later" in d["signals_recovered"][0]
    assert not any(c.startswith("dbt last run:") for c in d["signals_cleared"])


def test_dbt_not_rerun_is_not_recovered(tmp_path):
    bad = tmp_path / "bad"
    shutil.copytree(FIX / "failing", bad)
    other = tmp_path / "other"
    shutil.copytree(FIX / "failing", other)
    rr = json.loads((other / "run_results.json").read_text())
    rr["results"] = [r for r in rr["results"] if r["status"] not in ("error", "fail", "failed")]
    rr["metadata"]["generated_at"] = "2099-01-01T00:00:00Z"
    (other / "run_results.json").write_text(json.dumps(rr))
    d = diff_results(_dbt_res(bad), _dbt_res(other))
    assert d["signals_recovered"] == [] and d["signals_cleared"]
