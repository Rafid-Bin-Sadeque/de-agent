"""dbt scanner (M1c section 2): models, lineage, sources and exposures from `target/manifest.json`."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from de_agent.scanners.common import sha1


class ScanError(Exception):
    """A scan could not run (missing manifest, bad JSON). Reported, never fatal for other scans."""


class ManifestMissing(ScanError):
    """No manifest.json yet: the scan is skipped with a hint, not failed."""


def _relation(node: dict[str, Any]) -> str:
    schema = str(node.get("schema") or "").lower()
    name = str(node.get("alias") or node.get("identifier") or node.get("name") or "").lower()
    return f"{schema}.{name}" if schema else name


def manifest_path(scan: Any, root: Path) -> Path:
    t = Path(scan.target_dir)
    return (t if t.is_absolute() else root / t) / "manifest.json"


def scan_manifest(scan: Any, root: Path, revision: str) -> dict[str, Any]:
    mp = manifest_path(scan, root)
    try:
        raw = mp.read_bytes()
        m = json.loads(raw)
    except OSError:
        raise ManifestMissing(f"{mp} not found: run `dbt parse` (or `dbt compile`) so target/manifest.json exists") from None
    except ValueError:
        raise ScanError(f"{mp} is not valid JSON") from None
    nodes = m.get("nodes") or {}
    sources = m.get("sources") or {}
    rel_of: dict[str, str] = {}
    for uid, n in nodes.items():
        if n.get("resource_type") in ("model", "seed", "snapshot"):
            rel_of[uid] = _relation(n)
    for uid, s in sources.items():
        rel_of[uid] = _relation(s)
    models = []
    for uid, n in sorted(nodes.items()):
        if n.get("resource_type") != "model":
            continue
        parents = [p for p in (n.get("depends_on") or {}).get("nodes") or [] if p in rel_of]
        path = str(n.get("original_file_path") or n.get("path") or "")
        models.append({
            "unique_id": uid, "name": n.get("name"), "materialization": (n.get("config") or {}).get("materialized", "view"),
            "schema": n.get("schema"), "relation": rel_of[uid], "file": path,
            "folder": path.split("/")[-2] if "/" in path else ".",
            "parents": sorted(rel_of[p] for p in parents),
            "parent_ids": sorted(parents),
        })
    children: dict[str, set[str]] = {}
    for mod in models:
        for p in mod["parent_ids"]:
            children.setdefault(p, set()).add(mod["unique_id"])
    exposures = []
    for uid, e in sorted((m.get("exposures") or {}).items()):
        deps = [d for d in (e.get("depends_on") or {}).get("nodes") or [] if d in rel_of]
        exposures.append({"unique_id": uid, "name": e.get("name"), "reads": sorted(rel_of[d] for d in deps),
                          "file": str(e.get("original_file_path") or "")})
        for d in deps:
            children.setdefault(d, set()).add(uid)

    def descendants(uid: str) -> int:
        seen: set[str] = set()
        stack = list(children.get(uid, ()))
        while stack:
            c = stack.pop()
            if c not in seen:
                seen.add(c)
                stack.extend(children.get(c, ()))
        return len(seen)

    for mod in models:
        mod["downstream"] = descendants(mod["unique_id"])
        del mod["parent_ids"]
    src_out = [{"unique_id": uid, "name": s.get("name"), "source": s.get("source_name"), "relation": rel_of[uid]}
               for uid, s in sorted(sources.items())]
    md = m.get("metadata") or {}
    return {"type": "dbt", "revision": revision, "manifest_sha": sha1(raw)[:12],
            "dbt_version": md.get("dbt_version"), "project_name": md.get("project_name"),
            "models": models, "sources": src_out, "exposures": exposures,
            "stats": {"models": len(models), "sources": len(src_out), "exposures": len(exposures)}}


def manifest_hash(scan: Any, root: Path) -> dict[str, str]:
    mp = manifest_path(scan, root)
    try:
        return {mp.name: sha1(mp.read_bytes())}
    except OSError:
        return {}
