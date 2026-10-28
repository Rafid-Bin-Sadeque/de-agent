"""`dea scan` orchestration (M1c sections 5-6): incremental per-file parsing, catalog + profile headers + reindex."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from de_agent.scanners import airflow as af
from de_agent.scanners import catalog, headers
from de_agent.scanners import dbt as dbt_scan
from de_agent.scanners import sql as sql_scan
from de_agent.scanners.common import SCANNER_VERSION, last_commit, sha1, source_revision
from de_agent.scanners.edges import build_edges

STATE = "scan_state.json"
CACHE_DIR = "scan_cache"


@dataclass
class ScanOutcome:
    scans: list[dict[str, Any]] = field(default_factory=list)
    pages: int = 0
    profiles: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    edges: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return bool(self.errors)


def _sig(scan: Any) -> str:
    return sha1(json.dumps([SCANNER_VERSION, scan.model_dump()], sort_keys=True).encode())[:16]


def _load_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _dump(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def _per_file(scan: Any, files: dict[str, Path], cache: dict[str, Any], full: bool, parse: Any) -> tuple[
        dict[str, dict[str, Any]], dict[str, str], int, int]:
    """Hash every file; re-parse only those whose sha1 changed (or all with --full / a changed config)."""
    results: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    parsed = cached = 0
    old = {} if full or cache.get("sig") != _sig(scan) else cache.get("files", {})
    for rel, p in files.items():
        data = p.read_bytes()
        h = sha1(data)
        hashes[rel] = h
        ent = old.get(rel)
        if ent and ent["sha"] == h:
            results[rel] = ent["result"]
            cached += 1
        else:
            results[rel] = parse(data.decode("utf-8", errors="replace"), rel)
            parsed += 1
    return results, hashes, parsed, cached


def scan_one(hub: Path, scan: Any, full: bool) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Returns (assembled result or None on error, state entry). Never imports or runs scanned code."""
    t0 = time.monotonic()
    root = (hub / scan.path).resolve()
    state: dict[str, Any] = {"type": scan.type, "path": scan.path}
    if not root.is_dir():
        raise dbt_scan.ScanError(f"scan {scan.name}: path {scan.path} does not exist (relative to the hub)")
    cache_p = hub / ".dea" / CACHE_DIR / f"{scan.name}.files.json"
    cache = _load_json(cache_p) or {}
    result: dict[str, Any]
    if scan.type == "airflow":
        files, util, dag_rels = af.scan_files(scan, root)
        per_file, hashes, parsed, cached = _per_file(
            scan, files, cache, full, lambda src, rel: af.scan_python(src, rel, scan.dialect))
        sql_hash = {}
        for f in scan.dag_folders:
            base = root / f
            for p in sorted(base.rglob("*.sql")) if base.is_dir() else []:
                sql_hash[p.relative_to(root).as_posix()] = sha1(p.read_bytes())
        rev = source_revision(root, {**hashes, **sql_hash})
        result = af.assemble(scan, root, per_file, util, dag_rels, rev)
        _dump(cache_p, {"sig": _sig(scan), "files": {r: {"sha": hashes[r], "result": per_file[r]} for r in per_file}})
        state.update({"files": {**hashes, **sql_hash}, "parsed": parsed, "cached": cached})
    elif scan.type == "sql":
        files = {}
        for g in scan.globs:
            for p in sorted(root.glob(g)):
                if p.is_file():
                    files[p.relative_to(root).as_posix()] = p
        per_file, hashes, parsed, cached = _per_file(
            scan, files, cache, full, lambda src, rel: sql_scan.scan_sql_text(src, rel, scan.dialect))
        rev = source_revision(root, hashes)
        result = sql_scan.assemble(scan, per_file, rev)
        _dump(cache_p, {"sig": _sig(scan), "files": {r: {"sha": hashes[r], "result": per_file[r]} for r in per_file}})
        state.update({"files": hashes, "parsed": parsed, "cached": cached})
    else:
        mh = dbt_scan.manifest_hash(scan, root)
        rev = source_revision(root, mh)
        if mh and not rev.startswith("content."):  # target/ is untracked build output: its hash is part of the revision
            rev = f"{rev.split('+')[0]}+manifest.{next(iter(mh.values()))[:8]}"
        res = dbt_scan.scan_manifest(scan, root, rev)
        result = {**res, "scan": scan.name, "project": scan.project_name}
        state.update({"files": mh, "parsed": 1, "cached": 0})
    result.setdefault("scan", scan.name)
    result["project"] = scan.project_name
    state.update({"revision": result["revision"], "duration_s": round(time.monotonic() - t0, 3),
                  "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return result, state


def _write_if_changed(hub: Path, rel: str, text: str) -> bool:
    p = hub / rel
    data = text.encode("utf-8")
    try:
        if p.read_bytes() == data:
            return False
    except OSError:
        pass
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return True


def platform_results(hub: Path, cfg: Any) -> list[dict[str, Any]]:
    """Latest usable snapshot of every `type: aws` source (DMS / Firehose / Glue lineage edges read these)."""
    from de_agent.runner import result_status
    from de_agent.snapshot import list_snapshots, load_result

    snaps = list_snapshots(hub)
    out: list[dict[str, Any]] = []
    for s in cfg.sources:
        if s.type != "aws":
            continue
        for sid in reversed(snaps):
            r = load_result(hub, sid, s.name)
            if r and result_status(r) != "failed":
                out.append({**r, "snapshot_id": sid})
                break
    return out


def run_scan(hub: Path, cfg: Any, only: list[str] | None = None, full: bool = False) -> ScanOutcome:
    out = ScanOutcome()
    state_p = hub / ".dea" / STATE
    state = _load_json(state_p) or {"version": 1, "scans": {}}
    names = [s.name for s in cfg.scans]
    for n in only or []:
        if n not in names:
            out.errors.append(f"no such scan: {n} (configured: {', '.join(names) or 'none'})")
            return out
    results: dict[str, dict[str, Any]] = {}
    for scan in cfg.scans:
        res_p = hub / ".dea" / CACHE_DIR / f"{scan.name}.result.json"
        if only and scan.name not in only:
            cached = _load_json(res_p)
            if cached:
                results[scan.name] = cached
            continue
        try:
            res, st = scan_one(hub, scan, full)
        except dbt_scan.ManifestMissing as ex:
            msg = f"skipped: run dbt parse (or dbt compile) so the manifest exists ({ex})"
            out.warnings.append(f"{scan.name}: {msg}")
            out.scans.append({"name": scan.name, "type": scan.type, "status": "skipped", "error": msg})
            res_p.unlink(missing_ok=True)
            continue
        except dbt_scan.ScanError as ex:
            out.errors.append(str(ex))
            out.scans.append({"name": scan.name, "type": scan.type, "status": "error", "error": str(ex)})
            res_p.unlink(missing_ok=True)
            continue
        assert res is not None
        results[scan.name] = res
        _dump(res_p, res)
        state["scans"][scan.name] = st
        out.scans.append({"name": scan.name, "type": scan.type, "status": "ok", "files": len(st["files"]),
                          "parsed": st["parsed"], "cached": st["cached"], "duration_s": st["duration_s"],
                          "revision": st["revision"], "stats": res.get("stats", {})})
    ordered = [results[s.name] for s in cfg.scans if s.name in results]
    platform = platform_results(hub, cfg)
    edges = build_edges(ordered, platform)
    out.edges = edges
    rev = ",".join([f"{r['scan']}@{r['revision']}" for r in ordered]
                   + [f"{p['source']}@{p['snapshot_id']}" for p in platform]) or "none"
    pages: dict[str, str] = {}
    symbols: dict[str, Any] = {}
    for r in ordered:
        if r["type"] == "airflow":
            pages.update(catalog.render_airflow(r))
            pages.update(catalog.render_symbols(r))
            symbols[r["scan"]] = r["symbols"]
        elif r["type"] == "dbt":
            pages.update(catalog.render_dbt(r, r["scan"]))
        else:
            pages.update(catalog.render_sql(r))
    if ordered or platform:
        pages.update(catalog.render_edges(edges, rev))
        pages.update(catalog.render_symbols_json(symbols, rev))
    for rel, text in sorted(pages.items()):
        _write_if_changed(hub, rel, text)
    out.pages = len(pages)
    cat = hub / "catalog"
    if cat.is_dir():
        for p in sorted(cat.rglob("*")):
            rel = p.relative_to(hub).as_posix()
            if p.is_file() and p.suffix in (".md", ".json") and rel not in pages:
                p.unlink()
        for d in sorted((x for x in cat.rglob("*") if x.is_dir()), reverse=True):
            if not any(d.iterdir()):
                d.rmdir()
    _update_profiles(hub, cfg, ordered, edges, out)
    state["version"] = 1
    state["last_scan_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _dump(state_p, state)
    try:
        from de_agent.find import find_index_refresh, fts5_available

        if fts5_available():
            find_index_refresh(hub)
    except Exception as ex:  # indexing must never fail a scan
        out.errors.append(f"reindex failed: {ex}")
    return out


def _update_profiles(hub: Path, cfg: Any, ordered: list[dict[str, Any]], edges: dict[str, Any], out: ScanOutcome) -> None:
    by_project: dict[str, list[dict[str, Any]]] = {}
    scans = {s.name: s for s in cfg.scans}
    for r in ordered:
        by_project.setdefault(r["project"], []).append(r)
    prof_dir = hub / "projects"
    profiles = {p.stem for p in prof_dir.glob("*.md")} if prof_dir.is_dir() else set()
    for project, rs in sorted(by_project.items()):
        page = prof_dir / f"{project}.md"
        if project not in profiles:
            out.profiles[project] = f"no profile page (projects/{project}.md); header skipped"
            continue
        repos, links = {}, []
        for r in rs:
            sc = scans[r["scan"]]
            repos[r["scan"]] = {"path": sc.path, "commit": last_commit((hub / sc.path).resolve())}
            if r["type"] == "airflow":
                n = r["stats"]["dags"]
                href = f"../catalog/airflow/{r['scan']}" + ("/index.md" if n > catalog.SPLIT_OVER else ".md")
                links += [(f"airflow catalog: {r['scan']}", href), (f"symbols: {r['scan']}", f"../catalog/symbols/{r['scan']}.md")]
            elif r["type"] == "dbt":
                links.append((f"dbt catalog: {r['scan']}", f"../catalog/dbt/{r['scan']}.md"))
            else:
                links.append((f"sql catalog: {r['scan']}", f"../catalog/sql/{r['scan']}.md"))
        links.append(("edges (cross-project lineage)", "../catalog/edges.md"))
        block = headers.build_block(project, rs, edges, repos, profiles, links)
        out.profiles[project] = headers.rewrite_profile(page, block)
