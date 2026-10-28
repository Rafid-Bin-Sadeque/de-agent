"""Airflow collector (M1b section 3): REST API /api/v1 (2.x) or /api/v2 (3.x), GET-only.

`exec: local` calls the API from this machine. `exec: ssh:<host>` ships the stdlib-only `_airflow_core` module over
`ssh <host> python3 -` and calls the API from the remote host (base_url is as the REMOTE sees it, e.g.
http://localhost:8080). In that mode the secret ref must be `env:` or `file:` and is resolved ON THE REMOTE HOST: the
local side never reads or sends the secret.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from de_agent.collectors import _airflow_core as core
from de_agent.collectors.base import CollectorError, RunContext
from de_agent.config import ConfigError, resolve_secret

OPTION_KEYS = {"base_url", "auth", "user", "password", "token", "max_dags", "runs_per_dag", "fetch_logs"}
SSH_BASE = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


def validate_options(options: dict[str, Any]) -> None:
    unknown = set(options) - OPTION_KEYS
    if unknown:
        raise ValueError(f"unknown airflow options: {', '.join(sorted(unknown))}")
    url = options.get("base_url")
    if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
        raise ValueError("options.base_url must be an http(s) URL")
    auth = options.get("auth", "basic")
    if auth not in ("basic", "token", "none"):
        raise ValueError("options.auth must be basic, token or none")
    if auth == "basic" and not (options.get("user") and options.get("password")):
        raise ValueError("options.auth basic needs options.user and options.password (a secret ref)")
    if auth == "token" and not options.get("token"):
        raise ValueError("options.auth token needs options.token (a secret ref)")
    for key in ("max_dags", "runs_per_dag"):
        if key in options and not (isinstance(options[key], int) and options[key] > 0):
            raise ValueError(f"options.{key} must be a positive integer")
    if "fetch_logs" in options and not isinstance(options["fetch_logs"], bool):
        raise ValueError("options.fetch_logs must be true or false")


def validate_source(source: Any) -> None:
    if source.conn is not None:
        raise ValueError("airflow sources take no `conn:`; use options.base_url / auth / user / password")
    ref = source.options.get("token") if source.options.get("auth") == "token" else source.options.get("password")
    if source.exec.startswith("ssh:") and ref and not (ref.startswith("env:") or ref.startswith("file:")):
        raise ValueError("exec ssh:<host> needs an env: or file: secret ref (resolved on the remote host)")
    if not source.exec.startswith("ssh:") and ref == "pgpass":
        raise ValueError("pgpass is not a valid secret ref for airflow")


def _local_get(source: Any, ctx: RunContext) -> Callable[..., Any]:
    opts = source.options
    auth = opts.get("auth", "basic")
    secret = None
    ref = opts.get("token") if auth == "token" else opts.get("password")
    if auth != "none" and ref:
        try:
            secret = resolve_secret(ref)
        except ConfigError as ex:
            raise CollectorError(str(ex)) from None
    header = core.auth_header_for(opts, secret) if secret is not None else None
    base = core.make_urllib_get(opts["base_url"], header, timeout=min(20, max(1, int(ctx.remaining()))))
    return base


def _remote_script(cfg: dict[str, Any]) -> str:
    core_src = Path(core.__file__).read_text(encoding="utf-8")
    return f"{core_src}\n\nimport json as _j\nprint(_j.dumps(remote_main(_j.loads({json.dumps(json.dumps(cfg))}))))\n"


def _run_ssh(host: str, cfg: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    try:
        p = subprocess.run([*SSH_BASE, host, "python3", "-"], input=_remote_script(cfg), capture_output=True,
                           text=True, timeout=timeout_s + 10)
    except FileNotFoundError:
        raise CollectorError("ssh not found") from None
    except subprocess.TimeoutExpired:
        raise CollectorError(f"ssh {host} timed out after {int(timeout_s)}s") from None
    if p.returncode != 0:
        tail = "\n".join(p.stderr.strip().splitlines()[-3:])
        raise CollectorError(core.redact_text(tail or f"ssh exited {p.returncode}"))
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise CollectorError("remote script did not return JSON") from None


class AirflowCollector:
    type = "airflow"
    schema_version = 1
    list_keys = {
        "dags": ["dag_id"],
        "runs": ["dag_id", "run_id"],
        "failures_24h": ["dag_id", "task_id", "try"],
        "import_errors": ["filename"],
        "datasets": ["uri"],
        "pools": ["name"],
    }
    windowed_sections = {"runs", "failures_24h"}  # last-N runs / 24 h window: old rows age out
    volatile_fields = {"next_dagrun", "duration_s", "start", "end", "last_event", "log_tail", "occupied", "queued",
                       "stack"}
    validate_options = staticmethod(validate_options)
    validate_source = staticmethod(validate_source)

    def collect(self, source: Any, ctx: RunContext) -> dict[str, Any]:
        cfg = {k: v for k, v in source.options.items()}
        if source.exec.startswith("ssh:"):
            return _run_ssh(source.exec[4:], cfg, ctx.remaining())
        try:
            return core.gather(cfg, _local_get(source, ctx))
        except core.ApiError as ex:
            raise CollectorError(core.redact_text(str(ex))) from None

    def ping(self, source: Any, ctx: RunContext) -> str:
        if source.exec.startswith("ssh:"):
            return "remote API call is checked by dea snapshot"
        try:
            api, version = core.detect_api(_local_get(source, ctx))
        except core.ApiError as ex:
            raise CollectorError(str(ex)) from None
        return f"Airflow {version} ({api})"
