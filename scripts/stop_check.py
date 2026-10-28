#!/usr/bin/env python3
"""Stop hook (M2 section 4).

1. A plan applied (or rolled back) after the latest snapshot -> block the stop ONCE per plan: re-run `dea snapshot` and
   confirm the fix in `dea diff`. A marker per plan (and stop_hook_active) guarantees it never loops.
2. Otherwise, if files under a scanned repo changed since this session started, add a one-line non-blocking suggestion
   to run hub-sync (once per session).

Stdlib only, Python 3.9 compatible. Never fails the stop: any error -> silent exit 0.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SKIP_DIRS = {".git", ".dea", "node_modules", "__pycache__", ".venv", "target", "logs", "dist", "build"}
SCAN_BUDGET_S = 3.0
SNAP_RE = re.compile(r"^\d{8}T\d{6}Z$")


def newest_mtime(path):
    best = 0.0
    try:
        for name in os.listdir(path):
            try:
                best = max(best, os.stat(os.path.join(path, name)).st_mtime)
            except OSError:
                continue
        best = max(best, os.stat(path).st_mtime)
    except OSError:
        pass
    return best


def latest_snapshot_time(hub):
    sdir = os.path.join(hub, ".dea", "snapshots")
    try:
        names = sorted(n for n in os.listdir(sdir) if SNAP_RE.match(n))
    except OSError:
        return 0.0
    return newest_mtime(os.path.join(sdir, names[-1])) if names else 0.0


def pending_plans(hub):
    """[(plan_id, mtime)] for applied/rolled-back plans newer than the latest snapshot and not yet nagged about."""
    snap = latest_snapshot_time(hub)
    mdir = os.path.join(hub, ".dea", "stop_markers")
    out = []
    for suffix in (".applied.json", ".rolledback.json"):
        for path in glob.glob(os.path.join(hub, ".dea", "plans", "*" + suffix)):
            try:
                mt = os.stat(path).st_mtime
            except OSError:
                continue
            pid = os.path.basename(path)[: -len(suffix)] + suffix[:-5]
            if mt <= snap:
                continue
            marker = os.path.join(mdir, pid)
            try:
                with open(marker, encoding="utf-8") as f:
                    if float(f.read().strip() or 0) >= mt:
                        continue
            except (OSError, ValueError):
                pass
            out.append((pid, mt))
    return out


def mark(hub, pid, mt):
    mdir = os.path.join(hub, ".dea", "stop_markers")
    os.makedirs(mdir, exist_ok=True)
    with open(os.path.join(mdir, pid), "w", encoding="utf-8") as f:
        f.write(repr(mt))


def scan_paths(hub):
    try:
        with open(os.path.join(hub, "de-agent.yaml"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    m = re.search(r"^scans\s*:(.*)\Z", text, re.M | re.S)
    if not m:
        return []
    paths = []
    for p in re.findall(r"\bpath\s*:\s*[\"']?([^\s,}\"'#]+)", m.group(1)):
        full = os.path.normpath(os.path.join(hub, os.path.expanduser(p)))
        if os.path.isdir(full) and full not in paths:
            paths.append(full)
    return paths


def changed_since(paths, since):
    deadline = time.time() + SCAN_BUDGET_S
    for root in paths:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                try:
                    if os.stat(os.path.join(dirpath, fn)).st_mtime > since:
                        return True
                except OSError:
                    continue
            if time.time() > deadline:
                return False
    return False


def main():
    try:
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        from session_context import find_hub

        hub = find_hub(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR"))
        if not hub:
            return
        if not data.get("stop_hook_active"):
            pending = pending_plans(hub)
            if pending:
                for pid, mt in pending:
                    mark(hub, pid, mt)
                ids = ", ".join(p for p, _ in pending)
                sys.stdout.write(json.dumps({
                    "decision": "block",
                    "reason": (f"de-agent: plan(s) {ids} changed the platform after the latest snapshot. "
                               "Re-run `dea snapshot` and confirm the fix in `dea diff` before finishing."),
                }))
                return
        sid = str(data.get("session_id") or "")
        if sid and re.fullmatch(r"[A-Za-z0-9_-]+", sid):
            start = os.path.join(hub, ".dea", "session", sid + ".start")
            sent = os.path.join(hub, ".dea", "session", sid + ".suggested")
            if os.path.isfile(start) and not os.path.exists(sent):
                if changed_since(scan_paths(hub), os.stat(start).st_mtime):
                    with open(sent, "w", encoding="utf-8") as f:
                        f.write("1")
                    sys.stdout.write(json.dumps({
                        "systemMessage": "de-agent: files in a scanned repo changed this session; "
                                         "run the hub-sync skill to update status.md and profiles.",
                    }))
    except BaseException:  # never fail a stop
        return


if __name__ == "__main__":
    main()
    sys.exit(0)
