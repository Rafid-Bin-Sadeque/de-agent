"""Shared helpers for the AWS / Redshift tests. Everything is fictional (Acme); nothing here reaches a real account.

moto serves S3, Glue, Firehose and CloudWatch. moto has no DMS table statistics / describe_endpoints shapes we need and no
Athena BatchGetQueryExecution, so those go through botocore Stubber with documented response shapes (the stubber
validates every request and response against the service model).
"""
import json
from datetime import datetime, timedelta, timezone

from botocore.stub import Stubber
from de_agent.collectors.aws import _aws_core as core

REGION = "us-east-1"
ACCT = "123456789012"
INST_ARN = f"arn:aws:dms:{REGION}:{ACCT}:rep:ACMEINSTANCE1"
SRC_ARN = f"arn:aws:dms:{REGION}:{ACCT}:endpoint:SRC1"
TGT_S3_ARN = f"arn:aws:dms:{REGION}:{ACCT}:endpoint:TGTS3"
TGT_RS_ARN = f"arn:aws:dms:{REGION}:{ACCT}:endpoint:TGTRS"
TASK_S3_ARN = f"arn:aws:dms:{REGION}:{ACCT}:task:TASKS3RES"
TASK_RS_ARN = f"arn:aws:dms:{REGION}:{ACCT}:task:TASKRSRES"


def utcnow():
    return datetime.now(timezone.utc)


class SeenCalls:
    """Records every (service, operation) that reaches the guard, i.e. every call the collectors attempt."""

    def __init__(self):
        self.ops = []

    def handler(self, event_name=None, **kwargs):
        self.ops.append(core.event_service_op(event_name))


def guarded_session(sql_check=None, seen=None):
    """A boto3 session built by the product code (guard installed), plus an optional call recorder after it."""
    session = core.make_session({"region": REGION}, sql_check=sql_check)
    if seen is not None:
        session.events.register("before-call", seen.handler)
    return session


class StubSession:
    """Wraps a real (moto-backed, guarded) session; named services return pre-built stubbed or fake clients."""

    def __init__(self, base, overrides):
        self.base, self.overrides = base, overrides

    def client(self, name, **kw):
        return self.overrides[name] if name in self.overrides else self.base.client(name, **kw)


def stubbed(base, service):
    """(client, stubber) with the guard already on the client's events (the base session's emitter)."""
    client = base.client(service)
    stub = Stubber(client)
    return client, stub


class FakeCloudWatch:
    """Duck-typed CloudWatch client: get_metric_data answers from `series[(metric, stat, dim_value)]` (a list of floats)."""

    def __init__(self, series, now=None):
        self.series, self.calls, self.queries = series, 0, 0
        self.now = now or utcnow()

    def get_metric_data(self, **kw):
        self.calls += 1
        assert len(kw["MetricDataQueries"]) <= core.GMD_BATCH
        out = []
        for q in kw["MetricDataQueries"]:
            self.queries += 1
            ms = q["MetricStat"]
            dims = {d["Name"]: d["Value"] for d in ms["Metric"]["Dimensions"]}
            vals = None
            for (metric, stat, dim_value), v in self.series.items():
                if metric == ms["Metric"]["MetricName"] and stat == ms["Stat"] and dim_value in dims.values():
                    vals = v
            vals = vals or []
            ts = [self.now - timedelta(hours=len(vals) - i) for i in range(len(vals))]
            out.append({"Id": q["Id"], "Label": q["Id"], "Timestamps": ts, "Values": list(vals), "StatusCode": "Complete"})
        return {"MetricDataResults": out}


def dms_instance(**over):
    base = {"ReplicationInstanceIdentifier": "acme-dms-1", "ReplicationInstanceClass": "dms.r5.large",
            "ReplicationInstanceStatus": "available", "AllocatedStorage": 100, "EngineVersion": "3.5.2", "MultiAZ": False,
            "ReplicationInstanceArn": INST_ARN}
    base.update(over)
    return base


S3_RULES = {"rules": [
    {"rule-type": "selection", "rule-id": "1", "rule-name": "1", "object-locator": {"schema-name": "app", "table-name": "orders"},
     "rule-action": "include"},
    {"rule-type": "selection", "rule-id": "2", "rule-name": "2", "object-locator": {"schema-name": "app", "table-name": "customers"},
     "rule-action": "include"}]}
RS_RULES = {"rules": [
    {"rule-type": "selection", "rule-id": "1", "rule-name": "1", "object-locator": {"schema-name": "app", "table-name": "orders"},
     "rule-action": "include"},
    {"rule-type": "selection", "rule-id": "2", "rule-name": "2", "object-locator": {"schema-name": "app", "table-name": "%"},
     "rule-action": "include"},
    {"rule-type": "transformation", "rule-id": "3", "rule-name": "3", "rule-action": "rename", "rule-target": "schema",
     "object-locator": {"schema-name": "app"}, "value": "raw"}]}


def dms_task(tid, arn, tgt, rules, **over):
    base = {"ReplicationTaskIdentifier": tid, "ReplicationTaskArn": arn, "ReplicationInstanceArn": INST_ARN,
            "SourceEndpointArn": SRC_ARN, "TargetEndpointArn": tgt, "MigrationType": "full-load-and-cdc",
            "Status": "running", "TableMappings": json.dumps(rules), "ReplicationTaskStats": {"TablesErrored": 0}}
    base.update(over)
    return base


ENDPOINTS = [
    {"EndpointArn": SRC_ARN, "EndpointType": "SOURCE", "EngineName": "mysql", "EndpointIdentifier": "acme-src",
     "DatabaseName": "app"},
    {"EndpointArn": TGT_S3_ARN, "EndpointType": "TARGET", "EngineName": "s3", "EndpointIdentifier": "acme-s3",
     "S3Settings": {"BucketName": "acme-lake", "BucketFolder": "bronze"}},
    {"EndpointArn": TGT_RS_ARN, "EndpointType": "TARGET", "EngineName": "redshift", "EndpointIdentifier": "acme-rs",
     "DatabaseName": "dwh"},
]


def table_stat(schema, table, state="Table completed", ins=0, upd=0, dele=0, ddl=0, full=0):
    return {"SchemaName": schema, "TableName": table, "TableState": state, "Inserts": ins, "Updates": upd, "Deletes": dele,
            "Ddls": ddl, "FullLoadRows": full}


def build_dms_session(base, tasks, stats_by_arn, instances=None, endpoints=None, series=None, now=None):
    """Session whose `dms` client is a Stubber (validated shapes) and whose `cloudwatch` is a FakeCloudWatch."""
    dms, stub = stubbed(base, "dms")
    stub.add_response("describe_replication_instances", {"ReplicationInstances": instances or [dms_instance()]})
    # a live account drops TableMappings when WithoutSettings=True, so the collector must not send it
    stub.add_response("describe_replication_tasks", {"ReplicationTasks": tasks}, {"WithoutSettings": False})
    arns = sorted({a for t in tasks for a in (t["SourceEndpointArn"], t["TargetEndpointArn"])})
    stub.add_response("describe_endpoints", {"Endpoints": [e for e in (endpoints or ENDPOINTS) if e["EndpointArn"] in arns]})
    for t in tasks:
        stub.add_response("describe_table_statistics", {"ReplicationTaskArn": t["ReplicationTaskArn"],
                                                        "TableStatistics": stats_by_arn.get(t["ReplicationTaskArn"], [])})
    stub.activate()
    return StubSession(base, {"dms": dms, "cloudwatch": FakeCloudWatch(series or {}, now)}), stub
