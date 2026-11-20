"""Regression: `for x in range(<non-constant>)` recursed iter_values <-> iter_rows until RecursionError (0.2.5)."""
import ast
import json
import shutil
from pathlib import Path

import pytest
from de_agent.config import load_config
from de_agent.scanners import airflow as af
from de_agent.scanners import python as py_scan
from de_agent.scanners.pyexpr import Evaluator
from de_agent.scanners.run import run_scan

FIX = Path(__file__).parent / "fixtures" / "range_loops_repo"


def _call(src):
    return ast.parse(src, mode="eval").body


@pytest.mark.parametrize("src", [
    "range(len(xs))", "range(n, m)", "range(len(a), 0, -1)", "range(n)"])
def test_non_constant_range_is_not_enumerable(src):
    ev = Evaluator({})
    assert ev.iter_values(_call(src), {}) is None
    assert ev.iter_rows(_call(src), {}) is None


def test_constant_range_still_enumerates():
    ev = Evaluator({})
    assert ev.iter_values(_call("range(3)"), {}) == [0, 1, 2]
    assert ev.iter_rows(_call("range(3)"), {}) == [0, 1, 2]
    assert ev.iter_rows(_call("range(1, 6, 2)"), {}) == [1, 3, 5]


def test_nested_non_constant_loops_scan():
    src = ("def f(xs, n, m, cur):\n    for i in range(len(xs)):\n        for j in range(n, m):\n"
           "            for k in range(len(xs), 0, -1):\n                cur.execute(f'select {i}{j}{k} from t')\n")
    res = py_scan.scan_py_text(src, "a.py", "postgres")
    assert "error" not in res
    res = af.scan_python(src, "a.py", "postgres")
    assert "error" not in res and "util_error" not in res


def test_constant_range_unrolls_in_a_scan():
    src = "def f(cur):\n    for i in range(3):\n        cur.execute(f'select * from c_{i}')\n"
    res = py_scan.scan_py_text(src, "a.py", "postgres")
    tables = {t for r in res["funcs"]["f"]["sql"] for t in r.get("reads", [])}
    assert tables == {"c_0", "c_1", "c_2"}


@pytest.mark.parametrize("stype", ["python", "airflow"])
def test_corpus_of_40_loops_scans_with_zero_errors(tmp_path, stype):
    base = tmp_path / "ws"
    hub = base / "hub"
    hub.mkdir(parents=True)
    shutil.copytree(FIX, base / "repo")
    extra = ", dag_folders: [jobs]" if stype == "airflow" else ""
    (hub / "de-agent.yaml").write_text(f"version: 1\nscans:\n  - {{name: s, type: {stype}, path: ../repo{extra}}}\n")
    out = run_scan(hub, load_config(hub / "de-agent.yaml"))
    assert not out.failed, out.errors
    res = json.loads((hub / ".dea/scan_cache/s.result.json").read_text())
    assert res["stats"]["parse_errors"] == 0
    assert res["stats"].get("util_scan_errors", 0) == 0


def test_recursion_error_is_recorded_per_file_and_scan_continues(monkeypatch):
    def boom(self):
        raise RecursionError("maximum recursion depth exceeded")
    monkeypatch.setattr(af.FileScanner, "scan", boom)
    res = af.scan_python("def f():\n    pass\n", "dags/x.py", "postgres")
    assert res["rel"] == "dags/x.py"
    assert res["error"] == "recursion limit while evaluating loops"
    ok = py_scan.scan_py_text("x = 1\n", "y.py", "postgres")
    assert "error" not in ok
