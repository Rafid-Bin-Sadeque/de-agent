"""Hub lint rules (M1a section 8). `dea lint` exits 1 on any error."""
from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from de_agent.config import Config, ConfigError, load_config, parse_config
from de_agent.scanners.headers import block_lines, marker_state
from de_agent.secretscan import find_secret

LINE_BUDGETS = {"domains": 60, "projects": 150}
CONVENTIONS_BUDGET = 150
STALE_GRACE_DAYS = 14
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WIKILINK_RE = re.compile(r"\[\[([^\]\n]+)\]\]")
FENCE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
DONE_STATES = {"done", "closed", "resolved", "complete", "completed", "x"}
SKIP_DIRS = {".git", ".dea", "node_modules", "__pycache__"}
TEXT_EXT = {".md", ".yaml", ".yml", ".txt", ".json", ".sql", ".env", ".cfg", ".ini", ".toml", ".sh", ".py", ""}


@dataclass
class Finding:
    rule: str
    level: str  # error | warning
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.S)
    if not m:
        return {}, text
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    return (data if isinstance(data, dict) else {}), text[m.end():]


def _pages(hub: Path) -> list[Path]:
    out = []
    for p in sorted(hub.rglob("*.md")):
        rel = p.relative_to(hub)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        out.append(p)
    return out


def _is_catalog(hub: Path, p: Path) -> bool:
    return p.relative_to(hub).parts[0] == "catalog"


def _rel(hub: Path, p: Path) -> str:
    return p.relative_to(hub).as_posix()


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def link_targets(text: str) -> list[str]:
    stripped = FENCE_RE.sub("", text)
    out = []
    for raw in WIKILINK_RE.findall(stripped):
        name = raw.split("|")[0].split("#")[0].strip()
        if name:
            out.append(name)
    return out


def table_rows(text: str) -> list[tuple[list[str], list[str]]]:
    """Return (header_cells, row_cells) for every data row of every markdown table."""
    out: list[tuple[list[str], list[str]]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            header = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                out.append((header, [c.strip() for c in lines[i].strip().strip("|").split("|")]))
                i += 1
            continue
        i += 1
    return out


def _strip_link(cell: str) -> str:
    m = WIKILINK_RE.search(cell)
    return (m.group(1).split("|")[0].split("#")[0] if m else cell).strip()


def _to_date(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and DATE_RE.match(v.strip()):
        try:
            return date.fromisoformat(v.strip())
        except ValueError:
            return None
    return None


def status_rows(text: str, today: date) -> list[dict[str, Any]]:
    """Rows of tables that have a `Due` (or `Date`) and a `State` column."""
    rows = []
    for header, cells in table_rows(text):
        low = [h.lower() for h in header]
        di = next((k for k, h in enumerate(low) if h in ("due", "date")), None)
        si = next((k for k, h in enumerate(low) if h in ("state", "status")), None)
        if di is None or si is None or len(cells) <= max(di, si):
            continue
        d = _to_date(cells[di])
        if d is None:
            continue
        done = cells[si].strip().lower() in DONE_STATES
        rows.append({"due": d, "state": cells[si], "done": done, "cells": cells, "overdue": d < today and not done})
    return rows


def _load_cfg(hub: Path, out: list[Finding]) -> Config:
    path = hub / "de-agent.yaml"
    if not path.exists():
        return parse_config("")
    try:
        return load_config(path)
    except ConfigError as ex:
        out.append(Finding("config-invalid", "error", "de-agent.yaml", str(ex)))
        return parse_config("")


def run_lint(hub: Path, today: date | None = None, now: datetime | None = None) -> list[Finding]:
    today = today or date.today()
    now = now or datetime.now(timezone.utc)
    out: list[Finding] = []
    cfg = _load_cfg(hub, out)

    pages = _pages(hub)
    by_dir: dict[str, list[Path]] = {}
    for p in pages:
        parts = p.relative_to(hub).parts
        by_dir.setdefault(parts[0] if len(parts) > 1 else "", []).append(p)
    real = lambda ps: [p for p in ps if not p.name.startswith("_")]  # noqa: E731  (skip _template.md)

    # map-budget
    map_path = hub / "map.md"
    if not map_path.exists():
        out.append(Finding("map-budget", "error", "map.md", "map.md is missing"))
    else:
        tokens = (len(_read(map_path)) + 3) // 4
        if tokens > cfg.hub.map_budget_tokens:
            out.append(Finding("map-budget", "error", "map.md",
                               f"map.md is ~{tokens} tokens, budget {cfg.hub.map_budget_tokens}; split it by domain"))

    # generated-markers: a start marker without an end marker (or vice versa) would corrupt the next `dea scan`
    for p in real(by_dir.get("projects", [])):
        if marker_state(_read(p)) == "broken":
            out.append(Finding("generated-markers", "error", _rel(hub, p),
                               "unbalanced <!-- dea:generated:start/end --> markers (one without its pair, or out of order)"))

    # page-budget (the generated header block is exempt: it is not human text)
    for sub, limit in LINE_BUDGETS.items():
        for p in real(by_dir.get(sub, [])):
            text = _read(p)
            n = len(text.splitlines()) - block_lines(text)
            if n > limit:
                out.append(Finding("page-budget", "error", _rel(hub, p), f"{n} lines, budget {limit}"))
    conv = hub / "conventions.md"
    if conv.exists() and len(_read(conv).splitlines()) > CONVENTIONS_BUDGET:
        out.append(Finding("page-budget", "error", "conventions.md",
                           f"{len(_read(conv).splitlines())} lines, budget {CONVENTIONS_BUDGET}"))

    # broken-link
    stems = {p.stem for p in pages}
    for p in real(pages):
        if _is_catalog(hub, p):
            continue
        for name in dict.fromkeys(link_targets(_read(p))):
            if name.split("/")[-1].removesuffix(".md") not in stems:
                out.append(Finding("broken-link", "error", _rel(hub, p), f"[[{name}]] does not resolve to a hub page"))

    # orphan-project
    profiles = {p.stem for p in real(by_dir.get("projects", []))}
    domains = {p.stem for p in real(by_dir.get("domains", []))}
    referenced: set[str] = set()
    index_pages = [p for p in [map_path, *by_dir.get("domains", [])] if p.exists() and not p.name.startswith("_")]
    for p in index_pages:
        text = _read(p)
        referenced.update(n.split("/")[-1].removesuffix(".md") for n in link_targets(text))
        for _, cells in table_rows(text):
            if not cells or not cells[0]:
                continue
            name = _strip_link(cells[0])
            if re.fullmatch(r"[A-Za-z0-9_.-]+", name) and not name.startswith("_"):
                referenced.add(name)
                if name not in profiles and name not in domains:
                    out.append(Finding("orphan-project", "error", _rel(hub, p),
                                       f"'{name}' is listed but has no profile (projects/{name}.md)"))
    for name in sorted(profiles - referenced):
        out.append(Finding("orphan-project", "error", f"projects/{name}.md",
                           "profile is not listed in map.md or any domain page"))

    # profile-frontmatter + profile-stale
    for p in real(by_dir.get("projects", [])):
        fm, _ = parse_frontmatter(_read(p))
        missing = [k for k in ("owner", "domain") if not fm.get(k)]
        upd = fm.get("updated")
        d = _to_date(upd)
        if upd is None:
            missing.append("updated")
        elif d is None:
            out.append(Finding("profile-frontmatter", "error", _rel(hub, p), f"updated must be YYYY-MM-DD, got {upd!r}"))
        if missing:
            out.append(Finding("profile-frontmatter", "error", _rel(hub, p),
                               f"frontmatter missing: {', '.join(missing)}"))
        repo = fm.get("repo")
        if repo and d:
            last = _last_commit_date(hub, str(repo))
            if last and last > d + timedelta(days=STALE_GRACE_DAYS):
                out.append(Finding("profile-stale", "warning", _rel(hub, p),
                                   f"repo last commit {last} is more than {STALE_GRACE_DAYS} days after updated {d}"))

    # snapshot-stale
    for p in sorted((hub / "platform").glob("*.md")) if (hub / "platform").is_dir() else []:
        fm, _ = parse_frontmatter(_read(p))
        gen = fm.get("generated")
        gen_dt = gen if isinstance(gen, datetime) else _parse_ts(str(gen)) if gen else None
        if gen_dt is None:
            continue
        if gen_dt.tzinfo is None:
            gen_dt = gen_dt.replace(tzinfo=timezone.utc)
        age_h = (now - gen_dt).total_seconds() / 3600
        if age_h > cfg.hub.snapshot_max_age_hours:
            out.append(Finding("snapshot-stale", "warning", _rel(hub, p),
                               f"generated {age_h:.0f}h ago (max {cfg.hub.snapshot_max_age_hours:g}h): run `dea snapshot`"))

    # status-overdue
    status = hub / "status.md"
    if status.exists():
        for r in status_rows(_read(status), today):
            if r["overdue"]:
                out.append(Finding("status-overdue", "warning", "status.md",
                                   f"overdue since {r['due']}: {' | '.join(c for c in r['cells'] if c)[:100]}"))

    # secret-in-hub
    for p in sorted(hub.rglob("*")):
        rel = p.relative_to(hub)
        if not p.is_file() or any(part in SKIP_DIRS for part in rel.parts) or p.suffix not in TEXT_EXT:
            continue
        try:
            if p.stat().st_size > 1_000_000:
                continue
        except OSError:
            continue
        what = find_secret(_read(p))
        if what:
            out.append(Finding("secret-in-hub", "error", rel.as_posix(), f"{what}; hubs hold references, never values"))
    return out


def _parse_ts(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _last_commit_date(hub: Path, repo: str) -> date | None:
    path = Path(repo).expanduser()
    if not path.is_absolute():
        path = hub / path
    if not path.is_dir():
        return None
    try:
        p = subprocess.run(["git", "-C", str(path), "log", "-1", "--format=%cs"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _to_date(p.stdout.strip()) if p.returncode == 0 else None
