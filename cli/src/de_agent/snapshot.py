"""Orchestrate runner -> .dea/snapshots/<ts>/<source>.json + platform/<source>.md (M1a section 7)."""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from de_agent.collectors import get_collector
from de_agent.config import Config
from de_agent.render import HEADER, render_pages, signals_for
from de_agent.runner import result_status, run_sources

SNAP_RE = re.compile(r"^\d{8}T\d{6}Z$")


@dataclass
class SnapshotOutcome:
    snapshot_id: str
    results: list[dict[str, Any]]
    failed: list[str] = field(default_factory=list)


def snapshots_dir(hub: Path) -> Path:
    return hub / ".dea" / "snapshots"


def list_snapshots(hub: Path) -> list[str]:
    d = snapshots_dir(hub)
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir() and SNAP_RE.match(p.name))


def load_result(hub: Path, snapshot_id: str, source: str) -> dict[str, Any] | None:
    p = snapshots_dir(hub) / snapshot_id / f"{source}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _page_generated(path: Path) -> str | None:
    try:
        head = path.read_text(encoding="utf-8")[:600]
    except OSError:
        return None
    m = re.search(r"^generated:\s*(\S+)", head, re.M)
    return m.group(1) if m else None


def _write_index(hub: Path, cfg: Config, snaps: list[str]) -> None:
    rows = []
    for s in (x for x in cfg.sources if not x.writable):
        latest: dict[str, Any] | None = None
        for sid in reversed(snaps):
            latest = load_result(hub, sid, s.name)
            if latest:
                break
        page = hub / "platform" / f"{s.name}.md"
        good = _page_generated(page)
        if latest is None:
            rows.append(f"| {s.name} | {s.type} | never run | - | - | - |")
            continue
        status = result_status(latest)
        shown = latest["collected_at"] if status != "failed" else f"last good {good or 'never'}"
        link = f"[{s.name}]({s.name}.md)" if page.exists() else "-"
        rows.append(f"| {s.name} | {s.type} | {status} | {shown} | {len(signals_for(latest))} | {link} |")
    text = "\n".join([
        "---", "type: platform-index", "---", "", f"<!-- {HEADER} -->", "# Platform snapshot index", "",
        "| source | type | status | generated | signals | page |", "|---|---|---|---|---|---|", *rows, "",
    ])
    _write_atomic(hub / "platform" / "index.md", text)


def _write_pages(plat: Path, source: str, pages: dict[str, str]) -> None:
    """Write a source's page(s); a split AWS page lives in platform/<source>/, and leftovers of the other layout go."""
    for rel, text in pages.items():
        (plat / rel).parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(plat / rel, text)
    sub = plat / source
    if sub.is_dir():
        for p in sorted(sub.glob("*.md")):
            if f"{source}/{p.name}" not in pages:
                p.unlink()
        if not any(sub.iterdir()):
            sub.rmdir()


def _prune(hub: Path, keep: int) -> None:
    snaps = list_snapshots(hub)
    for sid in snaps[:-keep] if len(snaps) > keep else []:
        shutil.rmtree(snapshots_dir(hub) / sid, ignore_errors=True)


def take_snapshot(
    hub: Path,
    cfg: Config,
    only: list[str] | None = None,
    do_render: bool = True,
    query_fn: Callable[..., list[dict[str, Any]]] | None = None,
    now: datetime | None = None,
    deep: bool = False,
) -> SnapshotOutcome:
    chosen = [cfg.source(n) for n in only] if only else [s for s in cfg.sources if not s.writable]
    priors: dict[str, dict[str, Any]] = {}
    if not deep:  # latest usable result per source: collectors may reuse their heavy sections from it
        snaps = list_snapshots(hub)
        for s in chosen:
            good: list[dict[str, Any]] = []
            merge = getattr(get_collector(s.type), "best_prior", None)
            for sid in reversed(snaps):
                r = load_result(hub, sid, s.name)
                if r and result_status(r) != "failed":
                    good.append(r)
                    if merge is None:
                        break
            if good:
                priors[s.name] = merge(good) if merge else good[0]
    results = run_sources(chosen, query_fn=query_fn, priors=priors, deep=deep)

    now = now or datetime.now(timezone.utc)
    sid = now.strftime("%Y%m%dT%H%M%SZ")
    while (snapshots_dir(hub) / sid).exists():  # two runs in one second must not collide
        now += timedelta(seconds=1)
        sid = now.strftime("%Y%m%dT%H%M%SZ")
    sdir = snapshots_dir(hub) / sid
    sdir.mkdir(parents=True)
    plat = hub / "platform"
    plat.mkdir(exist_ok=True)

    failed: list[str] = []
    for r in results:
        (sdir / f"{r['source']}.json").write_text(
            json.dumps(r, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        status = result_status(r)
        if status == "failed":
            failed.append(r["source"])  # keep the last good platform page; never overwrite it with an error
        elif do_render:
            _write_pages(plat, r["source"], render_pages(r))
    if do_render:
        _write_index(hub, cfg, list_snapshots(hub))
        try:
            from de_agent.find import FindIndex

            FindIndex(hub).refresh()
        except Exception:  # indexing must never fail a snapshot
            pass
    _prune(hub, cfg.hub.retention)
    return SnapshotOutcome(sid, results, failed)
