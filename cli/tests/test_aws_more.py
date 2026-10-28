"""Error paths, caps and validation of the AWS collector (branches the happy-path tests do not reach)."""
import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from aws_fixtures import REGION, StubSession, guarded_session, stubbed, utcnow
from de_agent.collectors import aws as awsc
from de_agent.collectors.aws import _aws_core as core
from de_agent.config import ConfigError, parse_config
from moto import mock_aws


def _bad(yaml_bits):
    with pytest.raises(ConfigError):
        parse_config("sources:\n  - {name: a, type: aws, " + yaml_bits + "}\n")


@pytest.mark.parametrize("bits", [
    "aws: x, services: {dms: {}}",
    "aws: {region: 5}, services: {dms: {}}",
    "aws: {instance_role: maybe}, exec: 'ssh:h', services: {dms: {}}",
    "aws: {region: r}, services: {dms: {}}, options: {max_metrics: -1}",
    "aws: {region: r}, services: {dms: 5}",
    "aws: {region: r}, services: {glue: {crawlers: 'yes'}}",
    "aws: {region: r}, services: {glue: {catalog_databases: 'bronze'}}",
    "aws: {region: r}, services: {athena: {workgroups: 'primary'}}",
    "aws: {region: r}, services: {cloudwatch: {alarms: 1}}",
    "aws: {region: r}, services: {s3: {prefixes: ['s3://']}}",
    "aws: {region: r}, services: {s3: {prefixes: 'x'}}",
    "aws: {region: r}, services: {dms: {}}, conn: {dbname: d, user: u}",
])
def test_invalid_aws_configs_are_rejected(bits):
    _bad(bits)


def test_service_defaults_and_valid_blocks():
    src = parse_config("sources:\n  - {name: a, type: aws, aws: {region: r}, services: {glue: {jobs: ['a*'], crawlers: true, "
                       "catalog_databases: [b]}, athena: {workgroups: [primary]}, s3: {prefixes: ['s3://acme-lake/x/']}, "
                       "cloudwatch: {alarms: false}, firehose: {}}, options: {max_metrics: 10.5}}\n").sources[0]
    assert src.services["glue"]["jobs"] == ["a*"]


# ---- the guard's request parsing

def _event(service, op):
    return f"before-call.{service}.{op}"


def test_guard_handles_unreadable_and_empty_data_api_requests():
    guard = core.make_guard(lambda s: None)
    ev = _event("redshift-data", "ExecuteStatement")
    with pytest.raises(core.ReadOnlyViolation, match="not readable"):
        guard(event_name=ev, params={"body": b"\xff\xfe not json"})
    with pytest.raises(core.ReadOnlyViolation, match="no SQL to check"):
        guard(event_name=ev, params={"body": json.dumps({"Database": "d"}).encode()})
    assert guard(event_name=ev, params={"body": json.dumps({"Sql": "select 1"}).encode()}) is None
    assert guard(event_name=ev, params={"body": json.dumps({"Sql": "select 1"})}) is None  # str body too
    assert guard(event_name=_event("redshift-data", "CancelStatement"), params={}) is None


def test_guard_unknown_event_name_is_checked_as_an_unknown_operation():
    guard = core.make_guard()
    with pytest.raises(core.ReadOnlyViolation):
        guard(event_name="nonsense", params={})


def test_error_and_time_helpers():
    assert core.describe_error(core.ReadOnlyViolation("blocked x")) == "blocked x"
    assert core.describe_error(ValueError("bad thing\nsecond line aws_secret_access_key=abcd1234")).endswith("***")
    assert "****3456" in core.mask_ids("acct 123456123456 here")
    assert core.mask_ids("AKIAABCDEFGHIJKLMNOP") == "***"
    assert core._utc(0) == datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert core._utc("2026-10-20T12:00:00Z") == datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    assert core._utc("garbage") is None and core._utc(None) is None
    assert core._utc(datetime(2026, 1, 1)) == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert core._pct([], 0.95) is None and core._pct([1, 2, 3, 4], 0.5) in (2, 3)
    assert core._r(None) is None and core._r(1.23456, 1) == 1.2
    with pytest.raises(ValueError):
        core.parse_s3_uri("nope")


def test_make_session_profile_and_instance_role(monkeypatch):
    s = core.make_session({"instance_role": True, "region": "eu-west-1"})
    assert s.region_name == "eu-west-1"
    with pytest.raises(core.AwsUnavailable, match="no-such-profile"):
        core.make_session({"profile": "no-such-profile"})


@mock_aws
def test_gather_builds_its_own_guarded_session_by_default():
    boto3.client("cloudwatch", region_name=REGION)
    res = core.gather({"aws": {"region": REGION}, "services": {"cloudwatch": {}}})
    assert res["data"]["cloudwatch_alarms"] == [] and res["data"]["meta"]["services"] == ["cloudwatch"]


@mock_aws
def test_remote_entry_points():
    out = core.remote_main({"aws": {"instance_role": True, "region": REGION}, "services": {"cloudwatch": {}}})
    assert out["errors"] == [] and "cloudwatch_alarms" in out["data"]
    doc = core.remote_doctor()
    assert doc["ok"] is True and "****" in doc["detail"] and "123456789012" not in doc["detail"]


def test_remote_entry_point_failures(monkeypatch):
    monkeypatch.setattr(core, "make_session", lambda *a, **k: (_ for _ in ()).throw(core.AwsUnavailable("boto3 is not installed")))
    assert core.remote_main({"services": {}}) == {"data": {}, "errors": ["boto3 is not installed"]}
    assert core.remote_doctor() == {"ok": False, "detail": "boto3 is not installed"}
    monkeypatch.setattr(core, "make_session", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom 123456789012")))
    out = core.remote_main({"services": {}})
    assert out["errors"] == ["boom ****9012"]

    class S:
        def client(self, name):
            raise RuntimeError("sts down")

    monkeypatch.setattr(core, "make_session", lambda *a, **k: S())
    assert core.remote_doctor() == {"ok": False, "detail": "sts down"}


# ---- DMS errors and caps

def _dms_stub(base, steps):
    client, stub = stubbed(base, "dms")
    for kind, op, *rest in steps:
        if kind == "ok":
            stub.add_response(op, rest[0])
        else:
            stub.add_client_error(op, rest[0], rest[1] if len(rest) > 1 else "denied")
    stub.activate()
    return client, stub


def test_dms_endpoint_and_table_statistic_errors_are_reported_but_not_fatal():
    from aws_fixtures import (
        INST_ARN,
        S3_RULES,
        TASK_S3_ARN,
        TGT_S3_ARN,
        FakeCloudWatch,
        dms_instance,
        dms_task,
    )

    base = guarded_session()
    t = dms_task("acme-orders-cdc", TASK_S3_ARN, TGT_S3_ARN, S3_RULES)
    client, stub = _dms_stub(base, [
        ("ok", "describe_replication_instances", {"ReplicationInstances": [dms_instance()]}),
        ("ok", "describe_replication_tasks", {"ReplicationTasks": [t]}),
        ("err", "describe_endpoints", "AccessDeniedException"),
        ("err", "describe_table_statistics", "AccessDeniedException")])

    class BadCW(FakeCloudWatch):
        def get_metric_data(self, **kw):
            raise RuntimeError("throttled")

    res = core.gather({"services": {"dms": {}}}, StubSession(base, {"dms": client, "cloudwatch": BadCW({})}))
    assert any(e.startswith("dms.metrics: throttled") for e in res["errors"])
    assert any(e.startswith("dms.endpoints: AccessDeniedException") for e in res["errors"])
    assert any(e.startswith("dms.table_statistics acme-orders-cdc: AccessDeniedException") for e in res["errors"])
    task = res["data"]["dms_tasks"][0]
    assert task["target"] is None and task["rules"] and INST_ARN  # the task is still listed, without endpoint facts


def test_dms_lists_are_capped(monkeypatch):
    from aws_fixtures import build_dms_session, dms_instance

    monkeypatch.setattr(core, "MAX_ITEMS", 2)
    insts = [dms_instance(ReplicationInstanceIdentifier=f"i{i}", ReplicationInstanceArn=f"arn:aws:dms:{REGION}:123456789012:rep:R{i}")
             for i in range(5)]
    session, _ = build_dms_session(guarded_session(), [], {}, instances=insts)
    res = core.gather({"services": {"dms": {}}}, session)
    assert len(res["data"]["dms_instances"]) == 2 and res["data"]["meta"]["truncated"] == ["dms"]


def test_dms_total_failure_is_one_service_error():
    base = guarded_session()
    client, stub = stubbed(base, "dms")
    stub.add_client_error("describe_replication_instances", "AccessDeniedException", "no")
    stub.add_client_error("describe_replication_tasks", "AccessDeniedException", "no")
    stub.activate()
    res = core.gather({"services": {"dms": {}}}, StubSession(base, {"dms": client}))
    assert len(res["errors"]) == 2 and res["data"]["dms_instances"] == [] and res["data"]["dms_tasks"] == []


def test_service_level_exception_is_contained(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("unexpected 123456789012")

    for svc, fn in (("dms", "gather_dms"), ("glue", "gather_glue"), ("s3", "gather_s3"), ("athena", "gather_athena"),
                    ("firehose", "gather_firehose"), ("cloudwatch", "gather_cloudwatch")):
        monkeypatch.setattr(core, fn, boom)
        res = core.gather({"services": {svc: {}}}, guarded_session())
        assert res["errors"] == [f"{svc}: unexpected ****9012"], svc


# ---- Glue errors

@mock_aws
def test_glue_errors_are_contained_per_item():
    base = guarded_session()
    glue, stub = stubbed(base, "glue")
    stub.add_response("get_jobs", {"Jobs": [{"Name": "load_orders"}, {"Name": "load_bad"}]})
    stub.add_client_error("get_job_runs", "AccessDeniedException", "no")  # load_bad sorts first
    stub.add_response("get_job_runs", {"JobRuns": [{"Id": "jr_1", "JobRunState": "FAILED", "ErrorMessage": "x" * 500,
                                                    "StartedOn": utcnow() - timedelta(hours=1), "ExecutionTime": 5}]})
    stub.add_client_error("get_crawlers", "AccessDeniedException", "no")
    stub.add_client_error("get_tables", "EntityNotFoundException", "Database nope not found.")
    stub.add_response("get_tables", {"TableList": [{"Name": "p", "PartitionKeys": [{"Name": "dt", "Type": "string"}],
                                                    "StorageDescriptor": {"Location": "s3://b/p/"}}]})
    stub.add_client_error("get_partitions", "AccessDeniedException", "no")
    stub.activate()
    res = core.gather({"services": {"glue": {"crawlers": True, "catalog_databases": ["nope", "bronze"]}}},
                      StubSession(base, {"glue": glue}))
    errs = "\n".join(res["errors"])
    assert "glue.job_runs load_bad: AccessDeniedException" in errs and "glue.crawlers: AccessDeniedException" in errs
    assert "glue.catalog nope: EntityNotFoundException" in errs and "glue.partitions bronze.p: AccessDeniedException" in errs
    run = res["data"]["glue_job_runs"][0]
    assert run["state"] == "FAILED" and len(run["error"]) == 300
    assert res["data"]["glue_tables"][0]["partition_keys"] == ["dt"] and "partitions" not in res["data"]["glue_tables"][0]


def test_glue_jobs_listing_failure():
    base = guarded_session()
    glue, stub = stubbed(base, "glue")
    stub.add_client_error("get_jobs", "AccessDeniedException", "no")
    stub.activate()
    res = core.gather({"services": {"glue": {"jobs": ["*"]}}}, StubSession(base, {"glue": glue}))
    assert res["errors"] == ["glue.jobs: AccessDeniedException: no"]


# ---- S3 paging, Firehose paging and errors

@mock_aws
def test_s3_follows_continuation_tokens():
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket="acme-lake")
    for i in range(1003):
        s3.put_object(Bucket="acme-lake", Key=f"bronze/k{i:05d}", Body=b"ab")
    res = core.gather({"services": {"s3": {"prefixes": ["s3://acme-lake/bronze/"]}}}, guarded_session())
    row = res["data"]["s3_prefixes"][0]
    assert row["object_count"] == 1003 and row["bytes"] == 2006 and row["newest_complete"] is True


@mock_aws
def test_s3_metrics_failure_falls_back_to_listing():
    boto3.client("s3", region_name=REGION).create_bucket(Bucket="acme-raw")

    class BadCW:
        def get_metric_data(self, **kw):
            raise RuntimeError("throttled")

    res = core.gather({"services": {"s3": {"prefixes": ["s3://acme-raw/"]}}}, StubSession(guarded_session(), {"cloudwatch": BadCW()}))
    assert any(e.startswith("s3.metrics: throttled") for e in res["errors"])
    assert res["data"]["s3_prefixes"][0]["source"] == "list"


def test_firehose_paging_and_errors():
    base = guarded_session()
    fh, stub = stubbed(base, "firehose")
    stub.add_response("list_delivery_streams", {"DeliveryStreamNames": ["a"], "HasMoreDeliveryStreams": True})
    stub.add_response("list_delivery_streams", {"DeliveryStreamNames": ["b"], "HasMoreDeliveryStreams": False})
    desc = {"DeliveryStreamDescription": {"DeliveryStreamName": "a", "DeliveryStreamARN": "arn:aws:firehose:us-east-1:123456789012:deliverystream/a",
                                          "DeliveryStreamStatus": "ACTIVE", "DeliveryStreamType": "DirectPut", "VersionId": "1",
                                          "Destinations": [], "HasMoreDestinations": False}}
    stub.add_response("describe_delivery_stream", desc)
    stub.add_client_error("describe_delivery_stream", "ResourceNotFoundException", "gone")
    stub.activate()
    from aws_fixtures import FakeCloudWatch

    res = core.gather({"services": {"firehose": {}}}, StubSession(base, {"firehose": fh, "cloudwatch": FakeCloudWatch({})}))
    assert [r["stream"] for r in res["data"]["firehose_streams"]] == ["a"]
    assert res["data"]["firehose_streams"][0]["bucket"] is None
    assert any(e.startswith("firehose.describe b: ResourceNotFoundException") for e in res["errors"])
    assert not any("freshness" in s for s in __import__("de_agent.render", fromlist=["x"]).aws_signals(res["data"]))


def test_firehose_list_failure_and_metric_failure(monkeypatch):
    base = guarded_session()
    fh, stub = stubbed(base, "firehose")
    stub.add_client_error("list_delivery_streams", "AccessDeniedException", "no")
    stub.activate()
    res = core.gather({"services": {"firehose": {}}}, StubSession(base, {"firehose": fh}))
    assert res["errors"] == ["firehose.list: AccessDeniedException: no"] and "firehose_streams" not in res["data"]
    monkeypatch.setattr(core, "MAX_ITEMS", 1)
    fh2, stub2 = stubbed(base, "firehose")
    stub2.add_response("list_delivery_streams", {"DeliveryStreamNames": ["a", "b"], "HasMoreDeliveryStreams": False})
    stub2.add_response("describe_delivery_stream", {"DeliveryStreamDescription": {
        "DeliveryStreamName": "a", "DeliveryStreamARN": "arn:aws:firehose:us-east-1:123456789012:deliverystream/a",
        "DeliveryStreamStatus": "ACTIVE", "DeliveryStreamType": "DirectPut", "VersionId": "1", "Destinations": [],
        "HasMoreDestinations": False}})
    stub2.activate()

    class BadCW:
        def get_metric_data(self, **kw):
            raise RuntimeError("throttled")

    res = core.gather({"services": {"firehose": {}}}, StubSession(base, {"firehose": fh2, "cloudwatch": BadCW()}))
    assert res["data"]["meta"]["truncated"] == ["firehose"] and any(e.startswith("firehose.metrics") for e in res["errors"])


@mock_aws
def test_cloudwatch_alarm_listing_failure():
    base = guarded_session()
    cw, stub = stubbed(base, "cloudwatch")
    stub.add_client_error("describe_alarms", "AccessDenied", "no")
    stub.activate()
    res = core.gather({"services": {"cloudwatch": {"alarms": True}}}, StubSession(base, {"cloudwatch": cw}))
    assert res["errors"] == ["cloudwatch.alarms: AccessDenied: no"]
    assert core.gather({"services": {"cloudwatch": {"alarms": False}}}, guarded_session())["data"].get("cloudwatch_alarms") is None


def test_collector_python_errors_become_collector_errors(monkeypatch):
    from de_agent.collectors.base import CollectorError, RunContext

    src = parse_config("sources:\n  - {name: a, type: aws, aws: {region: r}, services: {dms: {}}}\n").sources[0]
    monkeypatch.setattr(core, "make_session", lambda *a, **k: (_ for _ in ()).throw(core.AwsUnavailable("pip install 'de-agent[aws]'")))
    with pytest.raises(CollectorError, match="pip install"):
        awsc.AwsCollector().collect(src, RunContext(timeout_seconds=5))
    with pytest.raises(CollectorError, match="pip install"):
        awsc.AwsCollector().ping(src, RunContext(timeout_seconds=5))

    class S:
        def client(self, name):
            raise RuntimeError("sts down 123456789012")

    with pytest.raises(CollectorError, match=r"sts down \*{4}9012"):
        awsc.AwsCollector().ping(src, RunContext(timeout_seconds=5, aws_session=S()))
    import subprocess

    monkeypatch.setattr(awsc.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    ssh = parse_config("sources:\n  - {name: a, type: aws, exec: 'ssh:h', aws: {instance_role: true}, services: {dms: {}}}\n").sources[0]
    with pytest.raises(CollectorError, match="ssh not found"):
        awsc.AwsCollector().collect(ssh, RunContext(timeout_seconds=5))
    monkeypatch.setattr(awsc.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("ssh", 1)))
    with pytest.raises(CollectorError, match="timed out"):
        awsc.AwsCollector().collect(ssh, RunContext(timeout_seconds=5))


def test_remote_doctor_accepts_the_config_argument_the_ssh_wrapper_passes():
    # _remote_script calls <fn>(json.loads(cfg)) for every entry point; a zero-argument remote_doctor failed on a live host
    import inspect

    assert len(inspect.signature(core.remote_doctor).parameters) == 1
