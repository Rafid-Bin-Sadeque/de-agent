"""AWS collector (M3 section 2): DMS, Glue, S3 (named prefixes only), Athena, Firehose, CloudWatch alarms.

One `type: aws` source with a `services:` map. Every call is read-only by construction (see `_aws_core`: a botocore
before-call guard). `exec: local` calls AWS from this machine with a named profile; `exec: ssh:<host>` ships `_aws_core`
over `ssh <host> python3 -` and uses the REMOTE instance role (no keys in config, ever).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from de_agent.collectors.aws import _aws_core as core
from de_agent.collectors.base import CollectorError, RunContext

OPTION_KEYS = {"max_metrics", "max_age_h", "max_scan_gb"}
AWS_KEYS = {"profile", "region", "instance_role"}
SERVICE_KEYS = {
    "dms": set(), "glue": {"jobs", "crawlers", "catalog_databases"}, "s3": {"prefixes"}, "athena": {"workgroups"},
    "firehose": set(), "cloudwatch": {"alarms"},
}
SSH_BASE = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


def _str_list(v: Any, what: str) -> None:
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise ValueError(f"{what} must be a list of strings")


def validate_options(options: dict[str, Any]) -> None:
    unknown = set(options) - OPTION_KEYS
    if unknown:
        raise ValueError(f"unknown aws options: {', '.join(sorted(unknown))}")
    for key in OPTION_KEYS:
        if key in options and not (isinstance(options[key], (int, float)) and not isinstance(options[key], bool)
                                   and options[key] > 0):
            raise ValueError(f"options.{key} must be a positive number")


def validate_aws_block(aws: Any, exec_target: str) -> None:
    if not isinstance(aws, dict):
        raise ValueError("`aws:` must be a mapping ({profile, region} or {instance_role: true})")
    unknown = set(aws) - AWS_KEYS
    if unknown:
        raise ValueError(f"unknown aws keys: {', '.join(sorted(unknown))} (credentials never go in config; "
                         "use a profile or an instance role)")
    for k in ("profile", "region"):
        if k in aws and not (isinstance(aws[k], str) and aws[k]):
            raise ValueError(f"aws.{k} must be a string")
    if "instance_role" in aws:
        if not isinstance(aws["instance_role"], bool):
            raise ValueError("aws.instance_role must be true or false")
        if aws["instance_role"] and not exec_target.startswith("ssh:"):
            raise ValueError("aws.instance_role: true needs exec ssh:<host> (the role belongs to that host)")
        if aws["instance_role"] and aws.get("profile"):
            raise ValueError("aws.instance_role and aws.profile are mutually exclusive")


def validate_source(source: Any) -> None:
    if source.conn is not None or source.data_api is not None:
        raise ValueError("aws sources take no `conn:`/`data_api:`; use aws: and services:")
    if source.aws is None:
        raise ValueError("aws sources need an `aws:` block ({profile, region} or {instance_role: true})")
    validate_aws_block(source.aws, source.exec)
    services = source.services
    if not services:
        raise ValueError("aws sources need a `services:` map (dms, glue, s3, athena, firehose, cloudwatch)")
    unknown = set(services) - set(SERVICE_KEYS)
    if unknown:
        raise ValueError(f"unknown aws services: {', '.join(sorted(unknown))}")
    for name, cfg in services.items():
        cfg = cfg or {}
        if not isinstance(cfg, dict):
            raise ValueError(f"services.{name} must be a mapping")
        bad = set(cfg) - SERVICE_KEYS[name]
        if bad:
            raise ValueError(f"unknown services.{name} keys: {', '.join(sorted(bad))}")
    g = services.get("glue") or {}
    if "jobs" in g:
        _str_list(g["jobs"], "services.glue.jobs")
    if "catalog_databases" in g:
        _str_list(g["catalog_databases"], "services.glue.catalog_databases")
    if "crawlers" in g and not isinstance(g["crawlers"], bool):
        raise ValueError("services.glue.crawlers must be true or false")
    s3 = services.get("s3")
    if s3 is not None:
        _str_list(s3.get("prefixes", []), "services.s3.prefixes")
        for uri in s3.get("prefixes", []):
            if not uri.startswith("s3://") or len(uri) <= 5:
                raise ValueError(f"services.s3.prefixes: {uri!r} must start with s3://bucket/ (named prefixes only)")
    if "workgroups" in (services.get("athena") or {}):
        _str_list(services["athena"]["workgroups"], "services.athena.workgroups")
    if "alarms" in (services.get("cloudwatch") or {}) and not isinstance(services["cloudwatch"]["alarms"], bool):
        raise ValueError("services.cloudwatch.alarms must be true or false")


def _remote_script(call: str, cfg: dict[str, Any]) -> str:
    src = Path(core.__file__).read_text(encoding="utf-8")
    return f"{src}\n\nimport json as _j\nprint(_j.dumps({call}(_j.loads({json.dumps(json.dumps(cfg))}))))\n"


def run_ssh(host: str, call: str, cfg: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    try:
        p = subprocess.run([*SSH_BASE, host, "python3", "-"], input=_remote_script(call, cfg), capture_output=True,
                           text=True, timeout=timeout_s + 10)
    except FileNotFoundError:
        raise CollectorError("ssh not found") from None
    except subprocess.TimeoutExpired:
        raise CollectorError(f"ssh {host} timed out after {int(timeout_s)}s") from None
    if p.returncode != 0:
        tail = "\n".join(p.stderr.strip().splitlines()[-3:])
        raise CollectorError(core.mask_ids(tail or f"ssh exited {p.returncode}"))
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise CollectorError("remote script did not return JSON") from None


class AwsCollector:
    type = "aws"
    schema_version = 1
    list_keys = {
        "dms_instances": ["id"], "dms_tasks": ["id"], "dms_table_issues": ["task", "schema", "table"],
        "dms_top_tables": ["task", "schema", "table"], "glue_job_runs": ["job", "run_id"], "glue_crawlers": ["name"],
        "glue_tables": ["database", "name"], "s3_prefixes": ["prefix"], "athena_workgroups": ["workgroup"],
        "athena_top_queries": ["workgroup", "query_id"], "athena_failures": ["workgroup", "query_id"],
        "firehose_streams": ["stream"], "cloudwatch_alarms": ["name"],
    }
    windowed_sections = {"glue_job_runs", "athena_failures"}
    ranking_sections = {"dms_top_tables", "athena_top_queries"}
    volatile_fields = {
        "cpu_avg", "cpu_max", "cpu_p95", "free_mem_min_bytes", "free_mem_min_pct", "swap_max_bytes", "swap_hours",
        "cdc_latency_source_p95_s", "cdc_latency_source_max_s", "cdc_latency_target_p95_s", "cdc_latency_target_max_s",
        "cdc_changes", "inserts", "updates", "deletes", "ddls", "full_load_rows", "tables_total", "newest_at",
        "delivery_success", "freshness_p95_s", "throttled_records", "scanned_bytes", "queries", "succeeded",
        "failed", "cancelled", "duration_s", "dpu_seconds", "started", "completed", "last_crawl_at", "since", "at",
        "reason", "error", "partitions", "last_partition_added", "last_failure_message", "query", "rules", "newest_complete",
    }
    validate_options = staticmethod(validate_options)
    validate_source = staticmethod(validate_source)

    def _cfg(self, source: Any) -> dict[str, Any]:
        return {"aws": dict(source.aws or {}), "services": {k: (v or {}) for k, v in source.services.items()},
                "options": dict(source.options)}

    def collect(self, source: Any, ctx: RunContext) -> dict[str, Any]:
        cfg = self._cfg(source)
        if source.exec.startswith("ssh:"):
            return run_ssh(source.exec[4:], "remote_main", cfg, ctx.remaining())
        try:
            session = ctx.aws_session or core.make_session(source.aws or {})
            return core.gather(cfg, session)
        except core.AwsUnavailable as ex:
            raise CollectorError(str(ex)) from None
        except core.ReadOnlyViolation as ex:  # pragma: no cover - a collector bug, never a user error
            raise CollectorError(str(ex)) from None

    def ping(self, source: Any, ctx: RunContext) -> str:
        if source.exec.startswith("ssh:"):
            res = run_ssh(source.exec[4:], "remote_doctor", {}, min(30, max(5, ctx.remaining())))
            if not res.get("ok"):
                raise CollectorError(f"remote: {res.get('detail')}")
            return f"boto3 importable, sts ok: {res['detail']}"
        try:
            session = ctx.aws_session or core.make_session(source.aws or {})
            arn, _ = core.caller_identity(session)
        except core.AwsUnavailable as ex:
            raise CollectorError(str(ex)) from None
        except Exception as ex:
            raise CollectorError(core.describe_error(ex)) from None
        return f"sts ok: {arn}"
