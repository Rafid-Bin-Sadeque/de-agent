"""M1c section 5 performance: a generated 1,000-DAG repo. Excluded by default; run with `pytest -m perf -s`."""
import json
import resource
import sys
import time

import pytest
from de_agent.config import load_config
from de_agent.scanners.run import run_scan

pytestmark = pytest.mark.perf

FULL_BUDGET_S = 60
INCREMENTAL_BUDGET_S = 3

PLAIN = '''from datetime import datetime

from airflow import DAG
from airflow.operators.empty import EmptyOperator
from common.util_{m} import fn_{m}_{k}

with DAG("plain_{i}", schedule="@daily", start_date=datetime(2026, 1, 1), tags=["plain", "g{g}"],
         default_args={{"owner": "team{g}"}}) as dag:
    EmptyOperator(task_id="a")
    fn_{m}_{k}()
'''
TASKFLOW = '''from datetime import datetime

from airflow.decorators import dag, task
from airflow.datasets import Dataset


@dag(schedule="@hourly", start_date=datetime(2026, 1, 1), tags=["taskflow"])
def tf_{i}():
    @task(outlets=[Dataset("warehouse://raw/t{i}")])
    def t():
        pass

    t()


tf_{i}()
'''
FACTORY = '''from datetime import datetime

from airflow import DAG
from airflow.operators.empty import EmptyOperator

NAMES = ["a", "b", "c", "d", "e"]


def make(name):
    with DAG(f"fac_{i}_{{name}}", schedule="@daily", start_date=datetime(2026, 1, 1), tags=["factory"]) as d:
        EmptyOperator(task_id="x")
    return d


for n in NAMES:
    globals()[f"fac_{i}_{{n}}"] = make(n)
'''
SQLDAG = '''from datetime import datetime

from airflow import DAG
from airflow.providers.postgres.operators.postgres import PostgresOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook

TABLE = "t{i}"


def work():
    hook = PostgresHook()
    hook.run(f"insert into mart.agg_{{TABLE}} select a, count(*) from raw.src_{i} s join raw.dim_{g} d on d.id = s.id group by a")
    hook.get_records("select * from mart.agg_{i} where x > 1")


with DAG("sql_{i}", schedule="@daily", start_date=datetime(2026, 1, 1), tags=["sql"]) as dag:
    PostgresOperator(task_id="load", sql="insert into staging.s_{i} select * from raw.src_{i}")
    PostgresOperator(task_id="proc", sql="CALL etl.refresh_{g}()")
'''


def build_repo(base):
    """~1,000 DAGs: 400 plain, 200 TaskFlow, 40 factory files x 5, 200 SQL-bearing; ~50 util symbols in 5 modules."""
    dags = base / "dags"
    util = base / "common"
    util.mkdir(parents=True)
    (util / "__init__.py").write_text("")
    for m in range(5):
        body = "".join(f'def fn_{m}_{k}():\n    """Util {m}.{k}."""\n    return {k}\n\n\n' for k in range(10))
        (util / f"util_{m}.py").write_text(body)
    n = 0
    for i in range(400):
        d = dags / f"team{i % 20}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"plain_{i}.py").write_text(PLAIN.format(i=i, g=i % 20, m=i % 5, k=i % 10))
        n += 1
    for i in range(200):
        d = dags / f"team{i % 20}"
        (d / f"tf_{i}.py").write_text(TASKFLOW.format(i=i))
        n += 1
    for i in range(40):
        d = dags / f"team{i % 20}"
        (d / f"fac_{i}.py").write_text(FACTORY.format(i=i))
        n += 5
    for i in range(200):
        d = dags / f"team{i % 20}"
        (d / f"sql_{i}.py").write_text(SQLDAG.format(i=i, g=i % 20))
        n += 1
    assert n == 1000
    return n


def test_thousand_dag_scan_times(tmp_path, capsys):
    repo = tmp_path / "repo"
    build_repo(repo)
    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "de-agent.yaml").write_text(
        "version: 1\nscans:\n  - {name: big, type: airflow, path: ../repo, dag_folders: [dags], util_packages: [common],"
        ' dataset_tables: ["warehouse://{schema}/{table}"]}\n')
    cfg = load_config(hub / "de-agent.yaml")
    t0 = time.perf_counter()
    out = run_scan(hub, cfg)
    full_s = time.perf_counter() - t0
    st = out.scans[0]["stats"]
    assert st["dags"] == 1000 and st["parse_errors"] == 0
    assert full_s < FULL_BUDGET_S
    # incremental: one changed file
    f = repo / "dags/team1/plain_1.py"
    f.write_text(f.read_text() + "\n# touched\n")
    t0 = time.perf_counter()
    out2 = run_scan(hub, cfg)
    inc_s = time.perf_counter() - t0
    assert out2.scans[0]["parsed"] == 1
    assert inc_s < INCREMENTAL_BUDGET_S
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024
    edges = json.loads((hub / "catalog/edges.json").read_text())
    msg = (f"PERF 1000 DAGs: full={full_s:.2f}s incremental(1 file)={inc_s:.2f}s peak_rss={rss_mb:.0f}MB "
           f"tables={len(edges['tables'])} symbols={len(json.loads((hub / 'catalog/symbols.json').read_text())['scans']['big'])}")
    with capsys.disabled():
        print("\n" + msg)
