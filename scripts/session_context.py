#!/usr/bin/env python3
"""SessionStart hook: inject map.md + due/overdue status rows + stale platform data (M1a section 11).

Stdlib only, Python 3.9 compatible. Must never fail a session: any error -> silent exit 0.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

MAX_CHARS = 6000
DONE = {"done", "closed", "resolved", "complete", "completed", "x"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def find_hub(cwd):
    env = os.environ.get("DEA_HUB")
    if env:
        p = os.path.expanduser(env)
        return p if os.path.isdir(p) else None
    cur = os.path.abspath(cwd or os.getcwd())
    while True:
        if os.path.isfile(os.path.join(cur, "de-agent.yaml")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    try:
        with open(os.path.expanduser("~/.config/de-agent/config.yaml"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    m = re.search(r"^hub:\s*[\"']?([^\"'\n#]+?)[\"']?\s*(?:#.*)?$", text, re.M)
    if m:
        p = os.path.expanduser(m.group(1).strip())
        if os.path.isabs(p) and os.path.isdir(p):
            return p
    return None


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def max_age_hours(hub):
    m = re.search(r"snapshot_max_age_hours\s*:\s*([0-9.]+)", read(os.path.join(hub, "de-agent.yaml")))
    try:
        return float(m.group(1)) if m else 24.0
    except ValueError:
        return 24.0


def split_row(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def due_rows(status_text, today, horizon_days=2):
    """Rows (of tables with Due/Date + State columns) with date <= today+horizon and state not done."""
    out = []
    lines = status_text.splitlines()
    i = 0
    limit = today + timedelta(days=horizon_days)
    while i < len(lines):
        if lines[i].lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            header = [h.lower() for h in split_row(lines[i])]
            di = next((k for k, h in enumerate(header) if h in ("due", "date")), None)
            si = next((k for k, h in enumerate(header) if h in ("state", "status")), None)
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = split_row(lines[i])
                i += 1
                if di is None or si is None or len(cells) <= max(di, si) or not DATE_RE.match(cells[di]):
                    continue
                try:
                    d = date.fromisoformat(cells[di])
                except ValueError:
                    continue
                if d <= limit and cells[si].strip().lower() not in DONE:
                    tag = "OVERDUE" if d < today else "due"
                    out.append("- [{}] {}".format(tag, " | ".join(c for c in cells if c)))
            continue
        i += 1
    return out


def stale_platform(hub, now):
    limit = max_age_hours(hub)
    pdir = os.path.join(hub, "platform")
    out = []
    try:
        names = sorted(os.listdir(pdir))
    except OSError:
        return out
    for n in names:
        if not n.endswith(".md"):
            continue
        m = re.search(r"^generated:\s*(\S+)", read(os.path.join(pdir, n))[:600], re.M)
        if not m:
            continue
        try:
            gen = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        age = (now - gen).total_seconds() / 3600.0
        if age > limit:
            out.append(f"- platform/{n} is {age:.0f}h old (max {limit:g}h); run `dea snapshot`")
    return out


def build(hub, today=None, now=None):
    today = today or date.today()
    now = now or datetime.now(timezone.utc)
    footer = "Use `dea find <term>` before searching repos; platform state: {}/".format(os.path.join(hub, "platform"))
    parts = []
    due = due_rows(read(os.path.join(hub, "status.md")), today)
    if due:
        parts.append("## Due / overdue\n" + "\n".join(due))
    stale = stale_platform(hub, now)
    if stale:
        parts.append("## Stale platform data\n" + "\n".join(stale))
    tail = "\n\n".join(parts + [footer])
    map_text = read(os.path.join(hub, "map.md")).strip()
    budget = MAX_CHARS - len(tail) - 2
    if len(tail) > MAX_CHARS - 200:  # pathological: keep the footer and as much as fits
        tail = tail[: MAX_CHARS - 200 - len(footer) - 2] + "\n\n" + footer
        budget = MAX_CHARS - len(tail) - 2
    if len(map_text) > budget:
        map_text = map_text[: max(budget - 40, 0)].rstrip() + "\n[map.md truncated]"
    text = (map_text + "\n\n" + tail) if map_text else tail
    return text[:MAX_CHARS]


def mark_session_start(hub, data):
    """Best effort: remember when the session started so stop_check can tell what changed during it."""
    sid = str(data.get("session_id") or "") if isinstance(data, dict) else ""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
        return
    try:
        d = os.path.join(hub, ".dea", "session")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, sid + ".start")
        if not os.path.exists(p):  # resume/clear/compact keep the original start
            with open(p, "w", encoding="utf-8") as f:
                f.write("1")
    except OSError:
        pass


def main():
    try:
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except Exception:
            data = {}
        cwd = (data.get("cwd") if isinstance(data, dict) else None) or os.environ.get("CLAUDE_PROJECT_DIR")
        hub = find_hub(cwd)
        if not hub:
            return
        mark_session_start(hub, data)
        text = build(hub)
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
    except BaseException:  # never fail a session
        return


if __name__ == "__main__":
    main()
    sys.exit(0)
