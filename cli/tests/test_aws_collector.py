"""AWS collector against moto (S3, Glue, Firehose, CloudWatch) and botocore Stubber (DMS, Athena BatchGetQueryExecution)."""
import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import boto3
import pytest
from aws_fixtures import (
    REGION,
    RS_RULES,
    S3_RULES,
    TASK_RS_ARN,
    TASK_S3_ARN,
    TGT_RS_ARN,
    TGT_S3_ARN,
    FakeCloudWatch,
    SeenCalls,
    StubSession,
    build_dms_session,
    dms_instance,
    dms_task,
    guarded_session,
    stubbed,
    table_stat,
    utcnow,
)
from de_agent.collectors import aws as awsc
from de_agent.collectors.aws import _aws_core as core
from de_agent.collectors.base import CollectorError, RunContext
from de_agent.config import ConfigError, parse_config
from de_agent.render import render, signals_for
from de_agent.runner import result_status, run_source
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[2]


def _source(extra="", services="{dms: {}}", options="{}", exec_="local", aws="{profile: readonly, region: us-east-1}"):
    return parse_config(
        f"sources:\n  - name: aws_main\n    type: aws\n    exec: {exec_}\n    aws: {aws}\n    services: {services}\n"
        f"    options: {options}\n{extra}").sources[0]


def _result(res, now=None):
    return {"source": "aws_main", "type": "aws", "schema_version": 1, "collected_at": res["data"]["meta"]["collected_at"],
            "duration_s": 0.1, **res}


# ---------------------------------------------------------------- config

def test_config_validation():
    assert _source().type == "aws"
    for bad, msg in [
        ("services: {}", "need a `services:` map"),
        ("services: {ec2: {}}", "unknown aws services"),
        ("services: {s3: {prefixes: ['acme-lake/bronze']}}", "must start with s3://"),
        ("services: {glue: {jobs: 'x'}}", "must be a list of strings"),
        ("services: {dms: {bogus: 1}}", "unknown services.dms keys"),
    ]:
        with pytest.raises(ConfigError, match=msg):
            parse_config("sources:\n  - {name: a, type: aws, aws: {region: us-east-1}, " + bad + "}\n")
    with pytest.raises(ConfigError, match="instance_role: true needs exec ssh"):
        _source(aws="{instance_role: true}")
    assert _source(aws="{instance_role: true}", exec_="ssh:bastion").exec == "ssh:bastion"
    with pytest.raises(ConfigError, match="mutually exclusive"):
        _source(aws="{instance_role: true, profile: x}", exec_="ssh:bastion")
    with pytest.raises(ConfigError, match="need an `aws:` block"):
        parse_config("sources:\n  - {name: a, type: aws, services: {dms: {}}}\n")
    with pytest.raises(ConfigError, match="unknown aws options"):
        _source(options="{bogus: 1}")


# ---------------------------------------------------------------- S3 (moto)

@mock_aws
def test_s3_prefixes_only_and_cloudwatch_for_whole_buckets():
    s3 = boto3.client("s3", region_name=REGION)
    for b in ("acme-lake", "acme-raw", "acme-other"):
        s3.create_bucket(Bucket=b)
    s3.put_object(Bucket="acme-lake", Key="bronze/app/orders/a.parquet", Body=b"x" * 100)
    s3.put_object(Bucket="acme-lake", Key="silver/b.parquet", Body=b"x" * 10)
    s3.put_object(Bucket="acme-raw", Key="firehose/2026/10/20/c.gz", Body=b"y" * 50)
    s3.put_object(Bucket="acme-other", Key="secret/never-listed.txt", Body=b"z")
    cw = boto3.client("cloudwatch", region_name=REGION)
    now = utcnow()
    cw.put_metric_data(Namespace="AWS/S3", MetricData=[
        {"MetricName": "BucketSizeBytes", "Dimensions": [{"Name": "BucketName", "Value": "acme-raw"},
                                                         {"Name": "StorageType", "Value": "StandardStorage"}],
         "Timestamp": now - timedelta(hours=30), "Value": 9000.0},
        {"MetricName": "NumberOfObjects", "Dimensions": [{"Name": "BucketName", "Value": "acme-raw"},
                                                        {"Name": "StorageType", "Value": "AllStorageTypes"}],
         "Timestamp": now - timedelta(hours=30), "Value": 77.0}])
    seen = SeenCalls()
    session = guarded_session(seen=seen)
    cfg = {"services": {"s3": {"prefixes": ["s3://acme-lake/bronze/", "s3://acme-raw/"]}}, "options": {}}
    # the S3 daily metrics need a wide window: whole-bucket stats are daily
    res = core.gather(cfg, session, now=now + timedelta(hours=1))
    rows = {r["prefix"]: r for r in res["data"]["s3_prefixes"]}
    assert rows["s3://acme-lake/bronze/"]["object_count"] == 1 and rows["s3://acme-lake/bronze/"]["bytes"] == 100
    assert rows["s3://acme-lake/bronze/"]["source"] == "list" and rows["s3://acme-lake/bronze/"]["newest_complete"]
    assert rows["s3://acme-raw/"]["source"] == "cloudwatch" and rows["s3://acme-raw/"]["object_count"] == 77
    assert res["errors"] == []
    # only the named prefixes were listed; never an account-wide listing
    assert ("s3", "ListBuckets") not in seen.ops and seen.ops.count(("s3", "ListObjectsV2")) == 2


@mock_aws
def test_s3_listing_is_capped_and_marked_incomplete(monkeypatch):
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket="acme-lake")
    for i in range(1100):
        s3.put_object(Bucket="acme-lake", Key=f"bronze/k{i:05d}", Body=b"")
    monkeypatch.setattr(core, "MAX_PAGES", 1)
    res = core.gather({"services": {"s3": {"prefixes": ["s3://acme-lake/bronze/"]}}}, guarded_session())
    row = res["data"]["s3_prefixes"][0]
    assert row["object_count"] == 1000 and row["newest_complete"] is False
    assert not any("newest object" in s for s in signals_for(_result(res)))  # a sample proves nothing about freshness


@mock_aws
def test_s3_errors_are_per_prefix():
    res = core.gather({"services": {"s3": {"prefixes": ["s3://acme-missing/x/", "not-a-prefix"]}}}, guarded_session())
    assert any("s3.list s3://acme-missing/x/" in e and "NoSuchBucket" in e for e in res["errors"])
    assert any("not an s3:// prefix" in e for e in res["errors"])
    assert res["data"]["s3_prefixes"][0]["source"] == "error"


# ---------------------------------------------------------------- Glue (moto)

@mock_aws
def test_glue_jobs_crawlers_catalog():
    g = boto3.client("glue", region_name=REGION)
    g.create_database(DatabaseInput={"Name": "bronze"})
    g.create_database(DatabaseInput={"Name": "other"})
    g.create_table(DatabaseName="bronze", TableInput={
        "Name": "orders", "StorageDescriptor": {"Location": "s3://acme-lake/bronze/app/orders/", "Columns": []},
        "PartitionKeys": [{"Name": "dt", "Type": "string"}], "Parameters": {"classification": "parquet"}})
    g.create_table(DatabaseName="bronze", TableInput={
        "Name": "dim_region", "StorageDescriptor": {"Location": "s3://acme-lake/bronze/dim_region/", "Columns": []}})
    g.create_table(DatabaseName="other", TableInput={"Name": "ignored", "StorageDescriptor": {"Location": "s3://x/y/"}})
    g.create_partition(DatabaseName="bronze", TableName="orders", PartitionInput={
        "Values": ["2026-10-26"], "StorageDescriptor": {"Location": "s3://acme-lake/bronze/app/orders/dt=2026-10-26/"}})
    for job in ("load_orders", "load_customers", "adhoc_tmp"):
        g.create_job(Name=job, Role="arn:aws:iam::123456789012:role/etl", Command={"Name": "glueetl", "ScriptLocation": "s3://x/y.py"})
    for _ in range(7):
        g.start_job_run(JobName="load_orders")
    g.start_job_run(JobName="load_customers")
    g.create_crawler(Name="bronze_crawler", Role="arn:aws:iam::123456789012:role/etl", DatabaseName="bronze",
                     Targets={"S3Targets": [{"Path": "s3://acme-lake/bronze/"}]})
    res = core.gather({"services": {"glue": {"jobs": ["load_*"], "crawlers": True, "catalog_databases": ["bronze"]}}},
                      guarded_session())
    assert res["errors"] == []
    d = res["data"]
    jobs = {}
    for r in d["glue_job_runs"]:
        jobs.setdefault(r["job"], []).append(r)
    assert set(jobs) == {"load_orders", "load_customers"}  # the glob keeps adhoc_tmp out
    assert len(jobs["load_orders"]) == 5  # the last 5 runs only
    assert jobs["load_orders"][0]["state"] == "SUCCEEDED" and jobs["load_orders"][0]["duration_s"] == 123
    assert d["glue_crawlers"][0]["name"] == "bronze_crawler" and d["glue_crawlers"][0]["state"] == "READY"
    tabs = {t["name"]: t for t in d["glue_tables"]}
    assert set(tabs) == {"orders", "dim_region"}  # only configured databases
    assert tabs["orders"]["location"] == "s3://acme-lake/bronze/app/orders/" and tabs["orders"]["partition_keys"] == ["dt"]
    assert tabs["orders"]["partitions"] == 1 and tabs["orders"]["last_partition_added"]
    assert "partitions" not in tabs["dim_region"]


@mock_aws
def test_glue_partition_signal_when_stale():
    g = boto3.client("glue", region_name=REGION)
    g.create_database(DatabaseInput={"Name": "bronze"})
    g.create_table(DatabaseName="bronze", TableInput={
        "Name": "orders", "StorageDescriptor": {"Location": "s3://acme-lake/bronze/orders/"},
        "PartitionKeys": [{"Name": "dt", "Type": "string"}]})
    g.create_partition(DatabaseName="bronze", TableName="orders", PartitionInput={
        "Values": ["2026-10-01"], "StorageDescriptor": {"Location": "s3://acme-lake/bronze/orders/dt=2026-10-01/"}})
    cfg = {"services": {"glue": {"catalog_databases": ["bronze"], "jobs": []}}}
    fresh = core.gather(cfg, guarded_session())
    assert not any("no partition added" in s for s in signals_for(_result(fresh)))
    stale = core.gather(cfg, guarded_session(), now=utcnow() + timedelta(days=8))
    assert any("glue table bronze.orders: no partition added in 7 d" in s for s in signals_for(_result(stale)))


@mock_aws
def test_glue_partition_scan_is_capped(monkeypatch):
    g = boto3.client("glue", region_name=REGION)
    g.create_database(DatabaseInput={"Name": "bronze"})
    g.create_table(DatabaseName="bronze", TableInput={"Name": "t", "StorageDescriptor": {"Location": "s3://b/t/"},
                                                      "PartitionKeys": [{"Name": "dt", "Type": "string"}]})
    for i in range(5):
        g.create_partition(DatabaseName="bronze", TableName="t", PartitionInput={
            "Values": [f"d{i}"], "StorageDescriptor": {"Location": f"s3://b/t/d{i}/"}})
    monkeypatch.setattr(core, "MAX_PARTITIONS", 3)
    res = core.gather({"services": {"glue": {"catalog_databases": ["bronze"], "jobs": []}}}, guarded_session(),
                      now=utcnow() + timedelta(days=30))
    t = res["data"]["glue_tables"][0]
    assert t["partitions"] == 3 and t["partitions_truncated"] is True
    assert not any("no partition added" in s for s in signals_for(_result(res)))  # capped scans do not raise it


# ---------------------------------------------------------------- Firehose + CloudWatch alarms (moto)

@mock_aws
def test_firehose_streams_and_metrics():
    f = boto3.client("firehose", region_name=REGION)
    f.create_delivery_stream(DeliveryStreamName="acme-orders", ExtendedS3DestinationConfiguration={
        "RoleARN": "arn:aws:iam::123456789012:role/fh", "BucketARN": "arn:aws:s3:::acme-raw",
        "Prefix": "firehose/orders/", "BufferingHints": {"SizeInMBs": 5, "IntervalInSeconds": 300}})
    base = guarded_session()
    cw = FakeCloudWatch({("DeliveryToS3.Success", "Average", "acme-orders"): [1.0, 0.9],
                         ("DeliveryToS3.DataFreshness", "p95", "acme-orders"): [200.0, 800.0],
                         ("ThrottledRecords", "Sum", "acme-orders"): [0.0, 5.0]})
    res = core.gather({"services": {"firehose": {}}}, StubSession(base, {"cloudwatch": cw}))
    row = res["data"]["firehose_streams"][0]
    assert row["status"] == "ACTIVE" and row["bucket"] == "acme-raw" and row["prefix"] == "firehose/orders/"
    assert row["buffer_interval_s"] == 300 and row["delivery_success"] == 0.9
    assert row["freshness_p95_s"] == 800.0 and row["throttled_records"] == 5
    sig = signals_for(_result(res))
    assert any("data freshness p95 800 s (> 2x the 300 s buffer)" in s for s in sig)
    assert any("delivery success 0.90 (< 1.0)" in s for s in sig)


@mock_aws
def test_cloudwatch_alarms_in_alarm_state():
    cw = boto3.client("cloudwatch", region_name=REGION)
    for name, state in (("dms-latency-high", "ALARM"), ("redshift-disk", "OK")):
        cw.put_metric_alarm(AlarmName=name, MetricName="M", Namespace="Acme/Test", Statistic="Average", Period=60,
                            EvaluationPeriods=1, Threshold=1.0, ComparisonOperator="GreaterThanThreshold")
        cw.set_alarm_state(AlarmName=name, StateValue=state, StateReason="threshold crossed")
    res = core.gather({"services": {"cloudwatch": {"alarms": True}}}, guarded_session())
    assert [a["name"] for a in res["data"]["cloudwatch_alarms"]] == ["dms-latency-high"]
    assert any(s.startswith("cloudwatch alarm dms-latency-high in ALARM since") for s in signals_for(_result(res)))


# ---------------------------------------------------------------- Athena (Stubber: moto has no BatchGetQueryExecution)

def _athena_query(qid, state, scanned, submitted, text="select 1", reason=None):
    st = {"State": state, "SubmissionDateTime": submitted}
    if reason:
        st["StateChangeReason"] = reason
    return {"QueryExecutionId": qid, "Query": text, "Status": st, "Statistics": {"DataScannedInBytes": scanned},
            "WorkGroup": "primary"}


def test_athena_window_top_queries_and_failures():
    base = guarded_session()
    athena, stub = stubbed(base, "athena")
    now = utcnow()
    qs = [_athena_query("q1", "SUCCEEDED", 5 * 1024 ** 3, now - timedelta(hours=1), "select  *\nfrom acme.orders"),
          _athena_query("q2", "FAILED", 0, now - timedelta(hours=2), "select x from nope", "TABLE_NOT_FOUND: no such table"),
          _athena_query("q3", "SUCCEEDED", 200 * 1024 ** 3, now - timedelta(hours=3)),
          _athena_query("q4", "SUCCEEDED", 1, now - timedelta(hours=30))]  # outside the 24 h window
    stub.add_response("list_query_executions", {"QueryExecutionIds": ["q1", "q2", "q3", "q4"]}, {"WorkGroup": "primary"})
    stub.add_response("batch_get_query_execution", {"QueryExecutions": qs, "UnprocessedQueryExecutionIds": []},
                      {"QueryExecutionIds": ["q1", "q2", "q3", "q4"]})
    stub.activate()
    res = core.gather({"services": {"athena": {"workgroups": ["primary"]}}, "options": {"max_scan_gb": 100}},
                      StubSession(base, {"athena": athena}), now=now)
    stub.assert_no_pending_responses()
    d = res["data"]
    wg = d["athena_workgroups"][0]
    assert (wg["queries"], wg["succeeded"], wg["failed"]) == (3, 2, 1)
    assert wg["scanned_bytes"] == 205 * 1024 ** 3
    assert [q["query_id"] for q in d["athena_top_queries"]][:2] == ["q3", "q1"]
    assert d["athena_top_queries"][1]["query"] == "select * from acme.orders"  # whitespace collapsed
    assert d["athena_failures"][0]["reason"].startswith("TABLE_NOT_FOUND")
    sig = signals_for(_result(res))
    assert any("athena workgroup primary: 1 failed query(ies) in 24h (TABLE_NOT_FOUND" in s for s in sig)
    assert any("athena query q3 scanned 200.0 GB (> 100 GB)" in s for s in sig)


def test_athena_workgroup_error_is_isolated():
    base = guarded_session()
    athena, stub = stubbed(base, "athena")
    stub.add_client_error("list_query_executions", "AccessDeniedException",
                          "User: arn:aws:sts::123456789012:assumed-role/ro/x is not authorized")
    stub.activate()
    res = core.gather({"services": {"athena": {"workgroups": ["primary"]}}}, StubSession(base, {"athena": athena}))
    assert len(res["errors"]) == 1 and "athena.primary: AccessDeniedException" in res["errors"][0]
    assert "123456789012" not in res["errors"][0] and "****9012" in res["errors"][0]  # account id masked


# ---------------------------------------------------------------- DMS (Stubber + series)

def _dms_world(now, series=None, tasks=None, stats=None, **kw):
    base = guarded_session()
    tasks = tasks or [dms_task("acme-orders-cdc", TASK_S3_ARN, TGT_S3_ARN, S3_RULES),
                      dms_task("acme-dwh-cdc", TASK_RS_ARN, TGT_RS_ARN, RS_RULES)]
    stats = stats if stats is not None else {
        TASK_S3_ARN: [table_stat("app", "orders", ins=100, upd=50), table_stat("app", "customers", ins=10)],
        TASK_RS_ARN: [table_stat("app", "orders", ins=900, upd=100, dele=5), table_stat("app", "audit", "Table error"),
                      table_stat("app", "events", "Table suspended"), table_stat("app", "tiny", ins=1)]}
    session, stub = build_dms_session(base, tasks, stats, series=series, now=now, **kw)
    return session, stub, base


def test_dms_instances_tasks_and_table_statistics():
    now = utcnow()
    series = {
        ("CPUUtilization", "Average", "acme-dms-1"): [20.0, 30.0], ("CPUUtilization", "Maximum", "acme-dms-1"): [60.0, 99.0],
        ("CPUUtilization", "p95", "acme-dms-1"): [40.0, 90.0], ("FreeableMemory", "Minimum", "acme-dms-1"): [8 * 1024 ** 3, 1.0 * 1024 ** 3],
        ("SwapUsage", "Maximum", "acme-dms-1"): [0.0, 1024.0, 2048.0, 4096.0],
        ("CDCLatencySource", "p95", "TASKS3RES"): [10.0, 20.0], ("CDCLatencySource", "Maximum", "TASKS3RES"): [30.0],
        ("CDCLatencyTarget", "p95", "TASKRSRES"): [100.0, 450.0], ("CDCLatencyTarget", "Maximum", "TASKRSRES"): [900.0],
    }
    session, stub, _ = _dms_world(now, series)
    res = core.gather({"services": {"dms": {}}}, session, now=now)
    stub.assert_no_pending_responses()
    assert res["errors"] == []
    d = res["data"]
    inst = d["dms_instances"][0]
    assert inst["class"] == "dms.r5.large" and inst["memory_gb"] == 16.0 and inst["multi_az"] is False
    assert inst["cpu_avg"] == 25.0 and inst["cpu_max"] == 99.0 and inst["cpu_p95"] == 90.0
    assert inst["free_mem_min_pct"] == 6.25 and inst["swap_hours"] == 3 and inst["swap_max_bytes"] == 4096
    tasks = {t["id"]: t for t in d["dms_tasks"]}
    assert tasks["acme-dwh-cdc"]["cdc_latency_target_p95_s"] == 450.0 and tasks["acme-dwh-cdc"]["cdc_latency_target_max_s"] == 900.0
    assert tasks["acme-dwh-cdc"]["tables_suspended"] == 1 and tasks["acme-dwh-cdc"]["tables_total"] == 4
    assert tasks["acme-orders-cdc"]["target"] == {"engine": "s3", "bucket": "acme-lake", "folder": "bronze"}
    assert tasks["acme-dwh-cdc"]["target"] == {"engine": "redshift", "database": "dwh"}
    assert tasks["acme-orders-cdc"]["rules"][0] == {"type": "selection", "action": "include", "schema": "app", "table": "orders"}
    assert [(i["task"], i["table"], i["state"]) for i in d["dms_table_issues"]] == [
        ("acme-dwh-cdc", "audit", "Table error"), ("acme-dwh-cdc", "events", "Table suspended")]
    top = [t for t in d["dms_top_tables"] if t["task"] == "acme-dwh-cdc"]
    assert top[0]["table"] == "orders" and top[0]["cdc_changes"] == 1005 and len(top) == 4
    sig = signals_for(_result(res))
    assert any("dms task acme-dwh-cdc: CDC latency p95 450 s (> 300 s)" in s for s in sig)
    assert any("dms task acme-dwh-cdc: 0 table(s) errored, 1 suspended" in s or "table(s) errored" in s for s in sig)
    assert any("dms instance acme-dms-1: freeable memory fell to 6% (< 10%)" in s for s in sig)
    assert any("swap in use for 3 of the last 24 h" in s for s in sig)
    assert any("CPU p95 90% (> 85%)" in s for s in sig)
    assert not any("is stopped" in s or "failed in the last 24h" in s for s in sig)


def test_dms_task_not_running_and_recent_failure():
    now = utcnow()
    tasks = [dms_task("acme-orders-cdc", TASK_S3_ARN, TGT_S3_ARN, S3_RULES, Status="failed", StopReason="FAILURE",
                      LastFailureMessage="Last Error Stream Component Fatal error. Task 'X' was suspended",
                      ReplicationTaskStats={"TablesErrored": 2, "StopDate": now - timedelta(hours=3)}),
             dms_task("acme-dwh-cdc", TASK_RS_ARN, TGT_RS_ARN, RS_RULES, Status="stopped", StopReason="STOPPED_BY_USER",
                      LastFailureMessage="old failure", ReplicationTaskStats={"StopDate": now - timedelta(days=5)})]
    session, stub, _ = _dms_world(now, tasks=tasks, stats={})
    res = core.gather({"services": {"dms": {}}}, session, now=now)
    sig = signals_for(_result(res))
    assert "dms task acme-orders-cdc is failed (FAILURE)" in sig
    assert any(s.startswith("dms task acme-orders-cdc failed in the last 24h: Last Error Stream") for s in sig)
    assert "dms task acme-dwh-cdc is stopped (STOPPED_BY_USER)" in sig
    assert not any(s.startswith("dms task acme-dwh-cdc failed in the last 24h") for s in sig)  # 5 days ago
    assert any("dms task acme-orders-cdc: 2 table(s) errored" in s for s in sig)


def test_dms_instance_class_memory_table():
    assert core.instance_memory_gb("dms.r5.large") == 16.0
    assert core.instance_memory_gb("dms.r6i.2xlarge") == 64.0
    assert core.instance_memory_gb("dms.c5.xlarge") == 8.0
    assert core.instance_memory_gb("dms.t3.medium") == 4.0
    assert core.instance_memory_gb("dms.weird.thing") is None


def test_dms_mapping_rules_are_parsed():
    rules = core._rules(json.dumps(RS_RULES))
    assert {"type": "transformation", "action": "rename", "target": "schema", "schema": "app", "value": "raw"} in rules
    assert core._rules("not json") == []


def test_cost_guard_caps_metrics_and_batches_at_500():
    now = utcnow()
    insts = [dms_instance(ReplicationInstanceIdentifier=f"acme-dms-{i}",
                          ReplicationInstanceArn=f"arn:aws:dms:{REGION}:123456789012:rep:I{i}") for i in range(120)]
    base = guarded_session()
    session, stub = build_dms_session(base, [], {}, instances=insts, series={}, now=now)
    res = core.gather({"services": {"dms": {}}, "options": {"max_metrics": 1000}}, session, now=now)
    cw = session.overrides["cloudwatch"]
    assert res["data"]["meta"]["metrics_requested"] == 600  # 120 instances x 5 metrics
    assert cw.calls == 2 and cw.queries == 600  # batched: 500 + 100
    session, stub = build_dms_session(guarded_session(), [], {}, instances=insts, series={}, now=now)
    res = core.gather({"services": {"dms": {}}, "options": {"max_metrics": 50}}, session, now=now)
    meta = res["data"]["meta"]
    assert meta["metrics_requested"] == 50 and meta["metrics_skipped"] == 550 and meta["max_metrics"] == 50
    assert session.overrides["cloudwatch"].queries == 50
    md = render(_result(res))
    assert "50 of 50 allowed" in md and "metrics skipped by the cap | 550" in md


def test_cloudwatch_get_metric_data_request_shape():
    """The real request: validated against the service model by Stubber (period 3600 s, 24 h window, NextToken paging)."""
    base = guarded_session()
    cw, stub = stubbed(base, "cloudwatch")
    now = utcnow()
    stub.add_response("get_metric_data", {"MetricDataResults": [{"Id": "m0", "Timestamps": [now - timedelta(hours=2)], "Values": [1.0],
                                                                 "StatusCode": "PartialData"}], "NextToken": "t1"},
                      {"MetricDataQueries": [{"Id": "m0", "ReturnData": True, "MetricStat": {
                          "Metric": {"Namespace": "AWS/DMS", "MetricName": "CPUUtilization",
                                     "Dimensions": [{"Name": "ReplicationInstanceIdentifier", "Value": "acme-dms-1"}]},
                          "Period": 3600, "Stat": "p95"}}],
                       "StartTime": now - timedelta(hours=24), "EndTime": now, "ScanBy": "TimestampAscending"})
    stub.add_response("get_metric_data", {"MetricDataResults": [{"Id": "m0", "Timestamps": [now - timedelta(hours=1)], "Values": [2.0],
                                                                 "StatusCode": "Complete"}]},
                      {"MetricDataQueries": [{"Id": "m0", "ReturnData": True, "MetricStat": {
                          "Metric": {"Namespace": "AWS/DMS", "MetricName": "CPUUtilization",
                                     "Dimensions": [{"Name": "ReplicationInstanceIdentifier", "Value": "acme-dms-1"}]},
                          "Period": 3600, "Stat": "p95"}}],
                       "StartTime": now - timedelta(hours=24), "EndTime": now, "ScanBy": "TimestampAscending",
                       "NextToken": "t1"})
    stub.activate()
    m = core.Metrics(StubSession(base, {"cloudwatch": cw}), now, 24, 300)
    out = m.fetch([("k", "AWS/DMS", "CPUUtilization", {"ReplicationInstanceIdentifier": "acme-dms-1"}, "p95", 3600)])
    stub.assert_no_pending_responses()
    assert out == {"k": [1.0, 2.0]} and m.calls == 2


def test_service_failure_leaves_the_other_services():
    base = guarded_session()
    dms, stub = stubbed(base, "dms")
    stub.add_client_error("describe_replication_instances", "AccessDeniedException", "not authorized")
    stub.add_client_error("describe_replication_tasks", "AccessDeniedException", "not authorized")
    stub.activate()
    with mock_aws():
        boto3.client("cloudwatch", region_name=REGION)
        res = core.gather({"services": {"dms": {}, "cloudwatch": {"alarms": True}}}, StubSession(base, {"dms": dms}))
    assert any(e.startswith("dms.instances: AccessDeniedException") for e in res["errors"])
    assert any(e.startswith("dms.tasks: AccessDeniedException") for e in res["errors"])
    assert res["data"]["cloudwatch_alarms"] == []  # the other service still ran
    assert result_status(_result(res)) == "partial"


# ---------------------------------------------------------------- collector class, ssh transport, no-boto3

def test_missing_profile_is_a_clear_error():
    res = run_source(_source(aws="{profile: no-such-profile-xyz, region: us-east-1}"))
    assert result_status(res) == "failed"
    assert any("no-such-profile-xyz" in e or "profile" in e.lower() for e in res["errors"])


@mock_aws
def test_collector_collect_via_injected_session_and_render():
    boto3.client("s3", region_name=REGION).create_bucket(Bucket="acme-lake")
    src = _source(services="{s3: {prefixes: ['s3://acme-lake/bronze/']}}")
    out = awsc.AwsCollector().collect(src, RunContext(timeout_seconds=30, aws_session=guarded_session()))
    assert out["errors"] == [] and out["data"]["s3_prefixes"][0]["object_count"] == 0
    md = render({"source": "aws_main", "type": "aws", "schema_version": 1, "collected_at": "2026-10-20T12:00:00Z", **out})
    assert "## S3" in md and "s3://acme-lake/bronze/" in md
    assert "no objects found" in md  # an empty prefix cannot be fresh


@mock_aws
def test_ping_masks_the_account():
    src = _source()
    detail = awsc.AwsCollector().ping(src, RunContext(timeout_seconds=15, aws_session=guarded_session()))
    assert detail.startswith("sts ok: ") and "123456789012" not in detail and "****" in detail


def test_remote_script_is_self_contained_and_py38_syntax():
    import ast

    text = awsc._remote_script("remote_main", {"services": {}})
    ast.parse(Path(core.__file__).read_text(), feature_version=(3, 8))
    assert "from de_agent" not in text and "import de_agent" not in text
    ns: dict = {"__name__": "remote"}
    import contextlib
    import io

    buf = io.StringIO()
    with mock_aws(), contextlib.redirect_stdout(buf):
        exec(compile(text, "<remote>", "exec"), ns)  # what `ssh host python3 -` would run (against moto here)
    out = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert set(out) == {"data", "errors"} and out["data"]["meta"]["services"] == []


def test_ssh_transport_uses_ssh_and_returns_remote_json(monkeypatch):
    calls = []

    def fake_run(argv, input=None, capture_output=None, text=None, timeout=None):
        calls.append((argv, input))
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"data": {"meta": {}}, "errors": []}) + "\n", stderr="")

    monkeypatch.setattr(awsc.subprocess, "run", fake_run)
    src = _source(aws="{instance_role: true}", exec_="ssh:bastion")
    out = awsc.AwsCollector().collect(src, RunContext(timeout_seconds=30))
    assert out == {"data": {"meta": {}}, "errors": []}
    argv, script = calls[0]
    assert argv[-3:] == ["bastion", "python3", "-"] and "BatchMode=yes" in argv
    assert "remote_main" in script and "instance_role" in script


def test_ssh_ping_runs_the_remote_doctor(monkeypatch):
    def fake_run(argv, input=None, **kw):
        assert "remote_doctor" in input
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"ok": True, "detail": "arn:aws:sts::****9012:assumed-role/ro/x"}), stderr="")

    monkeypatch.setattr(awsc.subprocess, "run", fake_run)
    src = _source(aws="{instance_role: true}", exec_="ssh:bastion")
    assert "boto3 importable, sts ok: arn:aws:sts::****9012" in awsc.AwsCollector().ping(src, RunContext(timeout_seconds=30))

    def bad(argv, input=None, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"ok": False, "detail": "boto3 is not installed"}), stderr="")

    monkeypatch.setattr(awsc.subprocess, "run", bad)
    with pytest.raises(CollectorError, match="boto3 is not installed"):
        awsc.AwsCollector().ping(src, RunContext(timeout_seconds=30))


def test_ssh_failures_are_collector_errors(monkeypatch):
    monkeypatch.setattr(awsc.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 255, stdout="", stderr="ssh: Could not resolve hostname bastion for account 123456789012"))
    src = _source(aws="{instance_role: true}", exec_="ssh:bastion")
    with pytest.raises(CollectorError, match=r"\*{4}9012"):
        awsc.AwsCollector().collect(src, RunContext(timeout_seconds=30))
    monkeypatch.setattr(awsc.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="garbage\n", stderr=""))
    with pytest.raises(CollectorError, match="did not return JSON"):
        awsc.AwsCollector().collect(src, RunContext(timeout_seconds=30))


def test_core_cli_imports_and_runs_without_boto3():
    code = (
        "import sys\n"
        "sys.modules['boto3'] = None; sys.modules['botocore'] = None\n"
        "import de_agent.cli, de_agent.collectors, de_agent.sqlexec, de_agent.render\n"
        "from de_agent.config import parse_config\n"
        "from de_agent.runner import run_source\n"
        "cfg = parse_config('sources:\\n  - {name: a, type: aws, aws: {region: us-east-1}, services: {dms: {}}}\\n"
        "  - {name: r, type: redshift, aws: {region: us-east-1}, data_api: {cluster_id: acme-dwh, database: dev, db_user: ro}}\\n')\n"
        "for s in cfg.sources:\n"
        "    res = run_source(s)\n"
        "    print(s.name, res['errors'])\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT / "cli" / "src")
    assert out.returncode == 0, out.stderr
    assert out.stdout.count("pip install 'de-agent[aws]'") >= 2, out.stdout
