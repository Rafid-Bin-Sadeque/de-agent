"""Fail when the repo contains a string from an optional custom deny-list.

The list lives OUTSIDE the repo; its path comes from DEA_DENYLIST_FILE (one string per line, `#` comments and blank lines
ignored, matched case-insensitively). Skipped when the variable is unset. Failure messages name the file, line and the
entry NUMBER only: the string itself is never echoed into CI logs.
"""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", "_vendor", "graphify-out", ".dea"}
SKIP_FILES = {"test_denylist.py"}  # this file names the variable, not the strings
MAX_BYTES = 2_000_000


def _entries():
    path = os.environ.get("DEA_DENYLIST_FILE")
    if not path:
        pytest.skip("DEA_DENYLIST_FILE is not set")
    p = Path(path).expanduser()
    if not p.is_file():
        pytest.fail("DEA_DENYLIST_FILE does not point to a readable file")
    try:
        # NOTE: inside the repo the deny-list itself would be a leak: refuse to run with it there
        p.resolve().relative_to(ROOT)
        pytest.fail("the deny-list must live outside the repository")
    except ValueError:
        pass
    lines = [ln.strip() for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()]
    return [(i, ln.lower()) for i, ln in enumerate([ln for ln in lines if ln and not ln.startswith("#")], 1)]


def _files():
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=ROOT, capture_output=True, timeout=60)
        if out.returncode == 0:
            for rel in out.stdout.decode().split("\0"):
                if rel:
                    yield ROOT / rel
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            yield Path(dirpath) / f


def test_repo_contains_no_denied_strings():
    entries = _entries()
    assert entries, "the deny-list is empty"
    hits = []
    for p in _files():
        if p.name in SKIP_FILES or SKIP_DIRS & set(p.relative_to(ROOT).parts) or not p.is_file():
            continue
        try:
            if p.stat().st_size > MAX_BYTES:
                continue
            text = p.read_bytes().decode("utf-8", errors="ignore").lower()
        except OSError:
            continue
        rel = p.relative_to(ROOT).as_posix()
        hay = [rel.lower()] + text.splitlines()  # file NAMES count too
        for n, needle in entries:
            for ln, line in enumerate(hay):
                if needle in line:
                    hits.append(f"{rel}:{ln if ln else 'path'} matches deny-list entry #{n}")
                    break
    assert not hits, "deny-listed strings found:\n" + "\n".join(hits[:50])


def test_denylist_matching_logic_without_the_real_list(tmp_path, monkeypatch):
    """The matcher itself, against a throwaway list (never the real one)."""
    lst = tmp_path / "list.txt"
    lst.write_text("# comment\n\nZzTopSecretZz\n")
    monkeypatch.setenv("DEA_DENYLIST_FILE", str(lst))
    assert _entries() == [(1, "zztopsecretzz")]
    monkeypatch.delenv("DEA_DENYLIST_FILE")
    with pytest.raises(pytest.skip.Exception):
        _entries()
