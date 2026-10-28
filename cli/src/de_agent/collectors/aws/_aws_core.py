"""AWS read-only gathering: DMS, Glue, S3, Athena, Firehose, CloudWatch (M3 section 2), plus the read-only guard.

This file is executed in two places: imported locally by collectors/aws/__init__.py and collectors/redshift.py, and
shipped as text over `ssh <host> python3 -` for `exec: ssh:<host>` sources (it then uses the remote instance role).
Keep it Python 3.8+, stdlib + boto3 only, and free of de-agent imports.

Read-only is enforced in code: every session gets a botocore `before-call` handler that raises BEFORE any network call
unless the operation is Describe*/List*/Get*/BatchGet*/Head* (or explicitly allowlisted), so a collector that tried
`create_bucket` or `start_replication_task` is stopped locally. The IAM policy in docs/aws-iam-readonly.json is generated
from USED_OPERATIONS below, and a test fails when the two drift.
"""
import fnmatch
import json
import re
from datetime import datetime, timedelta, timezone

WINDOW_H = 24
HOUR = 3600
MAX_ITEMS = 200  # instances / tasks / jobs / crawlers / streams / tables per service
MAX_PAGES = 10
MAX_PARTITIONS = 5000
DEFAULT_MAX_METRICS = 300
GMD_BATCH = 500  # GetMetricData accepts up to 500 queries per call
TOP_N = 10
S3_METRIC_WINDOW_H = 72  # BucketSizeBytes / NumberOfObjects are daily metrics

# ---- read-only guard ---------------------------------------------------------------------------------------------

ALLOWED_PREFIXES = ("Describe", "List", "Get", "BatchGet", "Head")
EXPLICIT_ALLOWED = ("GetMetricData", "BatchGetQueryExecution", "DescribeTableStatistics")
# prefix-allowed names that still read DATA (object bodies, secrets, query results): refused anyway
DENIED = ("GetObject", "GetObjectTorrent", "GetSecretValue", "GetParameter", "GetParameters", "GetParametersByPath",
          "GetQueryResults", "GetObjectAcl", "GetObjectAttributes")
# the Redshift Data API runs SQL, so it is allowed only when every statement passes the caller's read-only SQL check
DATA_API_OPS = ("ExecuteStatement", "BatchExecuteStatement", "CancelStatement")
DATA_API_SERVICE = "redshift-data"

# service segment of the botocore event name -> operations the collectors actually call (the IAM policy source)
USED_OPERATIONS = {
    "cloudwatch": ["DescribeAlarms", "GetMetricData"],
    "dms": ["DescribeEndpoints", "DescribeReplicationInstances", "DescribeReplicationTasks", "DescribeTableStatistics"],
    "glue": ["GetCrawlers", "GetJobRuns", "GetJobs", "GetPartitions", "GetTables"],
    "s3": ["ListObjectsV2"],
    "athena": ["BatchGetQueryExecution", "ListQueryExecutions"],
    "firehose": ["DescribeDeliveryStream", "ListDeliveryStreams"],
    "redshift": ["DescribeClusters"],
    "redshift-serverless": ["GetWorkgroup"],
    "redshift-data": ["BatchExecuteStatement", "CancelStatement", "DescribeStatement", "GetStatementResult"],
    "sts": ["GetCallerIdentity"],
}
# IAM action names that differ from the API operation name
IAM_ACTION = {("s3", "ListObjectsV2"): "s3:ListBucket"}
IAM_NO_PERMISSION = {("sts", "GetCallerIdentity")}  # needs no IAM permission
# needed only when the Redshift Data API is used: the credential lookups the service performs for the caller
IMPLICIT_DATA_API_ACTIONS = ["redshift:GetClusterCredentials", "redshift-serverless:GetCredentials",
                             "secretsmanager:GetSecretValue"]


class ReadOnlyViolation(Exception):
    """A blocked AWS call: raised before any network request is made."""


class AwsUnavailable(Exception):
    """boto3 is not installed or no usable credentials are configured."""


def operation_allowed(service, op):
    if op in DENIED:
        return False
    return op in EXPLICIT_ALLOWED or op.startswith(ALLOWED_PREFIXES)


# botocore event-name service segment -> IAM / USED_OPERATIONS prefix (only DMS differs)
EVENT_SERVICE = {"database-migration-service": "dms"}


def event_service_op(event_name):
    """('dms', 'DescribeReplicationTasks') from 'before-call.database-migration-service.DescribeReplicationTasks'."""
    parts = (event_name or "").split(".")
    if len(parts) < 3:
        return "?", "?"
    return EVENT_SERVICE.get(parts[1], parts[1]), parts[2]


def _request_sqls(request):
    """The SQL strings of a Redshift Data API request (json protocol: the body carries Sql / Sqls); fails closed."""
    body = (request or {}).get("body")
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    try:
        doc = json.loads(body)
    except (TypeError, ValueError):
        raise ReadOnlyViolation("blocked redshift-data call: request body is not readable, cannot check the SQL") from None
    sqls = list(doc.get("Sqls") or [])
    if doc.get("Sql"):
        sqls.append(doc["Sql"])
    if not sqls:
        raise ReadOnlyViolation("blocked redshift-data call: no SQL to check")
    return sqls


def make_guard(sql_check=None):
    """The `before-call` handler. `sql_check(sql)` must raise for anything that is not a read (Data API only)."""

    def guard(event_name=None, params=None, **kwargs):
        service, op = event_service_op(event_name)
        if service == DATA_API_SERVICE and op in DATA_API_OPS:
            if op == "CancelStatement":
                return
            if sql_check is None:
                raise ReadOnlyViolation("blocked %s.%s: no read-only SQL check is configured" % (service, op))
            for sql in _request_sqls(params):
                if re.match(r"^\s*set\s+statement_timeout\s+to\s+\d+\s*;?\s*$", sql, re.I):
                    continue
                try:
                    sql_check(sql)
                except Exception as ex:
                    raise ReadOnlyViolation("blocked %s.%s: %s" % (service, op, ex)) from None
            return
        if not operation_allowed(service, op):
            raise ReadOnlyViolation("blocked %s.%s: only read-only API calls are allowed" % (service, op))

    return guard


def make_session(aws_cfg, sql_check=None):
    """boto3 Session with the read-only guard. `aws_cfg`: {profile, region} or {instance_role: true}; never keys."""
    try:
        import boto3
    except ImportError:
        raise AwsUnavailable("boto3 is not installed: pip install 'de-agent[aws]'") from None
    kwargs = {}
    if aws_cfg.get("profile") and not aws_cfg.get("instance_role"):
        kwargs["profile_name"] = aws_cfg["profile"]
    if aws_cfg.get("region"):
        kwargs["region_name"] = aws_cfg["region"]
    try:
        session = boto3.Session(**kwargs)
    except Exception as ex:  # ProfileNotFound and friends
        raise AwsUnavailable(describe_error(ex)) from None
    session.events.register("before-call", make_guard(sql_check))
    return session


CREDENTIAL_RESOURCES = [
    "arn:aws:secretsmanager:<region>:<account>:secret:<your-redshift-secret>*",
    "arn:aws:redshift:<region>:<account>:dbuser:<cluster>/<readonly_user>",
    "arn:aws:redshift:<region>:<account>:dbname:<cluster>/<database>",
    "arn:aws:redshift-serverless:<region>:<account>:workgroup/<workgroup-id>",
]


def iam_policy():
    """The minimal read-only IAM policy, generated from USED_OPERATIONS (see docs/aws-iam-readonly.json)."""
    statements = []
    for service in sorted(USED_OPERATIONS):
        actions = set()
        for op in USED_OPERATIONS[service]:
            if (service, op) in IAM_NO_PERMISSION:
                continue
            actions.add(IAM_ACTION.get((service, op), "%s:%s" % (service, op)))
        if actions:
            statements.append({"Sid": "Gt" + re.sub(r"[^A-Za-z0-9]", "", service.title()), "Effect": "Allow",
                               "Action": sorted(actions), "Resource": "*"})
    # Scoped, never "*": replace every <placeholder> with your own ARN before attaching (see docs/collectors.md).
    statements.append({"Sid": "GtRedshiftDataApiCredentials", "Effect": "Allow",
                       "Action": sorted(IMPLICIT_DATA_API_ACTIONS), "Resource": CREDENTIAL_RESOURCES})
    return {"Version": "2012-10-17", "Statement": statements}


# ---- helpers -----------------------------------------------------------------------------------------------------

_ACCOUNT_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")
_SECRET_RE = [
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "***"),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"), "***"),
    (re.compile(r"(aws_secret_access_key\s*[=:]\s*)\S+", re.I), r"\1***"),
]


def mask_ids(text):
    """12-digit account ids -> ****1234; access key ids and secrets -> ***."""
    for rx, rep in _SECRET_RE:
        text = rx.sub(rep, text)
    return _ACCOUNT_RE.sub(lambda m: "****" + m.group(0)[-4:], text)


def describe_error(ex):
    """One short, masked line for an exception from boto3."""
    if isinstance(ex, ReadOnlyViolation):
        return str(ex)
    resp = getattr(ex, "response", None)
    if isinstance(resp, dict) and resp.get("Error"):
        err = resp["Error"]
        return mask_ids("%s: %s" % (err.get("Code", "Error"), str(err.get("Message", "")).strip()[:200]))
    text = (str(ex).strip().splitlines() or [type(ex).__name__])[-1]
    return mask_ids(text[:240])


def _utc(dt):
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return datetime.fromtimestamp(dt, timezone.utc)
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt):
    d = _utc(dt)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


def _pct(values, q):
    """Nearest-rank percentile; None for an empty series."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    k = max(0, min(len(xs) - 1, int(round(q * len(xs) + 0.5)) - 1))
    return xs[k]


def _r(v, nd=2):
    return None if v is None else round(float(v), nd)


def _paginate(client, op, key, cap=None, **kwargs):
    """Items of paginated list operation `op` (client method name), capped; returns (items, truncated)."""
    out = []
    for page in client.get_paginator(op).paginate(**kwargs):
        out.extend(page.get(key) or [])
        if cap and len(out) >= cap:
            return out[:cap], True
    return out, False


def _arn_tail(arn):
    return str(arn or "").rsplit(":", 1)[-1]


# ---- CloudWatch metrics (the cost guard) --------------------------------------------------------------------------

class Metrics:
    """Batched GetMetricData (<= 500 queries per call, 1 h period over the window), capped at `cap` metrics in total."""

    def __init__(self, session, now, window_h, cap):
        self.cw = session.client("cloudwatch")
        self.now, self.window_h, self.cap = now, window_h, cap
        self.requested = 0
        self.skipped = 0
        self.calls = 0

    def fetch(self, specs, window_h=None):
        """specs: list of (key, namespace, metric, dims dict, stat, period_s). Returns {key: [values, oldest first]}.
        `window_h` widens the window for daily metrics (S3 storage metrics are published once a day)."""
        room = max(0, self.cap - self.requested)
        take, self.skipped = specs[:room], self.skipped + max(0, len(specs) - room)
        self.requested += len(take)
        out = {}
        for i in range(0, len(take), GMD_BATCH):
            chunk = take[i:i + GMD_BATCH]
            queries, ids = [], {}
            for n, (key, ns, metric, dims, stat, period) in enumerate(chunk):
                qid = "m%d" % n
                ids[qid] = key
                queries.append({"Id": qid, "ReturnData": True, "MetricStat": {
                    "Metric": {"Namespace": ns, "MetricName": metric,
                               "Dimensions": [{"Name": k, "Value": v} for k, v in sorted(dims.items())]},
                    "Period": period, "Stat": stat}})
            token = None
            series = {}
            while True:
                kwargs = {"MetricDataQueries": queries, "StartTime": self.now - timedelta(hours=window_h or self.window_h),
                          "EndTime": self.now, "ScanBy": "TimestampAscending"}
                if token:
                    kwargs["NextToken"] = token
                self.calls += 1
                resp = self.cw.get_metric_data(**kwargs)
                for res in resp.get("MetricDataResults") or []:
                    pts = series.setdefault(res["Id"], [])
                    pts.extend(zip(res.get("Timestamps") or [], res.get("Values") or []))
                token = resp.get("NextToken")
                if not token:
                    break
            for qid, key in ids.items():
                out[key] = [v for _, v in sorted(series.get(qid, []), key=lambda p: _utc(p[0]))]
        return out


# ---- DMS ----------------------------------------------------------------------------------------------------------

_VCPU = {"large": 2, "xlarge": 4, "2xlarge": 8, "4xlarge": 16, "8xlarge": 32, "12xlarge": 48, "16xlarge": 64,
         "24xlarge": 96}
_GB_PER_VCPU = {"r": 8, "c": 2, "m": 4, "t": 4}
_SMALL = {"micro": 1, "small": 2, "medium": 4}


def instance_memory_gb(klass):
    """Approximate RAM of a dms.<family><gen>.<size> class, None when unknown."""
    m = re.match(r"^dms\.([a-z])\d[a-z]*\.([a-z0-9]+)$", klass or "")
    if not m:
        return None
    fam, size = m.groups()
    if size in _SMALL:
        return float(_SMALL[size]) if fam == "t" else None
    if size in _VCPU and fam in _GB_PER_VCPU:
        return float(_VCPU[size] * _GB_PER_VCPU[fam])
    return None


def _rules(table_mappings):
    """Selection + transformation rules of a DMS task as plain dicts (wildcards kept as patterns)."""
    try:
        doc = json.loads(table_mappings) if isinstance(table_mappings, str) else (table_mappings or {})
    except ValueError:
        return []
    out = []
    for r in doc.get("rules") or []:
        loc = r.get("object-locator") or {}
        row = {"type": r.get("rule-type"), "action": r.get("rule-action"), "target": r.get("rule-target"),
               "schema": loc.get("schema-name"), "table": loc.get("table-name")}
        if r.get("value") is not None:
            row["value"] = r.get("value")
        out.append({k: v for k, v in row.items() if v is not None})
    return out


def gather_dms(session, cfg, now, metrics, errors):
    dms = session.client("dms")
    out = {}
    try:
        instances, trunc = _paginate(dms, "describe_replication_instances", "ReplicationInstances", MAX_ITEMS)
    except Exception as ex:
        errors.append("dms.instances: " + describe_error(ex))
        instances, trunc = [], False
    arn_to_id = {i.get("ReplicationInstanceArn"): i.get("ReplicationInstanceIdentifier") for i in instances}
    specs = []
    for i in instances:
        iid = i.get("ReplicationInstanceIdentifier")
        dims = {"ReplicationInstanceIdentifier": iid}
        specs += [((iid, "cpu_avg"), "AWS/DMS", "CPUUtilization", dims, "Average", HOUR),
                  ((iid, "cpu_max"), "AWS/DMS", "CPUUtilization", dims, "Maximum", HOUR),
                  ((iid, "cpu_p95"), "AWS/DMS", "CPUUtilization", dims, "p95", HOUR),
                  ((iid, "mem_min"), "AWS/DMS", "FreeableMemory", dims, "Minimum", HOUR),
                  ((iid, "swap_max"), "AWS/DMS", "SwapUsage", dims, "Maximum", HOUR)]
    try:
        tasks, ttrunc = _paginate(dms, "describe_replication_tasks", "ReplicationTasks", MAX_ITEMS,
                                  WithoutSettings=False)  # True also drops TableMappings on a live account
    except Exception as ex:
        errors.append("dms.tasks: " + describe_error(ex))
        tasks, ttrunc = [], False
    for t in tasks:
        iid = arn_to_id.get(t.get("ReplicationInstanceArn"))
        dims = {"ReplicationInstanceIdentifier": iid or "", "ReplicationTaskIdentifier": _arn_tail(t.get("ReplicationTaskArn"))}
        tid = t.get("ReplicationTaskIdentifier")
        for m, key in (("CDCLatencySource", "src"), ("CDCLatencyTarget", "tgt")):
            specs += [((tid, key + "_p95"), "AWS/DMS", m, dims, "p95", HOUR),
                      ((tid, key + "_max"), "AWS/DMS", m, dims, "Maximum", HOUR)]
    try:
        series = metrics.fetch(specs) if specs else {}
    except Exception as ex:
        errors.append("dms.metrics: " + describe_error(ex))
        series = {}

    rows = []
    for i in instances:
        iid = i.get("ReplicationInstanceIdentifier")
        klass = i.get("ReplicationInstanceClass")
        mem = series.get((iid, "mem_min"))
        swap = series.get((iid, "swap_max"))
        gb = instance_memory_gb(klass)
        cpu = series.get((iid, "cpu_avg"))
        rows.append({
            "id": iid, "class": klass, "engine_version": i.get("EngineVersion"), "storage_gb": i.get("AllocatedStorage"),
            "multi_az": bool(i.get("MultiAZ")), "status": i.get("ReplicationInstanceStatus"),
            "memory_gb": gb,
            "cpu_avg": _r(sum(cpu) / len(cpu)) if cpu else None,
            "cpu_max": _r(max(series[(iid, "cpu_max")])) if series.get((iid, "cpu_max")) else None,
            "cpu_p95": _r(max(series[(iid, "cpu_p95")])) if series.get((iid, "cpu_p95")) else None,
            "free_mem_min_bytes": int(min(mem)) if mem else None,
            "free_mem_min_pct": _r(100.0 * min(mem) / (gb * 1024 ** 3)) if (mem and gb) else None,
            "swap_max_bytes": int(max(swap)) if swap else None,
            "swap_hours": sum(1 for v in swap if v > 0) if swap else None,
        })
    out["dms_instances"] = sorted(rows, key=lambda r: str(r["id"]))

    endpoints = {}
    arns = sorted({a for t in tasks for a in (t.get("SourceEndpointArn"), t.get("TargetEndpointArn")) if a})
    if arns:
        try:
            eps, _ = _paginate(dms, "describe_endpoints", "Endpoints", MAX_ITEMS,
                               Filters=[{"Name": "endpoint-arn", "Values": arns}])
            endpoints = {e.get("EndpointArn"): e for e in eps}
        except Exception as ex:
            errors.append("dms.endpoints: " + describe_error(ex))

    def endpoint(arn):
        e = endpoints.get(arn)
        if not e:
            return None
        s3 = e.get("S3Settings") or {}
        row = {"engine": e.get("EngineName"), "database": e.get("DatabaseName"), "bucket": s3.get("BucketName"),
               "folder": s3.get("BucketFolder")}
        return {k: v for k, v in row.items() if v}

    trows, issues, top = [], [], []
    for t in tasks:
        tid = t.get("ReplicationTaskIdentifier")
        stats = t.get("ReplicationTaskStats") or {}
        failure = t.get("LastFailureMessage")
        stop = _utc(stats.get("StopDate"))
        row = {
            "id": tid, "type": t.get("MigrationType"), "status": t.get("Status"), "stop_reason": t.get("StopReason"),
            "instance": arn_to_id.get(t.get("ReplicationInstanceArn")),
            "last_failure_message": (str(failure)[:300] if failure else None),
            "last_failure_at": _iso(stop) if failure and stop else None,
            "cdc_latency_source_p95_s": _r(max(series[(tid, "src_p95")])) if series.get((tid, "src_p95")) else None,
            "cdc_latency_source_max_s": _r(max(series[(tid, "src_max")])) if series.get((tid, "src_max")) else None,
            "cdc_latency_target_p95_s": _r(max(series[(tid, "tgt_p95")])) if series.get((tid, "tgt_p95")) else None,
            "cdc_latency_target_max_s": _r(max(series[(tid, "tgt_max")])) if series.get((tid, "tgt_max")) else None,
            "tables_errored": stats.get("TablesErrored"),
            "source": endpoint(t.get("SourceEndpointArn")), "target": endpoint(t.get("TargetEndpointArn")),
            "rules": _rules(t.get("TableMappings")),
        }
        try:
            stat_rows, _ = _paginate(dms, "describe_table_statistics", "TableStatistics", 10000,
                                     ReplicationTaskArn=t.get("ReplicationTaskArn"),
                                     PaginationConfig={"PageSize": 500})
        except Exception as ex:
            errors.append("dms.table_statistics %s: %s" % (tid, describe_error(ex)))
            stat_rows = []
        n_susp = 0
        cand = []
        for s in stat_rows:
            state = s.get("TableState") or ""
            changes = sum(int(s.get(k) or 0) for k in ("Inserts", "Deletes", "Updates", "Ddls"))
            base = {"task": tid, "schema": s.get("SchemaName"), "table": s.get("TableName")}
            if "error" in state.lower() or "suspend" in state.lower():
                if "suspend" in state.lower():
                    n_susp += 1
                issues.append(dict(base, state=state))
            cand.append(dict(base, state=state, cdc_changes=changes, inserts=int(s.get("Inserts") or 0),
                             updates=int(s.get("Updates") or 0), deletes=int(s.get("Deletes") or 0),
                             ddls=int(s.get("Ddls") or 0), full_load_rows=int(s.get("FullLoadRows") or 0)))
        row["tables_suspended"] = n_susp
        row["tables_total"] = len(stat_rows)
        top += sorted(cand, key=lambda r: (-r["cdc_changes"], str(r["schema"]), str(r["table"])))[:TOP_N]
        trows.append(row)
    out["dms_tasks"] = sorted(trows, key=lambda r: str(r["id"]))
    out["dms_table_issues"] = sorted(issues, key=lambda r: (str(r["task"]), str(r["schema"]), str(r["table"])))
    out["dms_top_tables"] = sorted(top, key=lambda r: (str(r["task"]), -r["cdc_changes"], str(r["table"])))
    return out, trunc or ttrunc


# ---- Glue ---------------------------------------------------------------------------------------------------------

def gather_glue(session, cfg, now, errors):
    glue = session.client("glue")
    out = {}
    truncated = False
    patterns = cfg.get("jobs") if cfg.get("jobs") is not None else ["*"]
    runs = []
    try:
        jobs, truncated = _paginate(glue, "get_jobs", "Jobs", MAX_ITEMS)
        for j in sorted(jobs, key=lambda j: str(j.get("Name"))):
            name = j.get("Name")
            if not any(fnmatch.fnmatchcase(str(name), p) for p in patterns):
                continue
            try:
                resp = glue.get_job_runs(JobName=name, MaxResults=5)
            except Exception as ex:
                errors.append("glue.job_runs %s: %s" % (name, describe_error(ex)))
                continue
            for r in (resp.get("JobRuns") or [])[:5]:
                err = r.get("ErrorMessage")
                runs.append({"job": name, "run_id": r.get("Id"), "state": r.get("JobRunState"),
                             "started": _iso(r.get("StartedOn")), "completed": _iso(r.get("CompletedOn")),
                             "duration_s": r.get("ExecutionTime"), "dpu_seconds": r.get("DPUSeconds"),
                             "error": str(err)[:300] if err else None})
        out["glue_job_runs"] = sorted(runs, key=lambda r: (str(r["job"]), str(r["started"]) == "None", str(r["started"]), str(r["run_id"])))
    except Exception as ex:
        errors.append("glue.jobs: " + describe_error(ex))
    if cfg.get("crawlers"):
        try:
            crawlers, t2 = _paginate(glue, "get_crawlers", "Crawlers", MAX_ITEMS)
            truncated = truncated or t2
            rows = []
            for c in crawlers:
                last = c.get("LastCrawl") or {}
                err = last.get("ErrorMessage")
                rows.append({"name": c.get("Name"), "state": c.get("State"), "last_status": last.get("Status"),
                             "last_crawl_at": _iso(last.get("StartTime")), "error": str(err)[:300] if err else None})
            out["glue_crawlers"] = sorted(rows, key=lambda r: str(r["name"]))
        except Exception as ex:
            errors.append("glue.crawlers: " + describe_error(ex))
    dbs = cfg.get("catalog_databases") or []
    if dbs:
        rows = []
        for db in dbs:
            try:
                tables, t3 = _paginate(glue, "get_tables", "TableList", MAX_ITEMS, DatabaseName=db)
                truncated = truncated or t3
            except Exception as ex:
                errors.append("glue.catalog %s: %s" % (db, describe_error(ex)))
                continue
            for t in tables:
                keys = [k.get("Name") for k in (t.get("PartitionKeys") or [])]
                row = {"database": db, "name": t.get("Name"),
                       "location": (t.get("StorageDescriptor") or {}).get("Location"),
                       "partition_keys": keys, "format": (t.get("Parameters") or {}).get("classification")}
                if keys:
                    row.update(_partition_info(glue, db, t.get("Name"), errors))
                rows.append({k: v for k, v in row.items() if v is not None})
        out["glue_tables"] = sorted(rows, key=lambda r: (r["database"], str(r["name"])))
    return out, truncated


def _partition_info(glue, db, table, errors):
    """Newest partition registration time (capped scan; `partitions_truncated` when the cap was hit)."""
    try:
        parts, trunc = _paginate(glue, "get_partitions", "Partitions", MAX_PARTITIONS, DatabaseName=db, TableName=table)
    except Exception as ex:
        errors.append("glue.partitions %s.%s: %s" % (db, table, describe_error(ex)))
        return {}
    times = [_utc(p.get("CreationTime")) for p in parts if p.get("CreationTime")]
    info = {"partitions": len(parts), "partitions_truncated": trunc}
    if times:
        info["last_partition_added"] = _iso(max(times))
    return info


# ---- S3 -----------------------------------------------------------------------------------------------------------

def parse_s3_uri(uri):
    m = re.match(r"^s3://([^/]+)/?(.*)$", uri or "")
    if not m:
        raise ValueError("not an s3:// prefix: %r" % uri)
    return m.group(1), m.group(2)


def gather_s3(session, cfg, now, metrics, errors):
    s3 = session.client("s3")
    prefixes = cfg.get("prefixes") or []
    rows, specs, whole = [], [], {}
    for uri in prefixes:
        try:
            bucket, prefix = parse_s3_uri(uri)
        except ValueError as ex:
            errors.append("s3: %s" % ex)
            continue
        row = {"prefix": uri, "bucket": bucket}
        if not prefix:
            whole[uri] = row
            specs += [((uri, "bytes"), "AWS/S3", "BucketSizeBytes",
                       {"BucketName": bucket, "StorageType": "StandardStorage"}, "Average", 86400),
                      ((uri, "count"), "AWS/S3", "NumberOfObjects",
                       {"BucketName": bucket, "StorageType": "AllStorageTypes"}, "Average", 86400)]
        rows.append(row)
    series = {}
    if specs:
        try:
            series = metrics.fetch(specs, window_h=S3_METRIC_WINDOW_H)
        except Exception as ex:
            errors.append("s3.metrics: " + describe_error(ex))
    for row in rows:
        uri = row["prefix"]
        bucket, prefix = parse_s3_uri(uri)
        count = size = None
        newest = None
        truncated = False
        listed = 0
        try:
            token = None
            for _ in range(MAX_PAGES):
                kwargs = {"Bucket": bucket, "Prefix": prefix}
                if token:
                    kwargs["ContinuationToken"] = token
                resp = s3.list_objects_v2(**kwargs)
                for o in resp.get("Contents") or []:
                    listed += 1
                    size = (size or 0) + int(o.get("Size") or 0)
                    lm = _utc(o.get("LastModified"))
                    if lm and (newest is None or lm > newest):
                        newest = lm
                token = resp.get("NextContinuationToken")
                if not resp.get("IsTruncated") or not token:
                    break
            else:
                truncated = True
            count = listed
        except Exception as ex:
            errors.append("s3.list %s: %s" % (uri, describe_error(ex)))
            row.update({"object_count": None, "bytes": None, "newest_at": None, "source": "error"})
            continue
        if uri in whole and series.get((uri, "count")) and series.get((uri, "bytes")):
            count, size, source = int(series[(uri, "count")][-1]), int(series[(uri, "bytes")][-1]), "cloudwatch"
        else:
            source = "list"
        row.update({"object_count": count, "bytes": size or 0, "newest_at": _iso(newest), "source": source,
                    "newest_complete": not truncated})
    return {"s3_prefixes": sorted(rows, key=lambda r: r["prefix"])}


# ---- Athena -------------------------------------------------------------------------------------------------------

def gather_athena(session, cfg, now, errors):
    athena = session.client("athena")
    since = now - timedelta(hours=WINDOW_H)
    wg_rows, top_rows, fail_rows = [], [], []
    for wg in cfg.get("workgroups") or ["primary"]:
        queries = []
        try:
            done = False
            for page in athena.get_paginator("list_query_executions").paginate(WorkGroup=wg):
                ids = page.get("QueryExecutionIds") or []
                for i in range(0, len(ids), 50):
                    got = athena.batch_get_query_execution(QueryExecutionIds=ids[i:i + 50])
                    for q in got.get("QueryExecutions") or []:
                        sub = _utc((q.get("Status") or {}).get("SubmissionDateTime"))
                        if sub and sub >= since:
                            queries.append(q)
                        elif sub:
                            done = True  # newest first: everything after this is older than the window
                    if done:
                        break
                if done or len(queries) >= 1000:
                    break
        except Exception as ex:
            errors.append("athena.%s: %s" % (wg, describe_error(ex)))
            continue
        states = {}
        scanned = 0
        for q in queries:
            st = (q.get("Status") or {}).get("State") or "UNKNOWN"
            states[st] = states.get(st, 0) + 1
            scanned += int((q.get("Statistics") or {}).get("DataScannedInBytes") or 0)
        wg_rows.append({"workgroup": wg, "queries": len(queries), "succeeded": states.get("SUCCEEDED", 0),
                        "failed": states.get("FAILED", 0), "cancelled": states.get("CANCELLED", 0),
                        "scanned_bytes": scanned})
        for q in sorted(queries, key=lambda q: -int((q.get("Statistics") or {}).get("DataScannedInBytes") or 0))[:5]:
            top_rows.append({"workgroup": wg, "query_id": q.get("QueryExecutionId"),
                             "scanned_bytes": int((q.get("Statistics") or {}).get("DataScannedInBytes") or 0),
                             "state": (q.get("Status") or {}).get("State"),
                             "query": " ".join(str(q.get("Query") or "").split())[:200]})
        for q in queries:
            st = q.get("Status") or {}
            if st.get("State") == "FAILED":
                fail_rows.append({"workgroup": wg, "query_id": q.get("QueryExecutionId"),
                                  "reason": str(st.get("StateChangeReason") or "")[:300],
                                  "at": _iso(st.get("SubmissionDateTime"))})
    return {"athena_workgroups": sorted(wg_rows, key=lambda r: r["workgroup"]),
            "athena_top_queries": sorted(top_rows, key=lambda r: (r["workgroup"], -r["scanned_bytes"], str(r["query_id"]))),
            "athena_failures": sorted(fail_rows, key=lambda r: (r["workgroup"], str(r["at"]), str(r["query_id"])))}


# ---- Firehose -----------------------------------------------------------------------------------------------------

def gather_firehose(session, cfg, now, metrics, errors):
    fh = session.client("firehose")
    names = []
    try:
        start = None
        for _ in range(MAX_PAGES):
            resp = fh.list_delivery_streams(Limit=100, **({"ExclusiveStartDeliveryStreamName": start} if start else {}))
            got = resp.get("DeliveryStreamNames") or []
            names += got
            if not resp.get("HasMoreDeliveryStreams") or not got:
                break
            start = got[-1]
    except Exception as ex:
        errors.append("firehose.list: " + describe_error(ex))
        return {}, False
    truncated = len(names) > MAX_ITEMS
    rows, specs = [], []
    for name in sorted(names)[:MAX_ITEMS]:
        try:
            d = fh.describe_delivery_stream(DeliveryStreamName=name)["DeliveryStreamDescription"]
        except Exception as ex:
            errors.append("firehose.describe %s: %s" % (name, describe_error(ex)))
            continue
        dest = (d.get("Destinations") or [{}])[0]
        s3d = dest.get("ExtendedS3DestinationDescription") or dest.get("S3DestinationDescription") or {}
        hints = s3d.get("BufferingHints") or {}
        bucket = s3d["BucketARN"].split(":::", 1)[-1] if s3d.get("BucketARN") else None
        rows.append({"stream": name, "status": d.get("DeliveryStreamStatus"), "bucket": bucket,
                     "prefix": s3d.get("Prefix"), "buffer_interval_s": hints.get("IntervalInSeconds"),
                     "buffer_mb": hints.get("SizeInMBs")})
        dims = {"DeliveryStreamName": name}
        specs += [((name, "ok"), "AWS/Firehose", "DeliveryToS3.Success", dims, "Average", HOUR),
                  ((name, "fresh"), "AWS/Firehose", "DeliveryToS3.DataFreshness", dims, "p95", HOUR),
                  ((name, "throttled"), "AWS/Firehose", "ThrottledRecords", dims, "Sum", HOUR)]
    try:
        series = metrics.fetch(specs) if specs else {}
    except Exception as ex:
        errors.append("firehose.metrics: " + describe_error(ex))
        series = {}
    for r in rows:
        ok, fresh, thr = (series.get((r["stream"], k)) for k in ("ok", "fresh", "throttled"))
        r["delivery_success"] = _r(min(ok), 4) if ok else None
        r["freshness_p95_s"] = _r(max(fresh)) if fresh else None
        r["throttled_records"] = int(sum(thr)) if thr else None
    return {"firehose_streams": sorted(rows, key=lambda r: r["stream"])}, truncated


# ---- CloudWatch alarms --------------------------------------------------------------------------------------------

def gather_cloudwatch(session, cfg, now, errors):
    cw = session.client("cloudwatch")
    out = {}
    if cfg.get("alarms", True):
        try:
            alarms, _ = _paginate(cw, "describe_alarms", "MetricAlarms", MAX_ITEMS, StateValue="ALARM",
                                  AlarmTypes=["MetricAlarm"])
            out["cloudwatch_alarms"] = sorted(
                ({"name": a.get("AlarmName"), "namespace": a.get("Namespace"), "metric": a.get("MetricName"),
                  "since": _iso(a.get("StateUpdatedTimestamp")),
                  "reason": " ".join(str(a.get("StateReason") or "").split())[:200]} for a in alarms),
                key=lambda r: str(r["name"]))
        except Exception as ex:
            errors.append("cloudwatch.alarms: " + describe_error(ex))
    return out


# ---- orchestration ------------------------------------------------------------------------------------------------

SERVICES = ("dms", "glue", "s3", "athena", "firehose", "cloudwatch")


def gather(cfg, session=None, now=None):
    """cfg: {aws, services, options}. Returns {"data": {...}, "errors": [...]}; one failing service leaves the rest."""
    now = now or datetime.now(timezone.utc)
    services = cfg.get("services") or {}
    opts = cfg.get("options") or {}
    cap = int(opts.get("max_metrics", DEFAULT_MAX_METRICS))
    errors = []
    data = {}
    truncated = []
    if session is None:
        session = make_session(cfg.get("aws") or {})
    metrics = Metrics(session, now, WINDOW_H, cap)
    if "dms" in services:
        try:
            part, trunc = gather_dms(session, services["dms"] or {}, now, metrics, errors)
            data.update(part)
            if trunc:
                truncated.append("dms")
        except Exception as ex:
            errors.append("dms: " + describe_error(ex))
    if "glue" in services:
        try:
            part, trunc = gather_glue(session, services["glue"] or {}, now, errors)
            data.update(part)
            if trunc:
                truncated.append("glue")
        except Exception as ex:
            errors.append("glue: " + describe_error(ex))
    if "s3" in services:
        try:
            data.update(gather_s3(session, services["s3"] or {}, now, metrics, errors))
        except Exception as ex:
            errors.append("s3: " + describe_error(ex))
    if "athena" in services:
        try:
            data.update(gather_athena(session, services["athena"] or {}, now, errors))
        except Exception as ex:
            errors.append("athena: " + describe_error(ex))
    if "firehose" in services:
        try:
            part, trunc = gather_firehose(session, services["firehose"] or {}, now, metrics, errors)
            data.update(part)
            if trunc:
                truncated.append("firehose")
        except Exception as ex:
            errors.append("firehose: " + describe_error(ex))
    if "cloudwatch" in services:
        try:
            data.update(gather_cloudwatch(session, services["cloudwatch"] or {}, now, errors))
        except Exception as ex:
            errors.append("cloudwatch: " + describe_error(ex))
    data["meta"] = {
        "collected_at": _iso(now), "window_h": WINDOW_H, "services": [s for s in SERVICES if s in services],
        "max_metrics": cap, "metrics_requested": metrics.requested, "metrics_skipped": metrics.skipped,
        "metric_calls": metrics.calls, "truncated": truncated,
        "max_age_h": float(opts.get("max_age_h", 26)), "max_scan_gb": float(opts.get("max_scan_gb", 100)),
    }
    return {"data": data, "errors": errors}


def caller_identity(session):
    """(arn with the account masked, account masked) via sts get-caller-identity."""
    ident = session.client("sts").get_caller_identity()
    return mask_ids(str(ident.get("Arn", ""))), mask_ids(str(ident.get("Account", "")))


def remote_main(cfg):
    """Entry point on a remote host: instance role, no profile; returns the gather() dict or an error dict."""
    try:
        return gather(cfg)
    except AwsUnavailable as ex:
        return {"data": {}, "errors": [str(ex)]}
    except Exception as ex:
        return {"data": {}, "errors": [describe_error(ex)]}


def remote_doctor(cfg=None):
    """Remote check for `dea doctor`: boto3 importable and sts get-caller-identity works."""
    try:
        session = make_session({"instance_role": True})
    except AwsUnavailable as ex:
        return {"ok": False, "detail": str(ex)}
    try:
        arn, _ = caller_identity(session)
    except Exception as ex:
        return {"ok": False, "detail": describe_error(ex)}
    return {"ok": True, "detail": arn}


if __name__ == "__main__":  # python -W ignore -m de_agent.collectors.aws._aws_core > docs/aws-iam-readonly.json
    print(json.dumps(iam_policy(), indent=2))
