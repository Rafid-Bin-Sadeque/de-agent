"""v0.2 section 2: util attribution, python/notebook/pyspark and terraform scanners, edges.ignore_tables."""
import json
import os
import shutil
import time
from pathlib import Path

import pytest
from de_agent import cli as cli_mod
from de_agent.cli import app
from de_agent.config import ConfigError, load_config, parse_config
from de_agent.scanners import pycode, terraform
from de_agent.scanners import python as py_scan
from de_agent.scanners.edges import build_edges, downstream
from de_agent.scanners.run import run_scan
from typer.testing import CliRunner

HERE = Path(__file__).parent
FIX = HERE / "fixtures"

UTIL_SCANS = (
    "scans:\n"
    "  - {name: pipelines, type: airflow, path: ../util_repo, dag_folders: [dags], util_packages: [utils]}\n"
    "  - {name: warehouse, type: sql, path: ../util_repo/sql, project: etl}\n")
PY_SCANS = "scans:\n  - {name: jobs, type: python, path: ../python_repo}\n"
TF_SCANS = "scans:\n  - {name: infra, type: terraform, path: ../tf_repo}\n"


def make_hub(tmp_path, scans, repos, extra=""):
    base = tmp_path / "ws"
    hub = base / "hub"
    hub.mkdir(parents=True)
    for r in repos:
        shutil.copytree(FIX / r, base / r)
    (hub / "de-agent.yaml").write_text("version: 1\n" + scans + extra)
    return hub


def scan(hub, **kw):
    return run_scan(hub, load_config(hub / "de-agent.yaml"), **kw)


def edges_of(hub):
    return json.loads((hub / "catalog" / "edges.json").read_text())


def golden(name, got):
    g = HERE / "golden" / name
    if os.environ.get("DEA_UPDATE_GOLDEN"):
        g.write_text(got)
    assert got == g.read_text()


def edge_set(e, table, side):
    return {(x["id"], x["via"], x.get("proc", "")) for x in e["tables"][table][side]}


@pytest.fixture
def util_hub(tmp_path):
    hub = make_hub(tmp_path, UTIL_SCANS, ["util_repo"])
    out = scan(hub)
    assert not out.failed, out.errors
    return hub


@pytest.fixture
def cli(util_hub, monkeypatch):
    monkeypatch.setenv("DEA_HUB", str(util_hub))
    return CliRunner()


# ---------------- config ----------------

def test_config_accepts_new_scan_types_and_python_default_globs():
    cfg = parse_config("scans:\n  - {name: a, type: python, path: x}\n  - {name: b, type: terraform, path: y}\n"
                       "  - {name: c, type: python, path: z, globs: ['jobs/*.py']}\n  - {name: d, type: sql, path: w}\n")
    assert cfg.scans[0].globs == ["**/*.py", "**/*.ipynb"]
    assert cfg.scans[2].globs == ["jobs/*.py"] and cfg.scans[3].globs == ["**/*.sql"]


def test_config_edges_ignore_tables_normalised():
    cfg = parse_config("edges:\n  ignore_tables: [ETL.Load_State, ' etl.audit ']\n")
    assert cfg.edges.ignore_tables == ["etl.load_state", "etl.audit"]
    with pytest.raises(ConfigError):
        parse_config("edges:\n  ignore_tables: ['']\n")
    with pytest.raises(ConfigError):
        parse_config("edges:\n  nope: 1\n")


# ---------------- util attribution (airflow) ----------------

def test_dag_calling_util_run_proc_gets_the_edge_via_util(util_hub):
    """The dogfood gap: `db.run_proc("etl.sp_refresh_orders")` inside a task callable lands on the DAG."""
    e = edges_of(util_hub)
    assert ("dag:refresh_orders_dag", "util:utils.db.run_proc", "etl.sp_refresh_orders") in edge_set(e, "raw.orders", "writers")
    assert ("dag:refresh_orders_dag", "util:utils.db.load_orders", "") in edge_set(e, "raw.orders_feed", "readers")
    assert ("dag:refresh_orders_dag", "util:utils.db.load_orders", "") in edge_set(e, "staging.orders_stage", "writers")
    # literal arguments flow into a util whose SQL is an f-string over its parameters
    assert ("dag:refresh_orders_dag", "util:utils.db.copy_table", "") in edge_set(e, "staging.orders_copy", "writers")
    hit = next(x for x in e["tables"]["raw.orders"]["writers"] if x["id"] == "dag:refresh_orders_dag")
    assert hit["file"].startswith("dags/util_proc.py:") and hit["hops"] == 1


def test_util_edge_resolves_proc_and_reaches_downstream(util_hub):
    e = edges_of(util_hub)
    rows = {r["reader"]: r["depth"] for r in downstream(e, "raw.orders_feed", 3, ignore=[])}
    assert rows["dag:refresh_orders_dag"] == 1                 # load_orders (a util) stages the feed
    assert rows["procedure:etl.sp_refresh_orders"] == 2        # ... which the proc then reads
    assert not any(u["kind"] == "proc" for u in e["unresolved"])


def test_two_hops_attributed_three_are_not(util_hub):
    e = edges_of(util_hub)
    two = edge_set(e, "raw.orders", "writers")
    assert ("dag:util_hops", "util:utils.etl.refresh>utils.db.run_proc", "etl.sp_refresh_orders") in two
    # refresh_all -> refresh -> run_proc is three hops from the DAG: not attributed, and nothing invents it
    assert not any(v == "util:utils.etl.refresh_all>utils.etl.refresh>utils.db.run_proc"
                   for t in e["tables"].values() for x in t["writers"] + t["readers"] for v in [x["via"]])
    hop2 = next(x for x in e["tables"]["raw.orders"]["writers"] if x["via"].count(">") == 1)
    assert hop2["hops"] == 2


def test_util_direct_sql_and_nested_call_inside_util(util_hub):
    e = edges_of(util_hub)
    assert ("dag:util_hops", "util:utils.etl.bookkeeping", "") in edge_set(e, "etl.load_state", "writers")
    # a proc CALL inside a util resolves like a CALL in the DAG
    assert ("dag:util_hops", "util:utils.etl.bookkeeping>utils.db.run_proc", "etl.sp_audit") in edge_set(e, "etl.run_log", "writers")


def test_config_driven_loops_are_unrolled(util_hub):
    e = edges_of(util_hub)
    w = edge_set(e, "analytics.daily_orders", "writers") | edge_set(e, "analytics.daily_customers", "writers")
    assert {("dag:nightly_procs", "util:utils.db.run_proc", "etl.sp_nightly_a"),
            ("dag:nightly_procs", "util:utils.db.run_proc", "etl.sp_nightly_b")} <= w   # for p in LIST
    r = edge_set(e, "raw.customers", "writers")
    assert ("dag:nightly_procs", "util:utils.db.run_proc", "etl.sp_refresh_customers") in r  # for k, v in DICT.items()
    assert all(x.get("unrolled") for x in e["tables"]["raw.customers"]["writers"] if x["id"] == "dag:nightly_procs")


def test_util_attribution_is_cached_and_deterministic(util_hub):
    first = {p.relative_to(util_hub).as_posix(): p.read_bytes() for p in (util_hub / "catalog").rglob("*") if p.is_file()}
    time.sleep(1.1)
    out = scan(util_hub)
    assert next(s for s in out.scans if s["name"] == "pipelines")["parsed"] == 0
    scan(util_hub, full=True)
    assert {p.relative_to(util_hub).as_posix(): p.read_bytes() for p in (util_hub / "catalog").rglob("*") if p.is_file()} == first


def test_util_edit_reparses_only_that_file_and_changes_the_edge(util_hub):
    f = util_hub.parent / "util_repo/utils/db.py"
    f.write_text(f.read_text().replace("raw.orders_feed", "raw.orders_feed_v2"))
    out = scan(util_hub)
    assert next(s for s in out.scans if s["name"] == "pipelines")["parsed"] == 1
    e = edges_of(util_hub)
    assert "raw.orders_feed_v2" in e["tables"] and "raw.orders_feed" not in e["tables"]


def test_unbound_parameter_is_counted_not_guessed(tmp_path):
    hub = make_hub(tmp_path, UTIL_SCANS, ["util_repo"])
    dag = hub.parent / "util_repo/dags/dynamic_arg.py"
    dag.write_text("from datetime import datetime\nfrom airflow import DAG\nfrom airflow.operators.python import PythonOperator\n"
                   "from utils import db\nimport os\n\nwith DAG('dyn', start_date=datetime(2026, 1, 1)) as d:\n"
                   "    PythonOperator(task_id='x', python_callable=lambda: db.run_proc(os.environ['PROC']))\n")
    out = scan(hub)
    assert out.edges["stats"]["pipelines.unresolved_util_args"] == 1
    assert not any(x["id"] == "dag:dyn" for t in out.edges["tables"].values() for x in t["writers"] + t["readers"])


def test_util_attribution_in_catalog_page_and_cli(cli):
    r = cli.invoke(app, ["edges", "raw.orders"])
    assert r.exit_code == 0 and "via util:utils.db.run_proc, proc etl.sp_refresh_orders" in r.output
    page = (Path(os.environ["DEA_HUB"]) / "catalog/airflow/pipelines.md").read_text()
    assert "via util: utils.db.copy_table, utils.db.load_orders, utils.db.run_proc" in page
    r = cli.invoke(app, ["impact", "utils.db.run_proc"])
    assert r.exit_code == 0 and "refresh_orders_dag" in r.output and "nightly_procs" in r.output


def test_golden_airflow_util_catalog(util_hub):
    golden("scan_util_airflow.md", (util_hub / "catalog/airflow/pipelines.md").read_text())
    golden("scan_util_edges.md", (util_hub / "catalog/edges.md").read_text())


# ---------------- edges.ignore_tables ----------------

def test_ignore_tables_keep_edges_but_impact_does_not_traverse(tmp_path):
    hub = make_hub(tmp_path, UTIL_SCANS, ["util_repo"], extra="edges:\n  ignore_tables: [etl.load_state, ETL.run_log]\n")
    scan(hub)
    e = edges_of(hub)
    assert e["ignore_tables"] == ["etl.load_state", "etl.run_log"]
    assert e["tables"]["etl.load_state"]["writers"]          # its own edges are kept
    plain = build_edges_from(hub, [])
    # staging.orders_stage -> proc sp_refresh_orders -> (updates etl.load_state) -> sp_monitor: bookkeeping noise
    with_ignore = {r["reader"] for r in downstream(e, "staging.orders_stage", 4)}
    without = {r["reader"] for r in downstream(plain, "staging.orders_stage", 4)}
    assert "procedure:etl.sp_monitor" in without and "procedure:etl.sp_monitor" not in with_ignore
    assert with_ignore < without
    # asking about the ignored table itself still lists its readers
    assert {r["reader"] for r in downstream(e, "etl.load_state", 1)} == {r["reader"] for r in downstream(plain, "etl.load_state", 1)}


def build_edges_from(hub, ignore):
    """The same scan results rebuilt with a different ignore list."""
    results = [json.loads((hub / ".dea/scan_cache" / f"{n}.result.json").read_text()) for n in ("pipelines", "warehouse")]
    return build_edges(results, None, ignore)


def test_impact_cli_uses_the_ignore_list_from_the_graph(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, UTIL_SCANS, ["util_repo"], extra="edges:\n  ignore_tables: [etl.load_state]\n")
    scan(hub)
    monkeypatch.setenv("DEA_HUB", str(hub))
    out = CliRunner().invoke(app, ["impact", "staging.orders_stage"])
    assert out.exit_code == 0 and "sp_refresh_orders" in out.output
    assert "sp_monitor" not in out.output


# ---------------- python scan ----------------

@pytest.fixture
def py_hub(tmp_path):
    hub = make_hub(tmp_path, PY_SCANS, ["python_repo"])
    out = scan(hub)
    assert not out.failed, out.errors
    return hub


def owners(hub):
    res = json.loads((hub / ".dea/scan_cache/jobs.result.json").read_text())
    return {o["id"]: o for o in res["owners"]}


def direct(o):
    return {(t, "r") for r in o["direct"] for t in r["reads"]} | {(t, "w") for r in o["direct"] for t in r["writes"]}


def test_python_per_function_edges_for_sql_literals_vars_and_fstring_loops(py_hub):
    o = owners(py_hub)
    assert direct(o["py:jobs/load_orders.py:load_orders"]) == {
        ("staging.orders_stage", "r"), ("raw.orders", "w"), ("staging.orders_stage", "w")}
    assert direct(o["py:jobs/load_orders.py:refresh_marts"]) == {("raw.orders", "r"), ("analytics.daily_orders", "w")}  # via a local variable
    sa = o["py:jobs/load_orders.py:stage_all"]
    assert ("landing.customers", "r") in direct(sa) and ("staging.customers_stage", "w") in direct(sa)  # dict.items() rows stay paired
    assert ("landing.orders", "r") in direct(sa) and all(r["unrolled"] for r in sa["direct"])
    assert "py:jobs/load_orders.py:broken_dynamic" not in o   # execute(param): a template, carried by its callers


def test_python_pandas_and_sqlalchemy(py_hub):
    o = owners(py_hub)
    assert direct(o["py:jobs/pandas_job.py:export_daily"]) == {
        ("analytics.daily_orders", "r"), ("raw.customers", "r"), ("exports.daily_orders_export", "w")}
    assert direct(o["py:jobs/pandas_job.py:archive"]) == {("raw.orders", "r"), ("archive.orders", "w")}  # text(...)


def test_python_pyspark_tables_deltatable_and_paths(py_hub):
    o = owners(py_hub)
    assert direct(o["py:jobs/spark_job.py:build_silver"]) == {
        ("s3://acme-lake/raw/orders/", "r"), ("bronze.customers", "r"), ("silver.orders_enriched", "w"),
        ("s3://acme-lake/silver/orders_enriched/", "w")}
    assert direct(o["py:jobs/spark_job.py:upsert"]) == {("gold.customer_ltv", "r"), ("gold.customer_ltv", "w")}
    assert direct(o["py:jobs/spark_job.py:sql_step"]) == {
        ("silver.orders_enriched", "r"), ("gold.daily_revenue", "w"), ("public.refunds", "r"), ("s3://acme-lake/raw/orders/late/", "r")}


def test_python_notebooks_ipynb_and_databricks_source(py_hub):
    o = owners(py_hub)
    nb = o["nb:notebooks/orders_rollup.ipynb"]
    assert direct(nb) == {("raw.orders", "r"), ("analytics.orders_rollup_nb", "w"), ("analytics.orders_rollup_nb", "r")}
    assert {r["file"] for r in nb["direct"]} == {"notebooks/orders_rollup.ipynb#cell2:1", "notebooks/orders_rollup.ipynb#cell2:2",
                                                 "notebooks/orders_rollup.ipynb#cell3:1"}
    dbx = o["nb:notebooks/dbx_export.py"]
    assert direct(dbx) == {("silver.orders_enriched", "r"), ("gold.revenue_daily", "w"), ("gold.revenue_daily", "r"),
                           ("s3://acme-lake/export/revenue/", "w")}
    assert any(r["file"] == "notebooks/dbx_export.py:7" for r in dbx["direct"])   # the %sql cell, real line number


def test_python_util_attribution_two_hops_and_template_instantiation(py_hub):
    o = owners(py_hub)
    main = o["py:jobs/load_orders.py:main"]
    by_via = {(u["via"], tuple(u["calls"])) for u in main["util"]}
    assert ("util:lib.etl.run_step", ("etl.sp_rollup",)) in by_via                           # literal into CALL {proc}()
    assert ("util:lib.etl.nightly>lib.etl.run_step", ("etl.sp_step_one",)) in by_via         # for step in STEPS, two hops
    assert ("util:lib.etl.nightly>lib.etl.run_step", ("etl.sp_step_two",)) in by_via
    assert ("util:jobs.load_orders.load_orders", ()) in {(u["via"], ()) for u in main["util"]}
    assert "py:lib/etl.py:run_step" not in o                                                  # parameterised: nothing of its own
    assert {u["via"] for u in o["py:lib/etl.py:vacuum_all"]["util"]} == {"util:lib.etl.run_step"}
    mod = o["py:jobs/load_orders.py"]                                                         # `if __name__ == ...: main()`
    assert all(u["hops"] == 2 for u in mod["util"]) and not any("nightly" in u["via"] for u in mod["util"])


def test_python_edges_and_stats(py_hub):
    e = edges_of(py_hub)
    assert {x["id"] for x in e["tables"]["raw.orders"]["writers"]} >= {"py:jobs/load_orders.py:load_orders"}
    assert e["tables"]["s3://acme-lake/raw/orders/"]["readers"][0]["via"] == "python"
    assert ("nb:notebooks/dbx_export.py", "notebook", "") in edge_set(e, "gold.revenue_daily", "writers")
    st = json.loads((py_hub / ".dea/scan_cache/jobs.result.json").read_text())["stats"]
    assert st["parse_errors"] == 1 and st["notebooks"] == 2 and st["unresolved_sql"] == 0
    assert e["stats"].get("jobs.unresolved_util_args", 0) == 0


def test_python_scan_incremental_deterministic_and_golden(py_hub):
    first = {p.relative_to(py_hub).as_posix(): p.read_bytes() for p in (py_hub / "catalog").rglob("*") if p.is_file()}
    out = scan(py_hub)
    assert next(s for s in out.scans if s["name"] == "jobs")["parsed"] == 0
    scan(py_hub, full=True)
    assert {p.relative_to(py_hub).as_posix(): p.read_bytes() for p in (py_hub / "catalog").rglob("*") if p.is_file()} == first
    golden("scan_python.md", (py_hub / "catalog/python/jobs.md").read_text())


def test_python_scan_never_imports_or_runs_scanned_code(tmp_path):
    hub = make_hub(tmp_path, PY_SCANS, ["python_repo"])
    marker = tmp_path / "ran"
    (hub.parent / "python_repo/jobs/evil.py").write_text(
        f"open({str(marker)!r}, 'w').write('x')\nimport os\nos.system('touch {marker}')\n"
        "import psycopg\npsycopg.connect('x').cursor().execute('delete from boom.t')\n")
    scan(hub)
    assert not marker.exists()
    assert ("boom.t", "w") in direct(owners(hub)["py:jobs/evil.py"])


def test_python_scan_skips_virtualenvs_and_caches(tmp_path):
    hub = make_hub(tmp_path, PY_SCANS, ["python_repo"])
    v = hub.parent / "python_repo/.venv/lib"
    v.mkdir(parents=True)
    (v / "x.py").write_text("import psycopg\npsycopg.connect('x').cursor().execute('delete from venv.t')\n")
    scan(hub)
    assert "venv.t" not in edges_of(hub)["tables"]


def test_python_dynamic_sql_is_counted_unresolved(tmp_path):
    hub = make_hub(tmp_path, PY_SCANS, ["python_repo"])
    (hub.parent / "python_repo/jobs/dyn.py").write_text(
        "import psycopg\n\ndef go(conn, table):\n    conn.cursor().execute('select ' + str(len(table)))\n"
        "    conn.cursor().execute(open('q.sql').read())\n    conn.cursor().execute(f'select * from {conn.name}')\n")
    out = scan(hub)
    st = next(s for s in out.scans if s["name"] == "jobs")["stats"]
    assert st["unresolved_sql"] >= 2


# ---------------- pycode unit behaviour ----------------

def _funcs(src, dialect="postgres"):
    return pycode.CodeScanner(pycode.parse(src), "m.py", dialect).scan()["funcs"]


def test_parameter_templates_and_defaults():
    f = _funcs("def run(proc, schema='etl'):\n    cur.execute(f'call {schema}.{proc}()')\n")["run"]
    assert f["params"] == ["proc", "schema"] and f["defaults"] == {"schema": ["etl"]}
    assert f["sql"][0]["tpl"]["sql"] == ["call etl.\x01proc\x02()".replace("etl", "\x01schema\x02")]


def test_loop_over_param_binds_elements():
    g = pycode.UtilGraph({"u.py": {"funcs": _funcs("def run_all(procs):\n    for p in procs:\n        cur.execute(f'call {p}()')\n"),
                                   "imports": {}},
                          "d.py": {"funcs": _funcs("from u import run_all\nrun_all(['a.p1', 'a.p2'])\n"), "imports": {}}}, "postgres")
    out = graph_calls(g, "d.py")
    assert sorted(c for e in out for c in e["calls"]) == ["a.p1", "a.p2"]


def graph_calls(g, rel):
    return g.inherited(rel, g.files[rel]["funcs"]["<module>"]["calls"])


def test_cycles_and_unknown_callees_terminate():
    src = "def a():\n    b()\n\ndef b():\n    a()\n    cur.execute('select 1 from t.x')\n    zzz()\n"
    g = pycode.UtilGraph({"m.py": {"funcs": _funcs(src), "imports": {}}}, "postgres")
    assert [e["via"] for e in g.inherited("m.py", g.files["m.py"]["funcs"]["a"]["calls"])] == ["util:m.b"]


def test_relative_imports_and_reexports_resolve():
    files = {
        "pkg/__init__.py": "from .db import run_proc\n",
        "pkg/db.py": "def run_proc(p):\n    cur.execute('call ' + p + '()')\n",
        "pkg/sub/job.py": "from .. import run_proc\nrun_proc('etl.sp_x')\n",
    }
    g = pycode.UtilGraph({r: {"funcs": _funcs(s), "imports": pycode.alias_map(pycode.parse(s))} for r, s in files.items()}, "postgres")
    out = graph_calls(g, "pkg/sub/job.py")
    assert [e["calls"] for e in out] == [["etl.sp_x"]] and out[0]["via"] == "util:pkg.db.run_proc"


def test_databricks_cell_blanking_keeps_line_numbers():
    text, sql = py_scan.databricks_cells((FIX / "python_repo/notebooks/dbx_export.py").read_text())
    assert text.count("\n") == (FIX / "python_repo/notebooks/dbx_export.py").read_text().count("\n")
    assert [ln for ln, _ in sql] == [7] and "create or replace table gold.revenue_daily" in sql[0][1]
    assert 'df = spark.table("gold.revenue_daily")' in text.split("\n")[12]


# ---------------- terraform ----------------

@pytest.fixture
def tf_hub(tmp_path):
    hub = make_hub(tmp_path, TF_SCANS, ["tf_repo"])
    out = scan(hub)
    assert not out.failed, out.errors
    return hub


def test_terraform_resources_and_edges(tf_hub):
    e = edges_of(tf_hub)
    ids = lambda t, s: {x["id"] for x in e["tables"][t][s]}  # noqa: E731
    # DMS task: selection rules -> source table readers, S3 target writers (bucket resolved through var + local)
    assert ids("public.orders", "readers") == {"dms:orders-cdc"}
    tgt = e["tables"]["s3://acme-lake/raw/public/orders/"]
    assert [x["source"] for x in tgt["writers"]] == ["public.orders"]
    assert {x["id"] for x in tgt["readers"]} == {"glue:lake_raw.orders", "glue-job:silver-orders"}
    assert ids("lake_raw.orders", "writers") == {"glue:lake_raw.orders"}
    assert ids("s3://acme-lake/silver/orders/", "writers") == {"glue-job:silver-orders"}
    assert ids("s3://acme-lake/events/", "writers") == {"firehose:events-to-lake"}
    assert ids("kinesis://shop-events", "readers") == {"firehose:events-to-lake"}
    assert ids("s3://acme-lake/dags/", "readers") == {"mwaa:acme-airflow"}
    # BigQuery view reads, Pub/Sub subscription -> BigQuery table, Snowflake task SQL
    assert ids("raw.orders", "readers") == {"bq:analytics.daily_revenue", "sf:analytics.rollup"}
    assert ids("raw.orders_stream", "writers") == {"pubsub:orders-to-bq"}
    assert ids("analytics.orders_rollup", "writers") == {"sf:analytics.rollup"}
    # a glue job argument with no direction word is a read marked unclear; a commented-out resource does not exist
    assert e["tables"]["s3://acme-lake/reference/"]["readers"][0]["unclear"] is True
    assert "s3://ghost/" not in e["tables"]
    res = json.loads((tf_hub / ".dea/scan_cache/infra.result.json").read_text())
    assert {n["address"] for n in res["nodes"]} >= {"aws_s3_bucket.lake", "databricks_job.nightly", "google_pubsub_topic.orders"}
    assert "aws_s3_bucket.ghost" not in {n["address"] for n in res["nodes"]}


def test_terraform_unresolved_reference_is_listed_not_guessed(tf_hub):
    e = edges_of(tf_hub)
    assert {"kind": "terraform-ref", "name": "s3://${var.external_bucket}/x/", "referenced_by": "aws_glue_job.silver_orders"} in e["unresolved"]
    assert not any("external_bucket" in t for t in e["tables"])


def test_terraform_catalog_page_and_golden(tf_hub):
    page = (tf_hub / "catalog/terraform/infra.md").read_text()
    assert "## aws_dms_replication_task (1)" in page and "main.tf:37" in page
    golden("scan_terraform.md", page)


def test_terraform_regex_fallback_matches_hcl2(tmp_path, monkeypatch):
    if terraform.hcl2 is None:
        pytest.skip("python-hcl2 not installed")
    a = make_hub(tmp_path / "a", TF_SCANS, ["tf_repo"])
    scan(a)
    monkeypatch.setattr(terraform, "hcl2", None)
    b = make_hub(tmp_path / "b", TF_SCANS, ["tf_repo"])
    out = scan(b)
    assert next(s for s in out.scans if s["name"] == "infra")["stats"]["parser"] == "regex"

    def norm(h):
        d = json.loads((h / ".dea/scan_cache/infra.result.json").read_text())
        d["stats"].pop("parser")
        d.pop("revision")
        return d

    assert norm(a) == norm(b)
    assert edges_of(a)["tables"] == edges_of(b)["tables"]


def test_terraform_works_without_hcl2_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(terraform, "hcl2", None)
    hub = make_hub(tmp_path, TF_SCANS, ["tf_repo"])
    scan(hub)
    e = edges_of(hub)
    assert [x["source"] for x in e["tables"]["s3://acme-lake/raw/public/customers/"]["writers"]] == ["public.customers"]
    assert e["tables"]["analytics.orders_rollup"]["writers"][0]["id"] == "sf:analytics.rollup"   # heredoc statement


def test_terraform_parser_change_invalidates_the_cache(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, TF_SCANS, ["tf_repo"])
    scan(hub)
    assert next(s for s in scan(hub).scans)["parsed"] == 0
    monkeypatch.setattr(terraform, "hcl2", None)
    assert next(s for s in scan(hub).scans)["parsed"] == 2


@pytest.mark.parametrize("src,expect", [
    ('resource "aws_s3_bucket" "a" {\n  bucket = "x" # trailing\n  tags = { Name = "}" }\n}\n', ["aws_s3_bucket.a"]),
    ('/* resource "aws_s3_bucket" "b" {} */\nresource "aws_s3_bucket" "a" {\n  bucket = "x"\n}\n', ["aws_s3_bucket.a"]),
    ('resource "aws_s3_bucket" "a" {\n  policy = <<EOT\n{ "x": "resource \\"y\\" \\"z\\" {" }\nEOT\n}\nresource "aws_kinesis_stream" "k" { name = "n" }\n',
     ["aws_s3_bucket.a", "aws_kinesis_stream.k"]),
    ('resource "aws_s3_bucket" "a" {\n  bucket = "unterminated\n', ["aws_s3_bucket.a"]),
])
def test_terraform_reader_handles_awkward_blocks(src, expect, monkeypatch):
    monkeypatch.setattr(terraform, "hcl2", None)
    r = terraform.scan_terraform_text(src, "x.tf", "postgres")
    assert [f"{x['type']}.{x['name']}" for x in r["resources"]] == expect


def test_terraform_garbage_does_not_crash():
    for junk in ("", "\x00\x01", "resource", 'resource "a"', "{{{{", '"' * 50, "a = [[[[[[[[[[[[[[[[[[[[1]]]]]]]]]]]]]]]]]]]]"):
        assert terraform.scan_terraform_text(junk, "x.tf", "postgres")["rel"] == "x.tf"


# ---------------- docs / cli ----------------

def test_scan_command_prints_new_scan_types(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, PY_SCANS + TF_SCANS.split("\n", 1)[1], ["python_repo", "tf_repo"])
    monkeypatch.setenv("DEA_HUB", str(hub))
    r = CliRunner().invoke(app, ["scan"])
    assert r.exit_code == 0, r.output
    assert "jobs: python ok" in r.output and "owners=" in r.output and "infra: terraform ok" in r.output and "resources=15" in r.output
    assert cli_mod  # imported for the CLI wiring
