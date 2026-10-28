"""Shared helpers for the static scanners: SQL analysis, hashing and source revision. Nothing here runs scanned code."""
from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

SCANNER_VERSION = 1
CALL_RE = re.compile(r"^\s*call\s+([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*)", re.I)
JINJA_BLOCK_RE = re.compile(r"\{%.*?%\}", re.S)
JINJA_EXPR_RE = re.compile(r"\{\{.*?\}\}", re.S)
PLACEHOLDER_RE = re.compile(r"%\(\w+\)s|%s|\$\d+")
HEURISTIC_RES = [
    ("w", re.compile(r"\b(?:insert\s+into|merge\s+into|update|delete\s+from|truncate\s+table|truncate)\s+([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)", re.I)),
    ("r", re.compile(r"\b(?:from|join)\s+([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)", re.I)),
]
# `from` that is not a table clause: EXTRACT(hour FROM col), x IS [NOT] DISTINCT FROM col, SUBSTRING/TRIM/OVERLAY(.. FROM ..)
NON_TABLE_FROM_RE = re.compile(
    r"\bextract\s*\(\s*\w+\s+from\b|\bis\s+(?:not\s+)?distinct\s+from\b|\b(?:substring|trim|overlay)\s*\([^()]*?\bfrom\b", re.I)
SQL_KEYWORDS = {"select", "lateral", "unnest", "values", "only", "set", "table"}


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()  # noqa: S324  (content fingerprint, not security)


def norm_table(t: exp.Table) -> str:
    parts = [p for p in (t.db, t.name) if p]
    return ".".join(parts).lower().replace('"', "")


def clean_sql(sql: str) -> str:
    sql = JINJA_BLOCK_RE.sub(" ", sql)
    sql = JINJA_EXPR_RE.sub("'jinja'", sql)
    return PLACEHOLDER_RE.sub("NULL", sql)


def _write_target(stmt: exp.Expression) -> exp.Table | None:
    target = stmt.this
    if isinstance(target, exp.Schema):  # INSERT INTO t (a, b)
        target = target.this
    return target if isinstance(target, exp.Table) else None


def analyze_sql(sql: str, dialect: str = "postgres") -> dict[str, Any]:
    """Tables read / written and CALL targets of a SQL string. Never raises; `unparsed` is True on failure."""
    out: dict[str, Any] = {"reads": [], "writes": [], "calls": [], "unparsed": False}
    reads: set[str] = set()
    writes: set[str] = set()
    calls: set[str] = set()
    text = clean_sql(sql)
    for chunk in re.split(r";\s*(?:\n|$)", text):
        m = CALL_RE.match(chunk)
        if m:
            calls.add(m.group(1).lower())
    try:
        stmts = [s for s in sqlglot.parse(text, read=dialect or "postgres") if s is not None]
    except Exception:
        out["unparsed"] = True
        out["raw"] = " ".join(sql.split())[:120]
        return out
    for stmt in stmts:
        if isinstance(stmt, exp.Command):
            continue  # VACUUM, CALL, SET ...: no table data flow we can state
        ctes = {c.alias.lower() for c in stmt.find_all(exp.CTE)}
        target: exp.Table | None = None
        if isinstance(stmt, (exp.Insert, exp.Update, exp.Delete, exp.Merge)):
            target = _write_target(stmt)
        elif isinstance(stmt, exp.Create) and str(stmt.args.get("kind") or "").upper() in (
                "TABLE", "VIEW", "MATERIALIZED VIEW"):
            target = _write_target(stmt)
        elif isinstance(stmt, exp.TruncateTable):
            for t in stmt.expressions:
                if isinstance(t, exp.Table):
                    writes.add(norm_table(t))
            continue
        if target is not None:
            writes.add(norm_table(target))
        for t in stmt.find_all(exp.Table):
            if t is target or not t.name:
                continue
            name = norm_table(t)
            if "." not in name and name in ctes:
                continue
            reads.add(name)
    out["reads"] = sorted(reads)
    out["writes"] = sorted(writes)
    out["calls"] = sorted(calls)
    return out


def heuristic_sql(body: str) -> dict[str, Any]:
    """Regex fallback for bodies sqlglot cannot parse (plpgsql). Marked heuristic by the caller."""
    reads: set[str] = set()
    writes: set[str] = set()
    calls = {m.group(1).lower() for m in re.finditer(r"\bcall\s+([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*)", body, re.I)}
    text = re.sub(r"--[^\n]*", " ", body)
    text = NON_TABLE_FROM_RE.sub(lambda m: m.group(0)[:-4] + " ", text)
    for kind, rx in HEURISTIC_RES:
        for m in rx.finditer(text):
            name = m.group(1).lower()
            if name in SQL_KEYWORDS:
                continue
            (writes if kind == "w" else reads).add(name)
    return {"reads": sorted(reads - writes), "writes": sorted(writes), "calls": sorted(calls), "unparsed": False}


def git(path: Path, *args: str) -> str | None:
    try:
        p = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def source_revision(path: Path, file_hashes: dict[str, str]) -> str:
    """Deterministic revision: last commit touching `path` (+ dirty fingerprint), or a content hash outside git."""
    content = sha1("\n".join(f"{k}:{v}" for k, v in sorted(file_hashes.items())).encode())[:10]
    head = git(path, "log", "-1", "--format=%h", "--", ".")
    if head:
        dirty = git(path, "status", "--porcelain", "--", ".")
        return f"{head}+dirty.{content}" if dirty else head
    return f"content.{content}"


def last_commit(path: Path) -> dict[str, str] | None:
    out = git(path, "log", "-1", "--format=%h%x1f%cs%x1f%s", "--", ".")
    if not out:
        return None
    h, d, s = (out.split("\x1f") + ["", ""])[:3]
    return {"hash": h, "date": d, "subject": s}


def rel_to(base: Path, p: Path) -> str:
    try:
        return p.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return p.as_posix()
