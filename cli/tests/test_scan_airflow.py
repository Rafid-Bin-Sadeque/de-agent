"""M1c airflow scanner: DAG discovery, datasets, SQL, symbols, and the never-execute guarantee."""
import json
import sys
from pathlib import Path

import pytest
from de_agent.config import load_config
from de_agent.scanners.airflow import scan_python
from de_agent.scanners.run import run_scan

FIX = Path(__file__).resolve().parent / "fixtures" / "airflow_repo"


def scan_file(name, dialect="postgres"):
    rel = f"dags/{name}"
    return scan_python((FIX / rel).read_text(), rel, dialect)


def by_id(res):
    return {d["dag_id"]: d for d in res["dags"]}


def hub_result(hub):
    run_scan(hub, load_config(hub / "de-agent.yaml"))
    return json.loads((hub / ".dea" / "scan_cache" / "pipelines.result.json").read_text())


def test_taskflow_dag_and_asset_outlet():
    d = by_id(scan_file("taskflow.py"))["taskflow_orders"]
    assert not d["dynamic"] and d["owner"] == "team-tf" and d["schedule"] == "@daily"
    assert d["tags"] == ["acme", "taskflow"] and "@task" in d["operators"]
    assert d["outlets"] == [{"uri": "warehouse://raw/orders", "unrolled": False, "line": 11}]


def test_with_dag_nested_in_function():
    d = by_id(scan_file("nested_with.py"))["nested_dag"]
    assert d["line"] == 8 and not d["dynamic"] and d["operators"] == ["EmptyOperator"]


def test_factory_loop_gives_one_dynamic_entry_per_call_site():
    res = by_id(scan_file("factory.py"))
    assert {"export_eu", "export_us"} <= set(res)
    for i in ("export_eu", "export_us"):
        assert res[i]["dynamic"] and res[i]["pattern"] == "export_{region}"
    unk = res["report_{name}"]  # argument is not a constant: stays a pattern, never guessed
    assert unk["dynamic"] and unk["pattern"] == "report_{name}"


def test_dataset_producers_consumers_and_schedule_inlets():
    res = by_id(scan_file("datasets_2x.py"))
    assert sorted(o["uri"] for o in res["ds_producer"]["outlets"]) == ["warehouse://raw/events", "warehouse://raw/orders"]
    cons = {i["uri"] for i in res["ds_consumer"]["inlets"]}
    assert cons == {"warehouse://raw/orders", "warehouse://staging/stg_orders"}


def test_sensor_trigger_pool():
    d = by_id(scan_file("sensors.py"))["waiter"]
    assert d["sensors"] == ["ds_producer"] and d["triggers"] == ["ds_consumer"] and d["pools"] == ["sensors"]
    assert {"ExternalTaskSensor", "TriggerDagRunOperator"} <= set(d["operators"])


def test_sql_kwarg_hook_concat_format_and_unparsed():
    k = by_id(scan_file("sql_kwarg.py"))["sql_kwarg"]
    assert k["writes"] == ["analytics.daily_orders"] and k["reads"] == ["staging.stg_orders"]
    h = by_id(scan_file("sql_hook.py"))["sql_hook"]
    assert "raw.customers" in h["writes"]                       # hook.run(update ...)
    assert {"raw.customers", "raw.products"} <= set(h["reads"])  # BASE + " where" and "...".format(SCHEMA)
    assert any(r["unparsed"] and r["raw"].startswith("select * from") for r in h["sql"])


def test_sql_file_and_call_proc_via_run(scan_hub):
    res = hub_result(scan_hub)
    d = {x["dag_id"]: x for x in res["dags"]}
    assert d["sql_file"]["writes"] == ["analytics.x_summary"]
    assert d["sql_file"]["reads"] == ["raw.customers", "raw.orders"]
    assert d["call_proc"]["calls"] == ["etl.no_such_proc", "etl.refresh_orders"]


def test_dynamic_unresolved_things_are_counted_not_guessed(scan_hub):
    res = hub_result(scan_hub)
    st = res["stats"]
    assert st["unresolved_sql"] >= 2        # f"truncate {hook.schema}.x" and f"delete from raw.{name}"
    assert st["unresolved_datasets"] == 1   # Dataset(make_uri())
    assert st["unparsed_sql"] >= 1
    u = {x["dag_id"]: x for x in res["dags"]}["unrolled"]
    assert "raw.dynamic" not in u["writes"] and not any("{" in t for t in u["writes"] + u["reads"])


def test_unrolled_loop_over_constant_tuple(scan_hub):
    d = {x["dag_id"]: x for x in hub_result(scan_hub)["dags"]}["unrolled"]
    assert d["writes"] == ["staging.copy_customers", "staging.copy_products"]
    assert [x["table"] for x in d["dataset_writes"]] == ["staging.copy_customers", "staging.copy_products"]
    assert all(x["unrolled"] for x in d["dataset_writes"])


def test_dataset_tables_pattern_gives_dataset_edges(scan_hub):
    run_scan(scan_hub, load_config(scan_hub / "de-agent.yaml"))
    edges = json.loads((scan_hub / "catalog" / "edges.json").read_text())
    w = {e["id"]: e for e in edges["tables"]["raw.orders"]["writers"]}
    assert w["dag:taskflow_orders"]["via"] == "dataset"
    r = {e["id"]: e for e in edges["tables"]["raw.orders"]["readers"]}
    assert r["dag:ds_consumer"]["via"] == "dataset"                    # inlet (schedule) -> reader edge
    cu = {e["id"]: e for e in edges["tables"]["staging.copy_customers"]["writers"]}
    assert cu["dag:unrolled"]["via"] in ("dataset", "unrolled")
    ds_edges = [e for e in edges["tables"]["staging.copy_customers"]["writers"] if e["via"] == "dataset"]
    assert ds_edges and ds_edges[0]["unrolled"] is True


def test_without_dataset_tables_no_dataset_edges(scan_hub):
    cfg = (scan_hub / "de-agent.yaml").read_text().replace(
        ' dataset_tables: ["warehouse://{schema}/{table}"]', "")
    (scan_hub / "de-agent.yaml").write_text(cfg)
    run_scan(scan_hub, load_config(scan_hub / "de-agent.yaml"))
    edges = json.loads((scan_hub / "catalog" / "edges.json").read_text())
    assert not any(e["via"] == "dataset" for t in edges["tables"].values() for e in t["writers"] + t["readers"])


def test_symbol_index_aliases_star_import_and_dynamic_calls(scan_hub):
    res = hub_result(scan_hub)
    s = res["symbols"]
    assert set(s) == {"common.pg_utils.copy_df", "common.pg_utils.get_conn", "common.pg_utils.Loader",
                      "utils.dates.ds_to_date", "utils.dates.unused_symbol"}   # private names excluded
    users = {k: sorted({d["dag_id"] for d in v["dags"]}) for k, v in s.items()}
    assert users["common.pg_utils.copy_df"] == ["taskflow_orders", "util_user"]  # direct + aliased `copy_df as cdf`
    assert users["common.pg_utils.get_conn"] == ["util_user"]                    # via `import ... as pgu`
    assert users["common.pg_utils.Loader"] == ["util_user"]
    assert users["utils.dates.ds_to_date"] == ["util_user"]                      # via `from utils import dates`
    assert users["utils.dates.unused_symbol"] == []
    assert s["common.pg_utils.copy_df"]["doc"] == "Copy a dataframe into a table."
    assert res["stats"]["unresolved_calls"] >= 2                                  # star import + getattr/import_module


def test_never_imports_or_executes_scanned_code(tmp_path):
    sentinel = tmp_path / "SENTINEL"
    repo = tmp_path / "repo" / "dags"
    repo.mkdir(parents=True)
    (repo / "evil.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('executed')\n"
        "raise RuntimeError('boom at import')\n"
        "from airflow import DAG\n"
        "with DAG('sentinel_dag', schedule='@daily'):\n    pass\n")
    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "de-agent.yaml").write_text("version: 1\nscans:\n  - {name: p, type: airflow, path: ../repo}\n")
    before = set(sys.modules)
    run_scan(hub, load_config(hub / "de-agent.yaml"))
    assert not sentinel.exists()
    assert "evil" not in set(sys.modules) - before
    res = json.loads((hub / ".dea" / "scan_cache" / "p.result.json").read_text())
    assert [d["dag_id"] for d in res["dags"]] == ["sentinel_dag"]


@pytest.mark.parametrize("src,expected", [
    ("from airflow import DAG\nDAG('a')\nx = DAG(dag_id='b')\n", {"b"}),   # bare call without `as`/with: only assignments count
    ("from airflow import DAG\nfor i in range(2):\n    with DAG(f'r{i}'):\n        pass\n", {"r0", "r1"}),
    ("from airflow import DAG\nwith DAG(dag_id='kw'):\n    pass\n", {"kw"}),
])
def test_dag_id_forms(src, expected):
    assert {d["dag_id"] for d in scan_python(src, "x.py", "postgres")["dags"]} == expected


def test_syntax_error_is_recorded_not_raised():
    r = scan_python("def broken(:\n", "bad.py", "postgres")
    assert r["dags"] == [] and "SyntaxError" in r["error"]
