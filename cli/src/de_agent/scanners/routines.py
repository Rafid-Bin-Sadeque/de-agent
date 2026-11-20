"""Live routines in the edge graph (v0.3 section 3): parse stored bodies, build the routine catalog, detect repo drift.

Offline only: reads the latest snapshots (hashes) and the content-addressed body store; nothing here touches an engine.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from de_agent.routines import RoutineStore
from de_agent.scanners.common import analyze_sql, heuristic_sql
from de_agent.scanners.sql import DOLLAR_RE, _body_flow, body_fingerprint

PARSER_VERSION = 2  # 2: temp tables a routine creates are dropped (live Postgres check 2026-11-08)
SQL_LANGUAGES = {"sql", "plpgsql", "tsql", "pltsql"}
PARSE_BUDGET_S = 20.0  # collect-time sqlglot budget per source; past it bodies are regex-scanned (heuristic)
MAX_PARSE_CHARS = 200_000  # bigger bodies are regex-scanned only (sqlglot cost grows fast)
ID_PREFIX = {"procedure": "proc", "function": "function", "view": "view", "materialized_view": "view", "trigger": "trigger"}
TRIGGER_EXEC_RE = re.compile(r"\bexecute\s+(?:function|procedure)\s+([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*)\s*\(", re.I)
_CREATE_AS_RE = re.compile(r"^\s*create\b.*?\bas\b\s*", re.I | re.S)
_QUOTED_BODY_RE = re.compile(r"\bas\s+'(.*)'\s*(?:language\b.*)?$", re.I | re.S)
_EMPTY: dict[str, Any] = {"reads": [], "writes": [], "calls": [], "parse": "none"}


_CTRL = re.compile(r"[\x00-\x1f\x7f]+")


def routine_name(row: dict[str, Any]) -> str:
    """Lower-case `schema.name`, control characters (newlines in a hostile identifier) collapsed: ids land in markdown."""
    return _CTRL.sub(" ", f"{row['schema']}.{row['name']}").lower()


def routine_id(source: str, row: dict[str, Any]) -> str:
    return f"{ID_PREFIX.get(row['kind'], 'proc')}:{source}:{routine_name(row)}"


def extract_bodies(text: str) -> list[str]:
    """The procedural bodies inside a CREATE statement: `$tag$ ... $tag$` blocks, else `AS '...'`, else the part after
    the header's AS (T-SQL, MySQL, Snowflake), else the whole text."""
    bodies = [b[1] for b in DOLLAR_RE.findall(text)]
    if bodies:
        return bodies
    m = _QUOTED_BODY_RE.search(text)
    if m:
        return [m.group(1).replace("''", "'")]
    m = _CREATE_AS_RE.match(text)
    if m:
        return [text[m.end():]]
    return [text]


_TEMP_TABLE_RE = re.compile(
    r"\bcreate\s+(?:(?:local|global)\s+)?(?:temp|temporary)\s+table\s+(?:if\s+not\s+exists\s+)?([\w$#.\"]+)"
    r"|\binto\s+(?:(?:local|global)\s+)?(?:temp|temporary)\s+(?:table\s+)?([\w$#.\"]+)", re.I)


def temp_tables(text: str) -> set[str]:
    """Names of the session-local tables a routine creates (`CREATE TEMP TABLE x`, `SELECT ... INTO TEMP x`)."""
    return {(a or b).replace('"', "").lower().split(".")[-1] for a, b in _TEMP_TABLE_RE.findall(text or "")}


def _drop_temp(res: dict[str, Any], text: str) -> dict[str, Any]:
    """A temp table (or a Redshift `#name` table) is not a platform table: it must not become a node or an edge."""
    tmp = temp_tables(text)

    def keep(t: str) -> bool:
        last = str(t).lower().split(".")[-1]
        return not last.startswith("#") and last not in tmp and not str(t).lower().startswith("pg_temp.")

    return {**res, "reads": [t for t in res.get("reads", []) if keep(t)], "writes": [t for t in res.get("writes", []) if keep(t)]}


def parse_routine(kind: str, language: str, text: str, dialect: str, regex_only: bool = False) -> dict[str, Any]:
    """{reads, writes, calls, parse}. Non-SQL languages are never parsed as SQL (node only). Never raises: a hostile or
    pathological body (deep nesting, a parser bug) degrades to the regex scan. Temp tables the routine creates are dropped."""
    try:
        return _drop_temp(_parse_routine(kind, language, text, dialect, regex_only), text)
    except Exception:  # RecursionError included: one routine must not take the collector down
        try:
            h = heuristic_sql(text)
            return _drop_temp({"reads": h["reads"], "writes": h["writes"], "calls": h["calls"], "parse": "heuristic"}, text)
        except Exception:
            return dict(_EMPTY)


def _parse_routine(kind: str, language: str, text: str, dialect: str, regex_only: bool) -> dict[str, Any]:
    if not text:
        return dict(_EMPTY)
    lang = (language or "").lower()
    if kind == "trigger":
        m = TRIGGER_EXEC_RE.search(text)
        if m:  # pg_get_triggerdef: the trigger only points at its function
            return {"reads": [], "writes": [], "calls": [m.group(1).lower()], "parse": "full"}
    elif lang not in SQL_LANGUAGES:
        return dict(_EMPTY)
    if kind == "trigger" and lang not in SQL_LANGUAGES:
        lang = "sql"
    if regex_only or len(text) > MAX_PARSE_CHARS:
        h = heuristic_sql(text)
        return {"reads": h["reads"], "writes": h["writes"], "calls": h["calls"], "parse": "heuristic"}
    if kind in ("view", "materialized_view"):
        m = _CREATE_AS_RE.match(text)
        body = (text[m.end():] if m else text).strip().rstrip(";").strip()
        res = analyze_sql(body, dialect)
        if res["unparsed"]:
            h = heuristic_sql(body)
            return {"reads": h["reads"], "writes": [], "calls": [], "parse": "heuristic"}
        return {"reads": res["reads"], "writes": [], "calls": [], "parse": "full"}
    reads: set[str] = set()
    writes: set[str] = set()
    calls: set[str] = set()
    heuristic = False
    for b in extract_bodies(text):
        f = _body_flow(b, dialect)
        reads |= set(f["reads"])
        writes |= set(f["writes"])
        calls |= set(f["calls"])
        heuristic = heuristic or bool(f.get("heuristic"))
    return {"reads": sorted(reads), "writes": sorted(writes), "calls": sorted(calls),
            "parse": "heuristic" if heuristic else "full"}


def make_parser(dialect: str) -> Any:
    """`parser(row, body) -> {reads, writes, calls, parse}` for finalize_rows (collect time), cached per process by body."""
    cache: dict[tuple[str, str, str], dict[str, Any]] = {}
    spent = [0.0]

    def parser(row: dict[str, Any], body: str) -> dict[str, Any]:
        key = (row["kind"], row["language"], row["body_hash"])
        if key not in cache:
            t0 = time.monotonic()
            cache[key] = parse_routine(row["kind"], row["language"], body, dialect, regex_only=spent[0] > PARSE_BUDGET_S)
            spent[0] += time.monotonic() - t0
        return dict(cache[key])

    return parser


def dialect_of_source(result: dict[str, Any]) -> str:
    t = result.get("type")
    if t == "redshift":
        return "redshift"
    if t == "sql":
        pack = (result.get("data") or {}).get("meta", {}).get("pack")
        try:
            from de_agent.packs import load_pack

            return load_pack(str(pack)).dialect or "postgres"
        except Exception:
            return "postgres"
    return "postgres"


def build_catalog(
    sources: list[dict[str, Any]], store: RoutineStore, previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """{source: {"schema.name(args)": {kind, language, reads, writes, calls, parse, body_hash, fingerprint}}}.

    `sources`: [{source, dialect, rows}] (latest snapshot rows). `previous`: the earlier catalog; an entry whose body_hash,
    kind, language and parser version are unchanged is reused, so an unchanged routine is never re-parsed."""
    prev = (previous or {}).get("sources") or {}
    out: dict[str, Any] = {}
    stats = {"parsed": 0, "reused": 0}
    for s in sources:
        old = prev.get(s["source"]) or {}
        entries: dict[str, Any] = {}
        for r in s["rows"]:
            key = f"{routine_name(r)}{r.get('args') or ''}"
            h = r.get("body_hash") or ""
            o = old.get(key)
            if o and o.get("body_hash") == h and o.get("kind") == r["kind"] and o.get("language") == r.get("language") \
                    and o.get("parser") == PARSER_VERSION and o.get("dialect") == s["dialect"]:
                entries[key] = o
                stats["reused"] += 1
                continue
            text = store.get(h) if h else None
            if text:
                parsed = parse_routine(r["kind"], r.get("language") or "", text, s["dialect"])
                fp = body_fingerprint(extract_bodies(text)) if r["kind"] in ("procedure", "function") else ""
                stats["parsed"] += 1
            else:
                parsed, fp = dict(_EMPTY), ""
            entries[key] = {"kind": r["kind"], "schema": r["schema"], "name": r["name"], "args": r.get("args") or "",
                            "language": r.get("language") or "", "body_hash": h, "fingerprint": fp,
                            "dialect": s["dialect"], "parser": PARSER_VERSION, **parsed}
        out[s["source"]] = entries
    return {"version": 1, "sources": out, "stats": stats}


def live_routines(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    """Flat list of routine nodes with their edge ids: {id, source, kind, name(schema.name), args, reads, writes, calls...}."""
    out: list[dict[str, Any]] = []
    for source, entries in sorted((catalog.get("sources") or {}).items()):
        for key, e in sorted(entries.items()):
            node = {**e, "source": source, "key": key, "qname": routine_name(e)}
            node["id"] = routine_id(source, e)
            out.append(node)
    return out


def qualify(name: str, schema: str) -> tuple[str, bool]:
    """A table name from a routine body: unqualified names live in the routine's own schema (a stated assumption)."""
    if "." in name:
        return name, False
    return f"{schema.lower()}.{name}", True


def drift(live: list[dict[str, Any]], repo_objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Routines present live AND in a repo `.sql` scan whose normalized inner bodies differ (procedures and functions only:
    the engines rewrite view text, so a view comparison would always differ)."""
    by_name: dict[str, list[dict[str, Any]]] = {}
    for o in repo_objects:
        if o.get("kind") in ("procedure", "function") and o.get("body_hash"):
            by_name.setdefault(o["name"].lower(), []).append(o)
    out: list[dict[str, Any]] = []
    for n in live:
        if n["kind"] not in ("procedure", "function") or not n.get("fingerprint"):
            continue
        cands = by_name.get(n["qname"], [])
        if not cands:
            continue
        if any(c["body_hash"] == n["fingerprint"] for c in cands):
            continue
        c = cands[0]
        out.append({"source": n["source"], "routine": n["qname"], "args": n.get("args", ""),
                    "repo": f"{c['file']}:{c['line']}", "scan": c.get("scan", "")})
    return out


def drift_message(d: dict[str, Any]) -> str:
    return (f"routine {d['routine']} differs from {d['repo']} (live changed outside the repo, or the repo is ahead)")


def hub_store(hub: Path) -> RoutineStore:
    return RoutineStore(hub)
