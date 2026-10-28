"""dbt collector (M1b section 4): reads manifest.json, run_results.json and sources.json from `target_dir`.

`exec: local` reads files from this machine (a relative target_dir resolves against the hub); `exec: ssh:<host>` reads
them with `ssh <host> cat <file>`. A missing file is a section error, never a crash. Manifest schema v10-v12
(dbt 1.6-1.9) is supported; an unknown version is read best effort and reported as a warning.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from de_agent.collectors.base import CollectorError, RunContext, utc_iso
from de_agent.hubpath import find_hub

OPTION_KEYS = {"target_dir"}
SUPPORTED_MANIFEST = (10, 11, 12)
SSH_BASE = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
FAIL_STATES = {"error", "fail", "failed"}
OK_STATES = {"success", "pass"}
MAX_OK_IDS = 2000


def validate_options(options: dict[str, Any]) -> None:
    unknown = set(options) - OPTION_KEYS
    if unknown:
        raise ValueError(f"unknown dbt options: {', '.join(sorted(unknown))}")
    if not isinstance(options.get("target_dir"), str) or not options["target_dir"]:
        raise ValueError("options.target_dir is required (the dbt `target/` directory)")


def validate_source(source: Any) -> None:
    if source.conn is not None:
        raise ValueError("dbt sources take no `conn:`; use options.target_dir")


def _read(source: Any, name: str, ctx: RunContext) -> dict[str, Any]:
    target = source.options["target_dir"]
    if source.exec.startswith("ssh:"):
        try:
            p = subprocess.run([*SSH_BASE, source.exec[4:], "cat", shlex.quote(f"{target.rstrip('/')}/{name}")],
                               capture_output=True, text=True, timeout=max(5, ctx.remaining()))
        except subprocess.TimeoutExpired:
            raise CollectorError(f"{name}: ssh timed out") from None
        if p.returncode != 0:
            raise CollectorError(f"{name}: not found or unreadable on {source.exec[4:]}")
        text = p.stdout
    else:
        base = Path(target).expanduser()
        if not base.is_absolute():
            base = (find_hub() or Path.cwd()) / base
        try:
            text = (base / name).read_text(encoding="utf-8")
        except OSError:
            raise CollectorError(f"{name}: not found in {base}") from None
    try:
        doc = json.loads(text)
    except ValueError:
        raise CollectorError(f"{name}: not valid JSON") from None
    if not isinstance(doc, dict):
        raise CollectorError(f"{name}: unexpected content")
    return doc


def _schema_version(doc: dict[str, Any]) -> int | None:
    m = re.search(r"/v(\d+)\.json$", str((doc.get("metadata") or {}).get("dbt_schema_version", "")))
    return int(m.group(1)) if m else None


def _parse_ts(v: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _model_folder(node: dict[str, Any]) -> str:
    parts = str(node.get("path") or node.get("original_file_path") or "").split("/")
    return parts[-2] if len(parts) > 1 else "."


def _guarded(manifest: dict[str, Any] | None, test_id: str) -> str | None:
    node = ((manifest or {}).get("nodes") or {}).get(test_id) or {}
    att = node.get("attached_node")
    if att:
        return str(att)
    for dep in (node.get("depends_on") or {}).get("nodes") or []:
        if str(dep).startswith(("model.", "seed.", "snapshot.")):
            return str(dep)
    return None


def _round(v: Any) -> float | None:
    return round(float(v), 2) if isinstance(v, (int, float)) else None


class DbtCollector:
    type = "dbt"
    schema_version = 1
    list_keys = {
        "models": ["folder", "materialization"],
        "failing_nodes": ["unique_id"],
        "slowest": ["unique_id"],
        "tests": ["unique_id"],
        "freshness": ["unique_id"],
        "exposures": ["name"],
    }
    ranking_sections = {"slowest"}  # a top-10 ranking: entering/leaving it is not news, a slower model is
    volatile_fields = {"age_s", "invocation_time", "elapsed_s", "message", "generated_at", "max_loaded_at"}
    validate_options = staticmethod(validate_options)
    validate_source = staticmethod(validate_source)

    def ping(self, source: Any, ctx: RunContext) -> str:
        doc = _read(source, "manifest.json", ctx)
        return f"manifest.json readable (dbt {(doc.get('metadata') or {}).get('dbt_version', '?')})"

    def collect(self, source: Any, ctx: RunContext) -> dict[str, Any]:
        data: dict[str, Any] = {}
        errors: list[str] = []
        docs: dict[str, dict[str, Any] | None] = {}
        for name in ("manifest.json", "run_results.json", "sources.json"):
            try:
                docs[name] = _read(source, name, ctx)
            except CollectorError as ex:
                docs[name] = None
                errors.append(str(ex))
        manifest, results, sources = docs["manifest.json"], docs["run_results.json"], docs["sources.json"]
        meta: dict[str, Any] = {}
        if manifest is not None:
            self._manifest(manifest, data, errors)
        if results is not None:
            self._results(results, manifest, data, meta)
        if sources is not None:
            self._freshness(sources, data)
        meta["collected_at"] = utc_iso()
        data["meta"] = meta
        return {"data": data, "errors": errors}

    def _manifest(self, m: dict[str, Any], data: dict[str, Any], errors: list[str]) -> None:
        md = m.get("metadata") or {}
        ver = _schema_version(m)
        if ver not in SUPPORTED_MANIFEST:
            errors.append(f"manifest.json: unknown schema version {ver!r} (supported v10-v12); read best effort")
        data["project"] = {"name": md.get("project_name"), "dbt_version": md.get("dbt_version"),
                           "adapter": md.get("adapter_type"), "generated_at": md.get("generated_at"),
                           "manifest_schema": ver}
        counts: dict[tuple[str, str], int] = {}
        for node in (m.get("nodes") or {}).values():
            if node.get("resource_type") != "model":
                continue
            key = (_model_folder(node), str((node.get("config") or {}).get("materialized", "view")))
            counts[key] = counts.get(key, 0) + 1
        data["models"] = [{"folder": f, "materialization": mat, "count": n} for (f, mat), n in sorted(counts.items())]
        data["exposures"] = sorted(
            ({"name": e.get("name"), "depends_on": sorted((e.get("depends_on") or {}).get("nodes") or [])}
             for e in (m.get("exposures") or {}).values()), key=lambda r: str(r["name"]))

    def _results(self, rr: dict[str, Any], manifest: dict[str, Any] | None, data: dict[str, Any],
                 meta: dict[str, Any]) -> None:
        rows = rr.get("results") or []
        counts: dict[str, int] = {}
        for r in rows:
            counts[str(r.get("status"))] = counts.get(str(r.get("status")), 0) + 1
        gen = (rr.get("metadata") or {}).get("generated_at")
        meta["last_run_at"] = gen
        data["last_run"] = {
            "invocation_time": gen, "command": (rr.get("args") or {}).get("which"),
            "elapsed_s": _round(rr.get("elapsed_time")), "nodes": len(rows),
            **{f"n_{k}": v for k, v in sorted(counts.items())},
        }
        ok_ids = sorted(str(r.get("unique_id")) for r in rows if str(r.get("status")) in OK_STATES)
        data["last_run"]["ok_ids"] = ok_ids[:MAX_OK_IDS]  # lets `dea diff` tell "recovered" from "not run again"
        failing, tests = [], []
        for r in rows:
            uid, status = str(r.get("unique_id")), str(r.get("status"))
            if uid.startswith("test."):
                if status in FAIL_STATES or status == "warn":
                    tests.append({"unique_id": uid, "status": status, "failures": r.get("failures"),
                                  "model": _guarded(manifest, uid), "message": str(r.get("message") or "")[:300]})
            elif status in FAIL_STATES:
                failing.append({"unique_id": uid, "status": status, "message": str(r.get("message") or "")[:300],
                                "execution_s": _round(r.get("execution_time"))})
        data["failing_nodes"] = sorted(failing, key=lambda x: x["unique_id"])
        data["tests"] = sorted(tests, key=lambda x: x["unique_id"])
        slow = sorted(((_round(r.get("execution_time")) or 0.0, str(r.get("unique_id"))) for r in rows),
                      key=lambda t: (-t[0], t[1]))[:10]
        data["slowest"] = [{"unique_id": u, "execution_s": s} for s, u in slow]

    def _freshness(self, src: dict[str, Any], data: dict[str, Any]) -> None:
        out = []
        for r in src.get("results") or []:
            out.append({"unique_id": r.get("unique_id"), "status": r.get("status"),
                        "max_loaded_at": r.get("max_loaded_at"), "age_s": _round(r.get("max_loaded_at_time_ago_in_s"))})
        data["freshness"] = sorted(out, key=lambda x: str(x["unique_id"]))
