"""`type: http` source: runs an HTTP pack's GET (and read-only GraphQL POST) requests.

Enforcement lives in `_pack_http_core.request`, the only function that opens a socket: it accepts GET, or POST of a GraphQL
document without mutation/subscription, and raises before any network call for anything else. Over `exec: ssh:<host>` the same
stdlib module runs remotely and resolves the secret there; the local side never sees it.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from de_agent import sqlexec  # module, not names: sqlexec -> collectors -> this module is a cycle
from de_agent.collectors import _pack_http_core as core
from de_agent.collectors.base import CollectorError, RunContext
from de_agent.collectors.pack_base import PackCollector, apply_select, validate_for_config
from de_agent.config import ConfigError, resolve_secret
from de_agent.packs import template
from de_agent.packs.errors import PackError
from de_agent.packs.model import Pack, Section

AUTHS = ("none", "bearer", "basic", "header")


def secret_ref(source: Any) -> str | None:
    return source.password if source.auth == "basic" else source.token


def build_request(sec: Section, params: dict[str, Any], item: str | None, now: datetime) -> dict[str, Any]:
    if sec.get is not None:
        return {"kind": "get", "path": template.render(sec.get, params, item, now)}
    return {"kind": "graphql", "path": sec.path or "/graphql", "query": template.render(sec.graphql or "", params, item, now)}


def refuse(req: dict[str, Any]) -> str | None:
    """The request-level read-only check (also enforced inside core.request, which is what actually opens the socket)."""
    why = core.path_refusal(req["path"])
    if why:
        return why
    if req["kind"] == "graphql":
        return core.graphql_refusal(req["query"])
    return None if req["kind"] == "get" else "only GET and read-only GraphQL POST are allowed"


class HttpPackCollector(PackCollector):
    type = "http"

    def validate_source(self, source: Any) -> None:
        if source.conn is not None:
            raise ValueError("http sources take no `conn:`; use base_url / auth / token / password")
        url = urlparse(source.base_url or "")
        if url.scheme not in ("http", "https") or not url.netloc:
            raise ValueError("http sources need `base_url:` as an http(s) URL")
        auth = source.auth or "none"
        if auth not in AUTHS:
            raise ValueError("auth must be none, bearer, basic or header")
        if auth == "basic" and not (source.user and source.password):
            raise ValueError("auth basic needs `user:` and `password:` (a secret reference)")
        if auth in ("bearer", "header") and not source.token:
            raise ValueError(f"auth {auth} needs `token:` (a secret reference)")
        if auth == "header" and not source.header_name:
            raise ValueError("auth header needs `header_name:`")
        ref = secret_ref(source)
        if source.exec.startswith("ssh:") and ref and not (ref.startswith("env:") or ref.startswith("file:")):
            raise ValueError("exec ssh:<host> needs an env: or file: secret ref (resolved on the remote host)")
        if not source.exec.startswith("ssh:") and ref == "pgpass":
            raise ValueError("pgpass is not a valid secret ref for http sources")
        validate_for_config(source, self.type)

    def _headers(self, source: Any) -> dict[str, str]:
        ref = secret_ref(source)
        secret = None
        if (source.auth or "none") != "none" and ref:
            try:
                secret = resolve_secret(ref)
            except ConfigError as ex:
                raise CollectorError(str(ex)) from None
        hdr = core.build_auth_header(source.auth or "none", source.user, secret, source.header_name)
        return {hdr[0]: hdr[1]} if hdr else {}

    def _fetch(self, source: Any, req: dict[str, Any], timeout_s: float) -> Any:
        if source.exec.startswith("ssh:"):
            res = _run_ssh(source.exec[4:], {
                "base_url": source.base_url, "auth": source.auth or "none", "user": source.user,
                "secret_ref": secret_ref(source), "header_name": source.header_name, "timeout": min(20, timeout_s),
                "requests": [req]}, timeout_s)
            if "error" in res:
                raise CollectorError(core.redact_text(str(res["error"])))
            one = res["results"][0]
        else:
            one = core.fetch(source.base_url, [req], self._headers(source), min(20, timeout_s))[0]
        if not one.get("ok"):
            raise CollectorError(sqlexec.redact(str(one.get("error") or "request failed")))
        return one["body"]

    def run_section(self, state: Any, source: Any, ctx: RunContext, pack: Pack, sec: Section, item: str | None,
                    params: dict[str, Any], now: datetime) -> list[Any]:
        left = ctx.remaining()
        if left <= 0:
            raise CollectorError("source timeout budget exhausted")
        req = build_request(sec, params, item, now)
        why = refuse(req)  # BEFORE any socket is opened
        if why:
            raise PackError(f"refused: {why}")
        body = ctx.pack_fn(sec, item, req) if ctx.pack_fn is not None else self._fetch(source, req, left)
        if req["kind"] == "graphql" and isinstance(body, dict) and body.get("errors") and not body.get("data"):
            raise CollectorError("graphql: " + sqlexec.redact(str((body["errors"][0] or {}).get("message", "error")))[:200])
        return apply_select(body, sec)

    def ping_kind(self, source: Any, ctx: RunContext, pack: Pack) -> str:
        self._headers(source)  # raises when the secret cannot be resolved
        return f"pack {pack.pack} ok, credentials resolve ({source.auth or 'none'} auth, {urlparse(source.base_url).netloc})"


def _remote_script(cfg: dict[str, Any]) -> str:
    core_src = Path(core.__file__).read_text(encoding="utf-8")
    return f"{core_src}\n\nimport json as _j\nprint(_j.dumps(remote_main(_j.loads({json.dumps(json.dumps(cfg))}))))\n"


def _run_ssh(host: str, cfg: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    try:
        p = subprocess.run([*sqlexec.SSH_BASE, host, "python3", "-"], input=_remote_script(cfg), capture_output=True,
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
