"""AWS render goldens, the split layout, and the AWS lineage edges (DMS -> S3 -> Glue, DMS -> Redshift -> dbt)."""
import copy
import json
from pathlib import Path

import pytest
from de_agent.config import load_config
from de_agent.diff import diff_results
from de_agent.render import aws_signals, render, render_pages, signals_for
from de_agent.scanners.edges import build_edges, downstream, s3_node
from de_agent.scanners.run import run_scan

FIX = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden"


def snap():
    return json.loads((FIX / "aws_snapshot.json").read_text())


def _golden(name, text):
    g = GOLDEN / name
    if not g.exists():
        g.write_text(text)
    assert text == g.read_text(), f"{name} differs from the golden file"


# ---------------------------------------------------------------- render

def test_full_page_golden_and_one_h2_per_service():
    md = render(snap())
    _golden("aws_full.md", md)
    assert [ln for ln in md.splitlines() if ln.startswith("## ")] == [
        "## Signals", "## DMS", "## Glue", "## S3", "## Athena", "## Firehose", "## CloudWatch", "## Collection"]
    assert md == render(snap())  # deterministic


def test_one_service_page_golden_excerpt():
    md = render(snap())
    start = md.index("## DMS")
    excerpt = md[start:md.index("## Glue")]
    _golden("aws_dms_section.md", excerpt)
    assert "acme-orders-cdc" in excerpt and "Top tables by CDC changes (since task start)" in excerpt


def test_signals_of_the_healthy_fixture_are_only_the_alarm_and_the_failed_query():
    sig = signals_for(snap())
    assert sig == ["athena workgroup primary: 1 failed query(ies) in 24h (SYNTAX_ERROR: line 1:8: Column 'x' cannot be resolved)",
                   "cloudwatch alarm dms-cdc-latency-high in ALARM since 2026-10-20 10:15"]


def test_s3_freshness_signal_uses_max_age_option():
    s = snap()
    s["data"]["s3_prefixes"][0]["newest_at"] = "2026-10-19T08:00:00Z"  # 28 h before collected_at
    assert any("s3 s3://acme-lake/bronze/: newest object is older than 26 h" in x for x in signals_for(s))
    s["data"]["meta"]["max_age_h"] = 48.0
    assert not any("newest object" in x for x in signals_for(s))


def test_glue_signals_failed_run_and_duration_regression_and_crawler():
    s = snap()
    runs = [{"job": "j", "run_id": f"r{i}", "state": "SUCCEEDED", "started": f"2026-10-1{i}T03:00:00Z", "duration_s": 100}
            for i in range(1, 5)]
    runs.append({"job": "j", "run_id": "r9", "state": "SUCCEEDED", "started": "2026-10-20T03:00:00Z", "duration_s": 250})
    s["data"]["glue_job_runs"] = runs
    assert any("glue job j: last run 4 min, > 2x the median of the previous 4" in x for x in signals_for(s))
    runs[-1]["duration_s"] = 150
    assert not any("median" in x for x in signals_for(s))
    runs.append({"job": "k", "run_id": "f1", "state": "FAILED", "started": "2026-10-20T05:00:00Z", "error": "OutOfMemoryError: Java heap " + "x" * 400})
    sig = [x for x in signals_for(s) if x.startswith("glue job k")]
    assert len(sig) == 1 and "1 failed run(s) in 24h: OutOfMemoryError" in sig[0] and len(sig[0]) < 200
    s["data"]["glue_crawlers"][0].update(last_status="FAILED", error="Crawler cannot access s3")
    assert any(x.startswith("glue crawler bronze_crawler last crawl failed") for x in signals_for(s))


def test_firehose_and_dms_signals_at_thresholds():
    s = snap()
    f = s["data"]["firehose_streams"][0]
    f["freshness_p95_s"] = 600.0  # exactly 2x the buffer: no signal
    assert not any("freshness" in x for x in signals_for(s))
    f["freshness_p95_s"] = 601.0
    assert any("freshness" in x for x in signals_for(s))
    f["status"] = "DELETING"
    assert "firehose stream acme-clicks is DELETING" in signals_for(s)
    t = s["data"]["dms_tasks"][0]
    t["cdc_latency_target_p95_s"] = 300.0
    assert not any("CDC latency" in x for x in signals_for(s))
    t["cdc_latency_target_p95_s"] = 301.0
    assert any("CDC latency p95 301 s" in x for x in signals_for(s))
    assert aws_signals({}) == []


def test_diff_between_two_aws_snapshots():
    old, new = snap(), snap()
    new["data"]["dms_tasks"][1]["status"] = "failed"
    new["data"]["dms_tasks"][1]["cdc_latency_target_p95_s"] = 5000.0  # volatile: not reported as a field change
    new["data"]["glue_job_runs"].append({"job": "load_orders", "run_id": "jr_a2", "state": "FAILED", "started": "2026-10-20T11:00:00Z",
                                         "duration_s": 5, "dpu_seconds": 1, "error": "boom"})
    d = diff_results(old, new)
    assert any("dms task acme-dwh-cdc is failed" in s for s in d["signals_new"])
    assert any("glue job load_orders" in s for s in d["signals_new"])
    assert d["lists"]["dms_tasks"]["changed"][0]["changes"][0]["field"] == "status"
    assert diff_results(old, old)["lists"] == {} and diff_results(old, old)["signals_new"] == []


def _big():
    s = snap()
    base = s["data"]["glue_tables"][0]
    s["data"]["glue_tables"] = [{**copy.deepcopy(base), "name": f"t{i:03d}", "location": f"s3://acme-lake/bronze/t{i:03d}/"}
                                for i in range(150)]
    s["data"]["glue_job_runs"] = [{"job": f"job{i}", "run_id": f"r{i}", "state": "SUCCEEDED", "started": "2026-10-20T03:00:00Z",
                                   "duration_s": 60, "dpu_seconds": 10, "error": None} for i in range(60)]
    return s


def test_long_page_splits_into_an_index_and_one_page_per_service():
    s = _big()
    assert render(s).count("\n") > 300
    pages = render_pages(s)
    assert sorted(pages) == ["aws_main.md", "aws_main/athena.md", "aws_main/cloudwatch.md", "aws_main/dms.md",
                             "aws_main/firehose.md", "aws_main/glue.md", "aws_main/s3.md"]
    idx = pages["aws_main.md"]
    assert idx.count("\n") < 80 and "[Glue](aws_main/glue.md)" in idx and "## Signals" in idx
    assert "cloudwatch alarm dms-cdc-latency-high" in idx  # signals stay on the index
    assert "service: glue" in pages["aws_main/glue.md"] and "[aws_main](../aws_main.md)" in pages["aws_main/glue.md"]
    assert pages["aws_main/glue.md"].count("\n") > 150
    # small pages stay one file
    assert list(render_pages(snap())) == ["aws_main.md"]
    _golden("aws_split_index.md", idx)


def test_snapshot_writes_split_pages_then_cleans_up_when_the_page_shrinks(tmp_path):
    from de_agent.snapshot import _write_pages

    plat = tmp_path / "platform"
    plat.mkdir()
    _write_pages(plat, "aws_main", render_pages(_big()))
    assert (plat / "aws_main" / "glue.md").is_file()
    _write_pages(plat, "aws_main", render_pages(snap()))
    assert (plat / "aws_main.md").is_file() and not (plat / "aws_main").exists()


# ---------------------------------------------------------------- lineage edges

def test_s3_node_normalization():
    assert s3_node("Acme-Lake", "bronze/App") == "s3://acme-lake/bronze/app/"
    assert s3_node("b", "") == "s3://b/"
    assert s3_node("b", "firehose/clicks/!{timestamp:yyyy/MM/dd}/") == "s3://b/firehose/clicks/"
    assert s3_node("b", "firehose/clicks-!{timestamp:yyyy}/") == "s3://b/firehose/"
    assert s3_node("b", "!{timestamp:yyyy}/") == "s3://b/"
    assert s3_node("b", "/ab") == "s3://b/ab/"


def _aws_edges():
    return build_edges([], [{**snap(), "snapshot_id": "20261020T120000Z"}])["tables"]


def test_dms_explicit_rules_become_source_task_target_edges():
    t = _aws_edges()
    assert [e["id"] for e in t["app.orders"]["readers"]] == ["dms:acme-dwh-cdc", "dms:acme-orders-cdc"]
    assert all(e["via"] == "dms" and e["project"] == "aws_main" and not e.get("pattern") for e in t["app.orders"]["readers"])
    # schema rename app -> raw applies to the Redshift target only
    assert [e["id"] for e in t["raw.orders"]["writers"]] == ["dms:acme-dwh-cdc"]
    assert [e["id"] for e in t["raw.events"]["writers"]] == ["dms:acme-dwh-cdc"]
    # the S3 target is an s3:// node under the endpoint's bucket/folder/schema/table
    assert [e["id"] for e in t["s3://acme-lake/bronze/app/orders/"]["writers"]] == ["dms:acme-orders-cdc"]


def test_wildcard_rules_stay_patterns_and_are_never_expanded():
    t = _aws_edges()
    assert "ref.%" in t and "raw_ref.%" in t
    assert t["ref.%"]["readers"][0]["pattern"] is True and t["raw_ref.%"]["writers"][0]["pattern"] is True
    assert not any(k.startswith(("ref.", "raw_ref.")) and k not in ("ref.%", "raw_ref.%") for k in t)  # nothing guessed


def test_firehose_writes_a_prefix_and_glue_tables_become_readers_by_prefix():
    t = _aws_edges()
    assert [e["id"] for e in t["s3://acme-raw/firehose/clicks/"]["writers"]] == ["firehose:acme-clicks"]
    assert [e["id"] for e in t["s3://acme-raw/firehose/clicks/"]["readers"]] == ["glue:raw_clicks.clicks"]
    assert [e["id"] for e in t["s3://acme-lake/bronze/app/orders/"]["readers"]] == ["glue:bronze.orders"]
    assert [e["id"] for e in t["s3://acme-lake/bronze/app/customers/"]["readers"]] == ["glue:bronze.customers"]
    assert not any("unrelated" in str(v) for v in t.values())  # a table nobody writes into gets no edge
    # the catalog table is itself a node others can read
    assert [e["id"] for e in t["bronze.orders"]["writers"]] == ["glue:bronze.orders"]


def test_impact_walks_dms_to_s3_to_glue():
    edges = build_edges([], [{**snap(), "snapshot_id": "20261020T120000Z"}])
    rows = downstream(edges, "app.orders", 5)
    by_reader = {r["reader"]: r for r in rows}
    assert by_reader["dms:acme-orders-cdc"]["depth"] == 1
    assert by_reader["glue:bronze.orders"]["depth"] == 2
    assert by_reader["glue:bronze.orders"]["path"] == ["app.orders", "[dms:acme-orders-cdc]", "s3://acme-lake/bronze/app/orders/",
                                                       "[glue:bronze.orders]"]


def test_dms_edges_are_table_pairs_with_the_task_as_attribute():
    t = _aws_edges()
    (rd,) = [e for e in t["app.orders"]["readers"] if e["task"] == "acme-orders-cdc"]
    assert rd["target"] == "s3://acme-lake/bronze/app/orders/" and rd["via"] == "dms"
    (w,) = t["s3://acme-lake/bronze/app/customers/"]["writers"]
    assert w["source"] == "app.customers" and w["task"] == "acme-orders-cdc"
    edges = build_edges([], [{**snap(), "snapshot_id": "x"}])
    assert not any("customers" in r["reader"] for r in downstream(edges, "app.orders", 5))
    assert any(r["reader"] == "glue:bronze.customers" for r in downstream(edges, "app.customers", 5))


def test_unsupported_transformations_are_reported_not_guessed():
    s = snap()
    s["data"]["dms_tasks"][1]["rules"].append(
        {"type": "transformation", "action": "add-prefix", "target": "table", "schema": "app", "table": "orders", "value": "x_"})
    e = build_edges([], [{**s, "snapshot_id": "x"}])
    assert {"kind": "dms-transform", "name": "add-prefix on app.orders", "referenced_by": "dms:acme-dwh-cdc"} in e["unresolved"]
    assert "raw.orders" in e["tables"]  # the unmodified name is still what the edge says


def test_table_rename_transformation_is_applied():
    s = snap()
    s["data"]["dms_tasks"][1]["rules"].append(
        {"type": "transformation", "action": "rename", "target": "table", "schema": "app", "table": "orders", "value": "orders_v2"})
    t = build_edges([], [{**s, "snapshot_id": "x"}])["tables"]
    assert "raw.orders_v2" in t and "raw.orders" not in t


def test_missing_target_endpoint_is_unresolved():
    s = snap()
    s["data"]["dms_tasks"][0]["target"] = None
    e = build_edges([], [{**s, "snapshot_id": "x"}])
    assert any(u["kind"] == "dms-target" and u["name"] == "acme-orders-cdc" for u in e["unresolved"])


# ---------------------------------------------------------------- end to end through dea scan / dea edges / dea impact

@pytest.fixture
def aws_hub(scan_hub):
    cfg = (scan_hub / "de-agent.yaml").read_text()
    (scan_hub / "de-agent.yaml").write_text(
        cfg.replace("version: 1\n", "version: 1\nsources:\n  - {name: aws_main, type: aws, aws: {profile: readonly, region: us-east-1}, "
                                    "services: {dms: {}, glue: {catalog_databases: [bronze]}}}\n", 1))
    d = scan_hub / ".dea" / "snapshots" / "20261020T120000Z"
    d.mkdir(parents=True)
    (d / "aws_main.json").write_text((FIX / "aws_snapshot.json").read_text())
    return scan_hub


def test_scan_reads_the_latest_platform_snapshot_and_joins_dbt(aws_hub):
    out = run_scan(aws_hub, load_config(aws_hub / "de-agent.yaml"))
    assert not out.failed, out.errors
    edges = json.loads((aws_hub / "catalog" / "edges.json").read_text())
    t = edges["tables"]
    assert "dms:acme-dwh-cdc" in [e["id"] for e in t["raw.orders"]["writers"]]  # next to the fixture DAGs that also load it
    assert "dbt:stg_orders" in [e["id"] for e in t["raw.orders"]["readers"]]  # DMS -> Redshift table -> dbt model
    cross = {(c["table"], c["writer"], c["reader"]) for c in edges["cross_project"]}
    assert ("raw.orders", "dms:acme-dwh-cdc", "dbt:stg_orders") in cross
    assert edges["stats"]["aws.dms_tasks"] == 2 and edges["stats"]["aws.firehose_streams"] == 1
    md = (aws_hub / "catalog" / "edges.md").read_text()
    assert "s3://acme-lake/bronze/app/orders/ ← dms:acme-orders-cdc | → glue:bronze.orders" in md
    assert "ref.% (pattern) ← — | → dms:acme-dwh-cdc" in md
    assert "aws_main@20261020T120000Z" in md.splitlines()[1] or "aws_main@20261020T120000Z" in md[:400]


def test_dea_edges_and_dea_impact_commands(aws_hub, monkeypatch):
    from de_agent.cli import app
    from typer.testing import CliRunner

    run_scan(aws_hub, load_config(aws_hub / "de-agent.yaml"))
    monkeypatch.setenv("DEA_HUB", str(aws_hub))
    r = CliRunner().invoke(app, ["edges", "raw.orders"])
    assert r.exit_code == 0 and "dms:acme-dwh-cdc [aws_main] via dms" in r.output and "dbt:stg_orders [analytics]" in r.output
    r = CliRunner().invoke(app, ["edges", "ref.%"])
    assert r.exit_code == 0 and "via dms, pattern" in r.output
    r = CliRunner().invoke(app, ["edges", "s3://acme-lake/bronze/app/orders/"])
    assert r.exit_code == 0 and "glue:bronze.orders" in r.output and "dms:acme-orders-cdc" in r.output
    r = CliRunner().invoke(app, ["impact", "app.orders"])
    assert r.exit_code == 0
    assert "depth 1  dms:acme-orders-cdc [aws_main]" in r.output
    assert "depth 2  glue:bronze.orders [aws_main]" in r.output
    assert "depth 2  dbt:stg_orders [analytics]" in r.output
    assert "depth 3  dbt:fct_orders [analytics]" in r.output
    assert "glue:bronze.customers" not in r.output and "bronze/app/customers" not in r.output  # table-pair edges: orders never reaches glue:bronze.customers
    r = CliRunner().invoke(app, ["edges", "raw.orders"])
    assert "dms:acme-dwh-cdc [aws_main] via dms" in r.output


def test_a_failed_snapshot_is_not_used_for_edges(aws_hub):
    d = aws_hub / ".dea" / "snapshots" / "20261021T000000Z"
    d.mkdir()
    (d / "aws_main.json").write_text(json.dumps({"source": "aws_main", "type": "aws", "schema_version": 1, "collected_at": "x",
                                                  "duration_s": 1, "data": {}, "errors": ["boom"]}))
    run_scan(aws_hub, load_config(aws_hub / "de-agent.yaml"))
    edges = json.loads((aws_hub / "catalog" / "edges.json").read_text())
    assert "raw.orders" in edges["tables"]  # still built from the older good snapshot


@pytest.mark.parametrize("service", ["glue", "s3", "athena", "firehose", "cloudwatch"])
def test_each_service_section_has_a_golden(service):
    md = render(snap())
    heads = {"glue": ("## Glue", "## S3"), "s3": ("## S3", "## Athena"), "athena": ("## Athena", "## Firehose"),
             "firehose": ("## Firehose", "## CloudWatch"), "cloudwatch": ("## CloudWatch", "## Collection")}[service]
    _golden(f"aws_{service}_section.md", md[md.index(heads[0]):md.index(heads[1])])


def test_split_pages_pass_lint_and_are_findable(example_hub):
    from de_agent.find import find
    from de_agent.lint import run_lint
    from de_agent.snapshot import _write_pages

    plat = example_hub / "platform"
    plat.mkdir(exist_ok=True)
    _write_pages(plat, "aws_main", render_pages(_big()))
    assert (plat / "aws_main" / "dms.md").is_file()
    assert [f for f in run_lint(example_hub) if f.level == "error"] == []
    hits = find(example_hub, "t042", 5, True)
    assert any(h.path.startswith("platform/aws_main/") for h in hits), [h.path for h in hits]
