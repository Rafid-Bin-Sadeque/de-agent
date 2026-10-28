"""SQL scanner (M1c section 2): objects defined per file and the tables each reads/writes."""
from __future__ import annotations

import re
from typing import Any

from de_agent.scanners.common import analyze_sql, heuristic_sql

DOLLAR_RE = re.compile(r"\$(\w*)\$(.*?)\$\1\$", re.S)
HEADER_RE = re.compile(
    r"^\s*create\s+(?:or\s+replace\s+)?(?:(?:temp|temporary|unlogged|external|recursive|secure)\s+)*"
    r"(materialized\s+view|table|view|procedure|function)\s+(?:if\s+not\s+exists\s+)?([\w.\"$]+)", re.I)


def _statements(text: str) -> list[tuple[str, int]]:
    """Split a SQL file into (statement, first line) honouring `$$ ... $$` bodies and quotes/comments."""
    out: list[tuple[str, int]] = []
    buf: list[str] = []
    start_line, line = 1, 1
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "$":
            m = re.compile(r"\$(\w*)\$").match(text, i)
            if m:
                end = text.find(m.group(0), m.end())
                if end != -1:
                    seg = text[i:end + len(m.group(0))]
                    buf.append(seg)
                    line += seg.count("\n")
                    i = end + len(m.group(0))
                    continue
        if c == "'":
            j = i + 1
            while j < n and not (text[j] == "'" and text[j + 1:j + 2] != "'"):
                j += 2 if text[j] == "'" else 1
            seg = text[i:j + 1]
            buf.append(seg)
            line += seg.count("\n")
            i = j + 1
            continue
        if text.startswith("--", i):
            j = text.find("\n", i)
            j = n if j == -1 else j
            i = j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i)
            j = n if j == -1 else j + 2
            line += text[i:j].count("\n")
            i = j
            continue
        if c == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append((stmt, start_line))
            buf = []
            i += 1
            start_line = line
            continue
        if c == "\n":
            line += 1
            if not "".join(buf).strip():
                start_line = line
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append((tail, start_line))
    return out


def _body_flow(body: str, dialect: str) -> dict[str, Any]:
    flat = re.sub(r"(?is)^\s*begin\b", "", body.strip())
    flat = re.sub(r"(?is)\bend\s*;?\s*$", "", flat)
    if _has_plpgsql(flat):
        h = heuristic_sql(body)
        h["heuristic"] = True
        return h
    res = analyze_sql(flat, dialect)
    if res["unparsed"]:
        h = heuristic_sql(body)
        h["heuristic"] = True
        return h
    res["heuristic"] = False
    return res


def _has_plpgsql(body: str) -> bool:
    return bool(re.search(r"(?im)^\s*(declare|if\b.*\bthen|loop|for\b.*\bloop|raise|return|execute)\b|:=", body))


def scan_sql_text(text: str, rel: str, dialect: str) -> dict[str, Any]:
    objects: list[dict[str, Any]] = []
    for stmt, line in _statements(text):
        m = HEADER_RE.match(stmt)
        if not m:
            continue
        kind = re.sub(r"\s+", " ", m.group(1).lower())
        name = m.group(2).strip('"').lower().replace('"', "")
        obj: dict[str, Any] = {"name": name, "kind": kind, "file": rel, "line": line}
        if kind in ("procedure", "function"):
            bodies = [b[1] for b in DOLLAR_RE.findall(stmt)]
            if not bodies:
                mm = re.search(r"\bas\s+'(.*)'\s*(?:language\b.*)?$", stmt, re.I | re.S)
                bodies = [mm.group(1).replace("''", "'")] if mm else []
            flow = {"reads": [], "writes": [], "calls": [], "heuristic": False}
            for b in bodies:
                f = _body_flow(b, dialect)
                for k in ("reads", "writes", "calls"):
                    flow[k] = sorted(set(flow[k]) | set(f[k]))
                flow["heuristic"] = flow["heuristic"] or f["heuristic"]
            obj.update(flow)
        else:
            res = analyze_sql(DOLLAR_RE.sub("'x'", stmt), dialect)
            obj.update({"reads": [r for r in res["reads"] if r != name], "writes": sorted({name, *res["writes"]}),
                        "calls": res["calls"], "heuristic": False})
            if res["unparsed"]:
                h = heuristic_sql(stmt)
                obj.update({"reads": [r for r in h["reads"] if r != name], "heuristic": True})
        objects.append(obj)
    return {"rel": rel, "objects": objects}


def assemble(scan: Any, per_file: dict[str, dict[str, Any]], revision: str) -> dict[str, Any]:
    objs = [o for rel in sorted(per_file) for o in per_file[rel]["objects"]]
    objs.sort(key=lambda o: (o["file"], o["line"], o["name"]))
    return {"scan": scan.name, "type": "sql", "project": scan.project_name, "revision": revision, "objects": objs,
            "stats": {"files": len(per_file), "objects": len(objs),
                      "heuristic": sum(1 for o in objs if o.get("heuristic"))}}

