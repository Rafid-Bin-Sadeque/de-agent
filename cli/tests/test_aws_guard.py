"""Read-only enforcement for AWS (M3 section 2): the guard, the IAM policy drift test, secret_guard."""
import json
import subprocess
import sys
from pathlib import Path

import boto3
import pytest
from aws_fixtures import REGION, SeenCalls, guarded_session
from de_agent.collectors.aws import _aws_core as core
from de_agent.sqlexec import check_read_only
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "docs" / "aws-iam-readonly.json"


@pytest.mark.parametrize("op", ["DescribeInstances", "ListObjectsV2", "GetMetricData", "BatchGetQueryExecution",
                                "DescribeTableStatistics", "HeadBucket", "GetJobRuns"])
def test_read_operations_are_allowed(op):
    assert core.operation_allowed("x", op)


@pytest.mark.parametrize("op", ["CreateBucket", "DeleteObject", "PutObject", "StartReplicationTask", "StopReplicationTask",
                                "StartQueryExecution", "UpdateJob", "PutMetricData", "TerminateInstances",
                                "GetObject", "GetSecretValue", "GetQueryResults"])
def test_write_and_data_reading_operations_are_refused(op):
    assert not core.operation_allowed("x", op)


@mock_aws
def test_guarded_session_blocks_create_bucket_and_moto_sees_no_call():
    session = guarded_session()
    with pytest.raises(core.ReadOnlyViolation, match="blocked s3.CreateBucket"):
        session.client("s3").create_bucket(Bucket="acme-lake")
    plain = boto3.client("s3", region_name=REGION)  # an unguarded client on the same moto backend
    assert plain.list_buckets()["Buckets"] == []  # moto never saw the call
    with pytest.raises(core.ReadOnlyViolation, match="blocked dms.StartReplicationTask"):
        session.client("dms").start_replication_task(
            ReplicationTaskArn="arn:aws:dms:us-east-1:123456789012:task:X", StartReplicationTaskType="start-replication")


@mock_aws
def test_guarded_session_allows_reads():
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket="acme-lake")
    assert guarded_session().client("s3").list_objects_v2(Bucket="acme-lake")["KeyCount"] == 0


def test_data_api_needs_a_read_only_sql_check():
    session = guarded_session()  # no sql_check configured
    with pytest.raises(core.ReadOnlyViolation, match="no read-only SQL check"):
        session.client("redshift-data").execute_statement(Sql="select 1", ClusterIdentifier="c", Database="d", DbUser="u")


def test_data_api_refuses_writes_before_any_call():
    seen = SeenCalls()
    session = guarded_session(lambda s: check_read_only(s, "redshift"), seen)
    client = session.client("redshift-data")
    for sql in ("drop table acme.orders", "delete from acme.orders", "insert into acme.t values (1)"):
        with pytest.raises(core.ReadOnlyViolation, match="refused"):
            client.batch_execute_statement(Sqls=["set statement_timeout to 1000", sql], ClusterIdentifier="c",
                                           Database="d", DbUser="u")
    assert seen.ops == []  # our handler is registered after the guard: nothing got past it


def test_no_credentials_in_config_keys():
    from de_agent.config import ConfigError, parse_config

    with pytest.raises(ConfigError, match="aws_secret_access_key|secret literal|unknown aws keys"):
        parse_config("sources:\n  - {name: a, type: aws, aws: {aws_secret_access_key: abc}, services: {dms: {}}}\n")
    with pytest.raises(ConfigError, match="unknown aws keys"):
        parse_config("sources:\n  - {name: a, type: aws, aws: {access_key_id: abc}, services: {dms: {}}}\n")


# ---- IAM policy drift -------------------------------------------------------------------------------------------

def test_iam_policy_file_matches_the_generated_policy():
    assert json.loads(POLICY.read_text()) == core.iam_policy(), (
        "docs/aws-iam-readonly.json drifted from USED_OPERATIONS: regenerate with "
        "`python -W ignore -m de_agent.collectors.aws._aws_core > docs/aws-iam-readonly.json`")


def test_policy_only_contains_read_actions():
    for st in core.iam_policy()["Statement"]:
        for action in st["Action"]:
            op = action.split(":", 1)[1]
            assert (core.operation_allowed(action.split(":")[0], op) or action in core.IMPLICIT_DATA_API_ACTIONS
                    or op in ("ListBucket", "ExecuteStatement", "BatchExecuteStatement", "CancelStatement")), action


def test_every_used_operation_passes_the_guard():
    for service, ops in core.USED_OPERATIONS.items():
        for op in ops:
            ok = core.operation_allowed(service, op) or (service == core.DATA_API_SERVICE and op in core.DATA_API_OPS)
            assert ok, (service, op)


def test_script_can_regenerate_the_policy():
    out = subprocess.run([sys.executable, "-W", "ignore", "-m", "de_agent.collectors.aws._aws_core"], capture_output=True, text=True,
                         cwd=ROOT / "cli" / "src")
    assert out.returncode == 0 and json.loads(out.stdout) == core.iam_policy()
