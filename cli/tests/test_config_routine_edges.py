"""v0.3.1 section 6: DAG -> routine `calls` edges from module-level config constants (static, never imported)."""
import json

from de_agent.config import load_config
from de_agent.scanners.edges import callers
from de_agent.scanners.run import run_scan
from de_agent.snapshot import take_snapshot

CFG = """
sources:
  - name: warehouse
    type: postgres
    conn: {dbname: shop, user: ro}
scans:
  - name: pipelines
    type: airflow
    path: pipelines
    dialect: postgres
"""

MART_CONFIG = '''
MART_PROCEDURES = [
    {"target_table": "daily_sales", "proc": "mart_sales.load_daily_sales"},
    {"target_table": "weekly_sales", "proc": "mart_sales.load_weekly_sales", "after": ["daily_sales"]},
]
'''
DAG_FROM_IMPORT = '''
from airflow import DAG
from airflow.operators.python import PythonOperator
from marts.sales.mart_config import MART_PROCEDURES

def run_proc(proc): ...

with DAG("sales_marts", schedule="@daily") as dag:
    for m in MART_PROCEDURES:
        PythonOperator(task_id=m["target_table"], python_callable=run_proc, op_kwargs={"proc": m["proc"]})
'''
DAG_MODULE_ATTR = '''
from airflow import DAG
from airflow.operators.python import PythonOperator
import marts.sales.mart_config

with DAG("sales_marts_b", schedule="@daily") as dag:
    for m in marts.sales.mart_config.MART_PROCEDURES:
        PythonOperator(task_id=m["target_table"], python_callable=print)
'''
OTHER_CONFIG = '''
DICT_OF_DICTS = {
    "a": {"procedure": "etl.load_a"},
    "b": {"Stored_Procedure": "CALL etl.load_b(1, 2)"},
    "c": {"sp": "ETL.Load_C()"},
    "d": {"routine": "etl.load_d", "function": "etl.fn_d"},
    "e": {"proc": f"etl.load_{1}"},
    "f": {"proc": "not_qualified"},
}
UNUSED_PROCS = [{"proc": "etl.never_called"}]
'''
DAG_OTHER = '''
from airflow import DAG
from other_config import DICT_OF_DICTS, UNUSED_PROCS
from ext_cfg import EXTERNAL
with DAG("etl_other", schedule="@daily") as dag:
    for k, v in DICT_OF_DICTS.items():
        print(k, v)
'''
OUTSIDE = 'EXTERNAL = [{"proc": "outside.should_be_ignored"}]\n'


def _row(schema, name):
    d = f"CREATE OR REPLACE PROCEDURE {schema}.{name}()\n LANGUAGE plpgsql\nAS $procedure$\nbegin\n  null;\nend;\n$procedure$"
    return {"schema": schema, "name": name, "kind": "procedure", "args": "()", "language": "plpgsql", "returns": "",
            "owner": "o", "definition": d}


ROWS = [_row("mart_sales", n) for n in ("load_daily_sales", "load_weekly_sales")] + [
    _row("etl", n) for n in ("load_a", "load_b", "load_c", "load_d", "never_called")] + [
    {**_row("etl", "fn_d"), "kind": "function", "args": "()"}]


def make_hub(tmp_path):
    hub = tmp_path / "hub"
    dags = hub / "pipelines" / "dags"
    (dags / "marts" / "sales").mkdir(parents=True)
    (dags / "marts" / "sales" / "mart_config.py").write_text(MART_CONFIG)
    (dags / "sales_marts_dag.py").write_text(DAG_FROM_IMPORT)
    (dags / "sales_marts_b_dag.py").write_text(DAG_MODULE_ATTR)
    (dags / "other_config.py").write_text(OTHER_CONFIG)
    (dags / "etl_other_dag.py").write_text(DAG_OTHER)
    (hub / "ext").mkdir()
    (hub / "ext" / "ext_cfg.py").write_text(OUTSIDE)  # not under a dag folder or util package: not scanned
    (hub / "de-agent.yaml").write_text(CFG)
    (hub / "platform").mkdir()
    return hub


def scan_with_routines(tmp_path):
    hub = make_hub(tmp_path)
    cfg = load_config(hub / "de-agent.yaml")

    def fn(conn, sql, exec_target, timeout_s):
        return ROWS if "pg_get_functiondef" in sql else []

    take_snapshot(hub, cfg, query_fn=fn, do_render=False)
    out = run_scan(hub, cfg)
    edges = json.loads((hub / "catalog" / "edges.json").read_text())
    return hub, cfg, out, edges, fn


def dag_calls(edges, dag):
    return sorted((c["to"].split(":")[-1], c["via"]) for c in edges["calls"] if c["from"] == f"dag:{dag}")


def test_both_dags_see_the_procs_of_the_config_constant(tmp_path):
    _, _, _, edges, _ = scan_with_routines(tmp_path)
    via = "config:marts.sales.mart_config.MART_PROCEDURES"
    want = [("mart_sales.load_daily_sales", via), ("mart_sales.load_weekly_sales", via)]
    for dag in ("sales_marts", "sales_marts_b"):
        got = [(n.split("(")[0], v) for n, v in dag_calls(edges, dag)]
        assert got == want, (dag, got)


def test_dict_of_dicts_call_form_case_and_dynamic(tmp_path):
    _, _, out, edges, _ = scan_with_routines(tmp_path)
    got = {n.split("(")[0] for n, _ in dag_calls(edges, "etl_other")}
    assert got == {"etl.load_a", "etl.load_b", "etl.load_c", "etl.load_d", "etl.fn_d"}  # f-string and bare name skipped
    assert all(v == "config:other_config.DICT_OF_DICTS" for _, v in dag_calls(edges, "etl_other"))
    airflow = next(s for s in out.scans if s.get("type") == "airflow")
    assert airflow["stats"]["dynamic_config_entries"] == 1


def test_unused_constant_and_outside_module_make_no_edge(tmp_path):
    _, _, _, edges, _ = scan_with_routines(tmp_path)
    targets = {c["to"] for c in edges["calls"]}
    assert not any("never_called" in t for t in targets)
    assert not any("outside" in t for t in targets)
    assert not any("outside.should_be_ignored" == u.get("name") for u in edges.get("unresolved", []))


def test_unresolved_config_proc_is_reported_not_dropped(tmp_path):
    hub = make_hub(tmp_path)
    cfg = load_config(hub / "de-agent.yaml")
    run_scan(hub, cfg)  # no snapshot: no live routines
    edges = json.loads((hub / "catalog" / "edges.json").read_text())
    names = {u["name"] for u in edges.get("unresolved", []) if u.get("kind") == "proc"}
    assert {"mart_sales.load_daily_sales", "etl.load_a"} <= names
    assert "etl.never_called" not in names


def test_routine_page_lists_the_dag_as_caller(tmp_path):
    hub, cfg, _, edges, fn = scan_with_routines(tmp_path)
    rid = next(t for t in {c["to"] for c in edges["calls"]} if "load_daily_sales" in t)
    assert callers(edges, rid) == ["dag:sales_marts", "dag:sales_marts_b"]
    take_snapshot(hub, cfg, query_fn=fn, do_render=True)
    page = next((hub / "platform" / "warehouse" / "routines").glob("*load_daily_sales*.md")).read_text()
    assert "## Called by" in page and "`dag:sales_marts`" in page and "`dag:sales_marts_b`" in page
