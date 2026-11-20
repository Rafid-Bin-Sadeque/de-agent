"""v0.3.2: dms_table_mappings section (task -> table list), its diff signal, render lines and the cap."""
import copy

from aws_fixtures import (
    RS_RULES,
    S3_RULES,
    TASK_RS_ARN,
    TASK_S3_ARN,
    TGT_RS_ARN,
    TGT_S3_ARN,
    build_dms_session,
    dms_task,
    guarded_session,
    table_stat,
    utcnow,
)
from de_agent.collectors.aws import _aws_core as core
from de_agent.diff import diff_results
from de_agent.render import aws_service_blocks, signals_for


def _sel(rid, schema, table, action="include"):
    return {"rule-type": "selection", "rule-id": rid, "rule-name": rid, "rule-action": action,
            "object-locator": {"schema-name": schema, "table-name": table}}


def _tr(rid, target, action, schema, table=None, value=None):
    loc = {"schema-name": schema, **({"table-name": table} if table is not None else {})}
    r = {"rule-type": "transformation", "rule-id": rid, "rule-name": rid, "rule-action": action, "rule-target": target,
         "object-locator": loc}
    if value is not None:
        r["value"] = value
    return r


def _rows(rules, stats):
    return core.dms_mapping_rows("t1", core._rules({"rules": rules}), stats, "src", "tgt")


def test_explicit_wildcard_exclude_rename_unresolved():
    rules = [_sel("1", "app", "orders"), _sel("2", "app", "%"), _sel("3", "app", "tmp_%", "exclude"),
             _sel("4", "ghost", "%"), _tr("5", "schema", "rename", "app", value="raw"),
             _tr("6", "table", "rename", "app", "orders", value="orders_v2")]
    stats = [table_stat("app", "orders"), table_stat("app", "events"), table_stat("app", "tmp_x"), table_stat("APP", "x")]
    got = {(r["schema"], r["table"]): (r["target_schema"], r["target_table"], r["rule"]) for r in _rows(rules, stats)}
    assert got == {
        ("app", "orders"): ("raw", "orders_v2", "explicit"),  # explicit wins over the wildcard that also matches
        ("app", "events"): ("raw", "events", "wildcard app.%"),
        ("ghost", "%"): ("ghost", "%", "wildcard ghost.% (unresolved)"),
    }  # tmp_x excluded; APP.x not matched (case-sensitive)


def test_case_and_prefix_transformations():
    rules = [_sel("1", "App", "Orders"), _tr("2", "schema", "convert-lowercase", "%"),
             _tr("3", "table", "add-prefix", "App", "%", value="src_"), _tr("4", "table", "convert-lowercase", "%", "%")]
    (r,) = _rows(rules, [])
    assert (r["target_schema"], r["target_table"]) == ("app", "src_orders")
    rules = [_sel("1", "s", "stg_a"), _tr("2", "table", "remove-prefix", "s", "%", value="stg_"),
             _tr("3", "table", "add-suffix", "s", "%", value="_raw")]
    (r,) = _rows(rules, [])
    assert r["target_table"] == "a_raw"


def _world(tasks=None, stats=None, stats_error=False, options=None):
    now = utcnow()
    tasks = tasks or [dms_task("acme-orders-cdc", TASK_S3_ARN, TGT_S3_ARN, S3_RULES),
                      dms_task("acme-dwh-cdc", TASK_RS_ARN, TGT_RS_ARN, RS_RULES)]
    stats = stats if stats is not None else {
        TASK_S3_ARN: [table_stat("app", "orders"), table_stat("app", "customers")],
        TASK_RS_ARN: [table_stat("app", "orders"), table_stat("app", "audit"), table_stat("app", "events")]}
    session, stub = build_dms_session(guarded_session(), tasks, stats, now=now)
    if stats_error:  # replace the RS task's queued table statistics with an AccessDenied
        stub._queue.pop()
        stub.add_client_error("describe_table_statistics", service_error_code="AccessDeniedFault",
                              service_message="denied")
    res = core.gather({"services": {"dms": {}}, "options": options or {}}, session, now=now)
    return {"source": "aws_main", "type": "aws", "schema_version": 1, "collected_at": res["data"]["meta"]["collected_at"],
            "duration_s": 0.1, **res}


def test_gather_section_rows_and_render():
    r = _world()
    m = r["data"]["dms_table_mappings"]
    assert [(x["task"], x["schema"], x["table"], x["target_schema"], x["rule"]) for x in m] == [
        ("acme-dwh-cdc", "app", "audit", "raw", "wildcard app.%"),
        ("acme-dwh-cdc", "app", "events", "raw", "wildcard app.%"),
        ("acme-dwh-cdc", "app", "orders", "raw", "explicit"),
        ("acme-orders-cdc", "app", "customers", "app", "explicit"),
        ("acme-orders-cdc", "app", "orders", "app", "explicit"),
    ]
    assert {(x["source_endpoint"], x["target_endpoint"]) for x in m} == {("acme-src", "acme-rs"), ("acme-src", "acme-s3")}
    assert "dms_table_mappings" not in r["data"]["meta"]["truncated"]
    block = "\n".join(aws_service_blocks(r["data"])["dms"])
    assert "- acme-dwh-cdc: 3 tables (2 via wildcard)" in block and "- acme-orders-cdc: 2 tables (0 via wildcard)" in block
    assert not [s for s in signals_for(r) if "is written by" in s]


def test_two_tasks_writing_one_target_table_warns():
    tasks = [dms_task("a-cdc", TASK_S3_ARN, TGT_RS_ARN, S3_RULES), dms_task("b-cdc", TASK_RS_ARN, TGT_RS_ARN, S3_RULES)]
    r = _world(tasks=tasks, stats={})
    sig = [s for s in signals_for(r) if "is written by" in s]
    assert sig == ["dms target acme-rs.app.customers is written by 2 tasks (a-cdc, b-cdc)",
                   "dms target acme-rs.app.orders is written by 2 tasks (a-cdc, b-cdc)"]


def test_cap_truncates_and_is_listed():
    r = _world(options={"dms_mappings_max": 2})
    assert len(r["data"]["dms_table_mappings"]) == 2
    assert "dms_table_mappings" in r["data"]["meta"]["truncated"]


def test_diff_added_and_removed_tables_signal():
    old = _world()
    new = copy.deepcopy(old)
    rows = new["data"]["dms_table_mappings"]
    rows[:] = [x for x in rows if x["table"] != "audit"]
    rows.append(dict(rows[0], table="payments", rule="explicit"))
    d = diff_results(old, new)
    lst = d["lists"]["dms_table_mappings"]
    assert [a["key"] for a in lst["added"]] and [x["key"] for x in lst["removed"]]
    assert "DMS mappings: +1 / -1 table(s) in acme-dwh-cdc" in d["signals_new"]


def test_diff_renamed_target_is_a_changed_row():
    old = _world()
    new = copy.deepcopy(old)
    new["data"]["dms_table_mappings"][0]["target_schema"] = "bronze"
    d = diff_results(old, new)
    lst = d["lists"]["dms_table_mappings"]
    assert not lst["added"] and not lst["removed"] and lst["changed"][0]["changes"][0]["field"] == "target_schema"
    assert not [s for s in d["signals_new"] if s.startswith("DMS mappings")]


def test_degraded_statistics_do_not_report_removed_tables():
    old = _world()
    new = _world(stats_error=True)
    assert any("dms.table_statistics acme-dwh-cdc" in e for e in new["errors"])
    assert "dms_table_mappings" in new["data"]["meta"]["truncated"]
    d = diff_results(old, new)
    assert not (d["lists"].get("dms_table_mappings") or {}).get("removed")
    assert not [s for s in d["signals_new"] if s.startswith("DMS mappings")]

