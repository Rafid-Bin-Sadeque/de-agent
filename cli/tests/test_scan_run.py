"""M1c: dbt/sql scanners, edges, determinism, incremental scans, profile headers, lint, find, CLI."""
import json
import time
from pathlib import Path

import pytest
from conftest import PIPELINES_PAGE
from de_agent.cli import app
from de_agent.config import ConfigError, load_config, parse_config
from de_agent.lint import run_lint
from de_agent.scanners import headers
from de_agent.scanners.edges import build_edges, downstream
from de_agent.scanners.run import run_scan
from typer.testing import CliRunner


def scan(hub, **kw):
    return run_scan(hub, load_config(hub / "de-agent.yaml"), **kw)


def edges_of(hub):
    return json.loads((hub / "catalog" / "edges.json").read_text())


def catalog_bytes(hub):
    return {p.relative_to(hub).as_posix(): p.read_bytes() for p in sorted((hub / "catalog").rglob("*")) if p.is_file()}


# ---------------- config ----------------

def test_scan_config_validation():
    cfg = parse_config("scans:\n  - {name: a, type: airflow, path: x, dataset_tables: ['w://{schema}/{table}']}\n")
    assert cfg.scans[0].dag_folders == ["dags"] and cfg.scans[0].project_name == "a"
    with pytest.raises(ConfigError, match="unknown scan type"):
        parse_config("scans:\n  - {name: a, type: nope, path: x}\n")
    with pytest.raises(ConfigError, match="must contain"):
        parse_config("scans:\n  - {name: a, type: airflow, path: x, dataset_tables: ['w://{schema}']}\n")
    with pytest.raises(ConfigError, match="duplicate scan names"):
        parse_config("scans:\n  - {name: a, type: sql, path: x}\n  - {name: a, type: sql, path: y}\n")


# ---------------- dbt / sql / edges ----------------

def test_dbt_models_sources_exposures(scan_hub):
    scan(scan_hub)
    res = json.loads((scan_hub / ".dea/scan_cache/analytics.result.json").read_text())
    names = {m["name"]: m for m in res["models"]}
    assert names["fct_orders"]["relation"] == "analytics.fct_orders" and names["fct_orders"]["materialization"] == "incremental"
    assert names["stg_orders"]["parents"] == ["raw.orders"]
    assert names["stg_orders"]["downstream"] == 4  # dim_customers, fct_orders, mart_daily_revenue, exposure
    assert res["exposures"][0]["reads"] == ["analytics.mart_daily_revenue"]


def test_sql_objects_parse_vs_heuristic(scan_hub):
    scan(scan_hub)
    res = json.loads((scan_hub / ".dea/scan_cache/warehouse_sql.result.json").read_text())
    o = {x["name"]: x for x in res["objects"]}
    assert o["etl.rollup_sales"]["heuristic"] is False and o["etl.rollup_sales"]["writes"] == ["analytics.sales_rollup"]
    assert o["etl.refresh_orders"]["heuristic"] is True        # plpgsql body: regex fallback
    assert o["etl.refresh_orders"]["writes"] == ["etl.audit_log", "raw.orders"]
    assert o["analytics.v_orders"]["kind"] == "view" and o["analytics.v_orders"]["reads"] == ["raw.customers", "raw.orders"]
    assert o["etl.audit_log"]["kind"] == "table"


def test_cross_project_edge_dag_calls_proc_that_writes_table_read_by_dbt(scan_hub):
    scan(scan_hub)
    e = edges_of(scan_hub)
    w = [x for x in e["tables"]["raw.orders"]["writers"] if x["via"] == "proc"]
    assert w and w[0]["id"] == "dag:call_proc" and w[0]["proc"] == "etl.refresh_orders"
    assert any(c["table"] == "raw.orders" and c["writer"] == "dag:call_proc" and c["reader"] == "dbt:stg_orders"
               and c["writer_project"] == "pipelines" and c["reader_project"] == "analytics" for c in e["cross_project"])
    assert {"kind": "proc", "name": "etl.no_such_proc", "referenced_by": "dag:call_proc"} in e["unresolved"]
    md = (scan_hub / "catalog/edges.md").read_text()
    assert "## Cross-project" in md and "raw.orders: dag:call_proc [pipelines] → dbt:stg_orders [analytics]" in md


def test_downstream_depth_and_paths(scan_hub):
    scan(scan_hub)
    e = edges_of(scan_hub)
    rows = downstream(e, "staging.stg_orders", 3)
    by = {(r["reader"]): r for r in rows}
    assert by["dbt:fct_orders"]["depth"] == 1 and by["dbt:mart_daily_revenue"]["depth"] == 2
    assert by["exposure:revenue_dashboard"]["depth"] == 3
    assert by["exposure:revenue_dashboard"]["path"][-1] == "[exposure:revenue_dashboard]"
    assert all(r["depth"] <= 3 for r in downstream(e, "raw.orders", 3))
    assert not any(r["depth"] > 1 for r in downstream(e, "staging.stg_orders", 1))


def test_self_reader_is_not_downstream():
    results = [{"type": "airflow", "scan": "p", "project": "p", "revision": "r", "stats": {}, "symbols": {}, "dags": [{
        "dag_id": "d", "dynamic": False, "file": "f.py", "line": 1, "sql": [], "reads": ["raw.a"], "writes": ["raw.a"],
        "calls": [], "dataset_writes": [], "dataset_reads": [], "outlets": [], "inlets": [], "sensors": [], "triggers": []}]}]
    e = build_edges(results)
    assert downstream(e, "raw.a") == []


# ---------------- determinism / incremental ----------------

def test_two_scans_of_unchanged_sources_are_byte_identical(scan_hub):
    scan(scan_hub)
    first = catalog_bytes(scan_hub)
    time.sleep(1.1)  # a wall-clock timestamp anywhere in catalog/ would differ now
    scan(scan_hub, full=True)
    assert catalog_bytes(scan_hub) == first
    page = (scan_hub / "projects/pipelines.md").read_text()
    scan(scan_hub)
    assert (scan_hub / "projects/pipelines.md").read_text() == page
    for rel, data in first.items():
        if rel.endswith(".md"):
            fm = data.decode().split("---")[1]
            assert "generated: " in fm and "T" not in fm.split("generated:")[1].split("\n")[0]  # revision, not a time


def test_scan_time_lives_in_state_not_catalog(scan_hub):
    scan(scan_hub)
    st = json.loads((scan_hub / ".dea/scan_state.json").read_text())
    assert st["scans"]["pipelines"]["scanned_at"].startswith("20") and "last_scan_at" in st
    assert st["scans"]["pipelines"]["files"]["dags/taskflow.py"]  # per-file sha1
    assert not any(b"scanned_at" in b for b in catalog_bytes(scan_hub).values())


def test_incremental_reparses_only_changed_file(scan_hub):
    first = scan(scan_hub)
    p = next(s for s in first.scans if s["name"] == "pipelines")
    assert p["parsed"] == 15 and p["cached"] == 0   # 15 .py files (the .sql file is hashed, not parsed)
    second = scan(scan_hub)
    p2 = next(s for s in second.scans if s["name"] == "pipelines")
    assert p2["parsed"] == 0 and p2["cached"] == 15
    f = scan_hub.parent / "airflow_repo/dags/sql_kwarg.py"
    f.write_text(f.read_text().replace("analytics.daily_orders", "analytics.daily_orders2"))
    third = scan(scan_hub)
    p3 = next(s for s in third.scans if s["name"] == "pipelines")
    assert p3["parsed"] == 1
    assert "analytics.daily_orders2" in edges_of(scan_hub)["tables"]
    assert "analytics.daily_orders" not in edges_of(scan_hub)["tables"]


def test_only_name_reuses_other_scans(scan_hub):
    scan(scan_hub)
    before = catalog_bytes(scan_hub)
    out = scan(scan_hub, only=["warehouse_sql"])
    assert [s["name"] for s in out.scans] == ["warehouse_sql"]
    assert catalog_bytes(scan_hub) == before


def test_missing_dbt_manifest_is_skipped_with_a_hint_not_an_error(scan_hub):
    (scan_hub.parent / "analytics/target/manifest.json").unlink()
    out = scan(scan_hub)
    assert not out.failed and not out.errors
    assert any("skipped: run dbt parse" in w for w in out.warnings)
    assert [s["status"] for s in out.scans if s["name"] == "analytics"] == ["skipped"]
    assert (scan_hub / "catalog/airflow/pipelines.md").exists()  # other scans still produced


def test_cli_scan_without_dbt_manifest_exits_0(scan_hub, monkeypatch):
    from de_agent.cli import app
    from typer.testing import CliRunner

    (scan_hub.parent / "analytics/target/manifest.json").unlink()
    monkeypatch.setenv("DEA_HUB", str(scan_hub))
    res = CliRunner().invoke(app, ["scan"])
    assert res.exit_code == 0, res.output
    assert "skipped: run dbt parse" in res.output and "pipelines: airflow ok" in res.output


# ---------------- profile headers ----------------

def test_header_inserted_after_frontmatter_and_human_text_byte_identical(scan_hub):
    scan(scan_hub)
    new = (scan_hub / "projects/pipelines.md").read_text()
    assert headers.marker_state(new) == "ok"
    fm_end = PIPELINES_PAGE.index("---\n", 4) + 4
    a, b = new.index(headers.START), new.index(headers.END) + len(headers.END)
    assert new[:a] == PIPELINES_PAGE[:fm_end]                      # frontmatter untouched, incl. `updated:`
    assert new[b:].lstrip("\n") == PIPELINES_PAGE[fm_end:]          # human body untouched, trailing spaces kept
    assert "updated: 2026-10-01" in new[:a]
    assert "Second line with trailing spaces   \n" in new


def test_header_rewrite_between_existing_markers_leaves_everything_else(tmp_path):
    page = tmp_path / "p.md"
    human_before = "---\nowner: x\nupdated: 2026-01-01\n---\n# T\n\nintro\n\n"
    human_after = "\n\n## Gotchas\n\n- keep   me\n"
    page.write_text(human_before + headers.START + "\nOLD\n" + headers.END + human_after)
    assert headers.rewrite_profile(page, headers.START + "\nNEW\n" + headers.END) == "updated"
    assert page.read_text() == human_before + headers.START + "\nNEW\n" + headers.END + human_after
    assert headers.rewrite_profile(page, headers.START + "\nNEW\n" + headers.END) == "unchanged"


def test_header_page_without_frontmatter_inserted_at_top(tmp_path):
    page = tmp_path / "p.md"
    body = "# No frontmatter\n\nhello\n"
    page.write_text(body)
    block = headers.START + "\nX\n" + headers.END
    assert headers.rewrite_profile(page, block) == "updated"
    new = page.read_text()
    assert new.startswith(headers.START) and new.endswith(body) and new == block + "\n\n" + body


def test_header_with_broken_markers_left_untouched(tmp_path):
    page = tmp_path / "p.md"
    text = "# T\n" + headers.START + "\nno end marker\n"
    page.write_text(text)
    assert headers.rewrite_profile(page, headers.START + "\nX\n" + headers.END) == "broken-markers"
    assert page.read_text() == text


def test_missing_profile_page_skips_header(scan_hub):
    (scan_hub / "projects/analytics.md").unlink()
    out = scan(scan_hub)
    assert "no profile page" in out.profiles["analytics"]


# ---------------- lint ----------------

def test_lint_exempts_catalog_and_generated_block_from_budgets(scan_hub):
    (scan_hub / "map.md").write_text("# map\n\n| project |\n|---|\n| pipelines |\n| analytics |\n")
    big = "\n".join(f"- row {i}" for i in range(200))
    scan(scan_hub)
    (scan_hub / "catalog" / "huge.md").write_text(big)
    page = scan_hub / "projects/pipelines.md"
    text = page.read_text()
    # push the GENERATED block alone over the 150-line budget: still no page-budget error
    a = text.index(headers.END)
    page.write_text(text[:a] + "\n".join(f"- gen line {i}" for i in range(200)) + "\n" + text[a:])
    errs = {(f.rule, f.path) for f in run_lint(scan_hub) if f.level == "error"}
    assert ("page-budget", "projects/pipelines.md") not in errs
    assert not any(p.startswith("catalog/") for _, p in errs)
    page.write_text(text + "\n".join(f"human {i}" for i in range(200)))  # human lines still count
    assert ("page-budget", "projects/pipelines.md") in {(f.rule, f.path) for f in run_lint(scan_hub)}


@pytest.mark.parametrize("which", ["start", "end"])
def test_lint_errors_on_unbalanced_markers(scan_hub, which):
    (scan_hub / "map.md").write_text("# map\n\n| project |\n|---|\n| pipelines |\n| analytics |\n")
    scan(scan_hub)
    page = scan_hub / "projects/pipelines.md"
    text = page.read_text().replace(headers.START if which == "start" else headers.END, "")
    page.write_text(text)
    assert ("generated-markers", "projects/pipelines.md") in {(f.rule, f.path) for f in run_lint(scan_hub)}


# ---------------- find ----------------

def test_find_indexes_catalog_with_kind_catalog(scan_hub):
    from de_agent.find import find

    scan(scan_hub)
    hits = find(scan_hub, "raw.orders", 10)
    kinds = {h.path: h.kind for h in hits}
    assert kinds["catalog/edges.md"] == "catalog" and "catalog/airflow/pipelines.md" in kinds
    top = next(h for h in hits if h.path == "catalog/edges.md")
    assert top.snippet.startswith("raw.orders ←")


# ---------------- CLI ----------------

@pytest.fixture
def cli(scan_hub, monkeypatch):
    monkeypatch.setenv("DEA_HUB", str(scan_hub))
    r = CliRunner()
    assert r.invoke(app, ["scan"]).exit_code == 0
    return r


def test_cli_edges_impact_and_unknown_names(cli):
    out = cli.invoke(app, ["edges", "analytics.fct_orders"])
    assert out.exit_code == 0 and "dbt:fct_orders" in out.output and "dbt:mart_daily_revenue" in out.output
    out = cli.invoke(app, ["impact", "raw.orders"])
    assert out.exit_code == 0 and "dbt:stg_orders" in out.output and "->" in out.output
    out = cli.invoke(app, ["impact", "common.pg_utils.copy_df"])
    assert out.exit_code == 0 and "taskflow_orders" in out.output and "util_user" in out.output
    out = cli.invoke(app, ["impact", "copy_df"])  # bare name resolves by suffix
    assert out.exit_code == 0 and "util_user" in out.output
    out = cli.invoke(app, ["edges", "analytics.fct_order"])
    assert out.exit_code == 1
    closest = out.output.split("closest:")[1].strip().split(", ")
    assert closest[0] == "analytics.fct_orders" and len(closest) <= 5
    out = cli.invoke(app, ["impact", "common.pg_utils.copy_dff"])
    assert out.exit_code == 1 and "common.pg_utils.copy_df" in out.output


def test_impact_default_depth_is_5_and_max_10(cli, monkeypatch):
    seen = []
    import de_agent.scanners.edges as edges_mod

    real = edges_mod.downstream
    monkeypatch.setattr(edges_mod, "downstream", lambda e, t, d: (seen.append(d), real(e, t, d))[1])
    assert cli.invoke(app, ["impact", "raw.orders"]).exit_code == 0
    assert cli.invoke(app, ["impact", "raw.orders", "--depth", "10"]).exit_code == 0
    assert seen == [5, 10]
    assert cli.invoke(app, ["impact", "raw.orders", "--depth", "11"]).exit_code == 2


def test_cli_scan_without_scans_config(example_hub, monkeypatch):
    monkeypatch.setenv("DEA_HUB", str(example_hub))
    assert CliRunner().invoke(app, ["scan"]).exit_code == 2


def test_cli_scan_unknown_name(scan_hub, monkeypatch):
    monkeypatch.setenv("DEA_HUB", str(scan_hub))
    assert CliRunner().invoke(app, ["scan", "--name", "nope"]).exit_code == 1


def test_scanner_modules_do_not_import_scanned_code_names():
    """Belt and braces: the scanners never call exec/eval/import_module/__import__/compile on scanned text."""
    import re

    src = Path(__file__).resolve().parents[1] / "src/de_agent/scanners"
    for f in src.glob("*.py"):
        text = re.sub(r'"[^"\n]*"|\'[^\'\n]*\'', '""', f.read_text())
        assert not re.search(r"(?<!re\.)\b(exec|eval|compile|__import__|importlib)\s*\(", text), f.name


def test_heuristic_sql_ignores_non_table_from():
    from de_agent.scanners.common import heuristic_sql
    body = ("begin select extract(hour from k.created_at), trim(both ' ' from k.note) from stg.orders k "
            "where k.a is distinct from k.b; end")
    assert heuristic_sql(body)["reads"] == ["stg.orders"]


def test_scan_python_silences_syntax_warnings_of_scanned_code(recwarn):
    from de_agent.scanners.airflow import scan_python
    r = scan_python('import re\nRX = re.compile("\\[a-z]+")\n', "dags/x.py", "postgres")
    assert "error" not in r
    assert not [w for w in recwarn if issubclass(w.category, SyntaxWarning)]


def test_cli_edges_and_impact_accept_an_unqualified_table_when_unique(cli):
    out = cli.invoke(app, ["edges", "fct_orders"])
    assert out.exit_code == 0 and out.output.startswith("analytics.fct_orders") and "dbt:mart_daily_revenue" in out.output
    out = cli.invoke(app, ["impact", "stg_orders"])
    assert out.exit_code == 0 and "impact of table staging.stg_orders" in out.output


def test_cli_unqualified_table_that_is_ambiguous_lists_candidates(cli, monkeypatch):
    import de_agent.cli as climod

    monkeypatch.setattr(climod, "_load_edges", lambda hub: {"tables": {"a.orders": {"writers": [], "readers": []},
                                                                         "b.orders": {"writers": [], "readers": []}}})
    out = cli.invoke(app, ["edges", "orders"])
    assert out.exit_code == 1 and "a.orders" in out.output and "b.orders" in out.output and "ambiguous" in out.output


def test_cli_find_without_hits_says_so(cli):
    out = cli.invoke(app, ["find", "zzz_no_such_identifier_zzz"])
    assert out.exit_code == 1 and "no hits for zzz_no_such_identifier_zzz" in out.output
