"""`python` scan (v0.2 section 2): scripts, jobs, notebooks and pyspark code, read with `ast`, never run.

One owner per function (`py:<file>:<func>`), one for a file's module-level code (`py:<file>`), one per notebook
(`nb:<file>`, every cell is the notebook's). Each owner carries its direct SQL/table/path records and the effects it
inherits through calls to other scanned functions (`via: util:<module.func>`, up to 2 hops).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from de_agent.scanners import pycode
from de_agent.scanners.common import analyze_sql, heuristic_sql, rel_to

DBX_HEADER = "# Databricks notebook source"
DBX_SEP = "# COMMAND ----------"
SQL_MAGIC_RE = re.compile(r"^\s*%{1,2}sql\b", re.I)


def is_notebook_source(src: str) -> bool:
    return src.lstrip().startswith(DBX_HEADER)


def _sql_rec(text: str, line: int, cell: int | None, dialect: str) -> dict[str, Any]:
    res = analyze_sql(text, dialect)
    heur = False
    if res["unparsed"]:
        res = heuristic_sql(text)
        heur = True
    rec = {"line": line, "unrolled": False, "reads": res["reads"], "writes": res["writes"], "calls": res["calls"],
           "heuristic": heur, "text": " ".join(text.split())[:120]}
    if cell is not None:
        rec["cell"] = cell
    return rec


def databricks_cells(src: str) -> tuple[str, list[tuple[int, str]]]:
    """A Databricks source notebook -> (python text with non-python cells blanked, line numbers kept; [(line, sql cell)])."""
    lines = src.split("\n")
    out: list[str] = []
    sql: list[tuple[int, str]] = []
    i = 0
    n = len(lines)
    while i < n:
        j = i
        while j < n and lines[j].strip() != DBX_SEP:
            j += 1
        cell = lines[i:j]
        magic = [ln for ln in cell if ln.strip()]
        first = magic[0].strip() if magic else ""
        if first.startswith("# MAGIC"):
            body = [re.sub(r"^\s*# MAGIC ?", "", ln) for ln in cell]
            head = next((k for k, ln in enumerate(body) if ln.strip()), 0)
            if SQL_MAGIC_RE.match(body[head]):
                rest = SQL_MAGIC_RE.sub("", body[head], count=1)
                text = "\n".join([rest, *body[head + 1:]]).strip()
                if text:
                    sql.append((i + head + 1, text))
            out.extend([""] * len(cell))
        else:
            out.extend(cell)
        if j < n:
            out.append("")  # the separator line
        i = j + 1
    return "\n".join(out), sql


def _scan_python_source(text: str, rel: str, dialect: str) -> dict[str, Any]:
    tree = pycode.parse(text)
    res = pycode.CodeScanner(tree, rel, dialect).scan()
    return res


def scan_py_text(src: str, rel: str, dialect: str) -> dict[str, Any]:
    kind = "notebook" if is_notebook_source(src) else "python"
    out: dict[str, Any] = {"rel": rel, "kind": kind, "funcs": {}, "imports": {}, "stats": {}}
    try:
        text, sql_cells = databricks_cells(src) if kind == "notebook" else (src, [])
        res = _scan_python_source(text, rel, dialect)
    except (SyntaxError, ValueError, RecursionError) as ex:
        out["error"] = f"{type(ex).__name__}: {str(ex)[:80]}"
        return out
    except Exception as ex:  # a scanner bug on one file must not stop the scan
        out["error"] = f"scanner error {type(ex).__name__}: {str(ex)[:80]}"
        return out
    out.update(res)
    for line, text_ in sql_cells:
        out["funcs"].setdefault("<module>", {"line": 1, "params": [], "defaults": {}, "sql": [], "calls": []})["sql"].append(
            _sql_rec(text_, line, None, dialect))
    return out


def scan_ipynb_text(src: str, rel: str, dialect: str) -> dict[str, Any]:
    out: dict[str, Any] = {"rel": rel, "kind": "notebook", "funcs": {}, "imports": {}, "stats": {}}
    try:
        nb = json.loads(src)
        cells = nb["cells"] if isinstance(nb, dict) else []
    except (ValueError, KeyError, TypeError) as ex:
        out["error"] = f"{type(ex).__name__}: {str(ex)[:80]}"
        return out
    py_lines: list[str] = []
    starts: list[tuple[int, int]] = []  # (first line in the joined text, cell number)
    sql_cells: list[tuple[int, str]] = []
    cell_no = 0
    for c in cells:
        if not isinstance(c, dict) or c.get("cell_type") != "code":
            continue
        cell_no += 1
        s = c.get("source", "")
        body = "".join(s) if isinstance(s, list) else str(s)
        lines = body.split("\n")
        first = next((ln for ln in lines if ln.strip()), "")
        if SQL_MAGIC_RE.match(first):
            idx = next(k for k, ln in enumerate(lines) if ln.strip())
            rest = SQL_MAGIC_RE.sub("", lines[idx], count=1)
            text = "\n".join([rest, *lines[idx + 1:]]).strip()
            if text:
                sql_cells.append((cell_no, text))
            continue
        starts.append((len(py_lines) + 1, cell_no))
        for ln in lines:
            st = ln.lstrip()
            py_lines.append("" if st.startswith(("%", "!")) else ln)
        py_lines.append("")
    try:
        res = _scan_python_source("\n".join(py_lines), rel, dialect)
    except (SyntaxError, ValueError, RecursionError) as ex:
        out["error"] = f"{type(ex).__name__}: {str(ex)[:80]}"
        res = {"funcs": {}, "imports": {}, "stats": {}}
    out.update(res)

    def locate(line: int) -> tuple[int, int]:
        cell, start = 1, 1
        for s_line, c_no in starts:
            if s_line <= line:
                cell, start = c_no, s_line
        return cell, line - start + 1

    for fn in out["funcs"].values():
        for rec in [*fn["sql"], *fn["calls"]]:
            rec["cell"], rec["line"] = locate(rec["line"])
    for cell, text in sql_cells:
        out["funcs"].setdefault("<module>", {"line": 1, "params": [], "defaults": {}, "sql": [], "calls": []})["sql"].append(
            _sql_rec(text, 1, cell, dialect))
    return out


def scan_file(src: str, rel: str, dialect: str) -> dict[str, Any]:
    return scan_ipynb_text(src, rel, dialect) if rel.endswith(".ipynb") else scan_py_text(src, rel, dialect)


def list_files(scan: Any, root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for g in scan.globs:
        for p in sorted(root.glob(g)):
            if p.is_file() and not (set(p.relative_to(root).parts) & pycode.SKIP_SEGMENTS):
                files[rel_to(root, p)] = p
    return files


def _owner_file(rel: str, rec: dict[str, Any]) -> str:
    if rec.get("cell") is not None:
        return f"{rel}#cell{rec['cell']}:{rec['line']}"
    return f"{rel}:{rec['line']}"


def assemble(scan: Any, per_file: dict[str, dict[str, Any]], revision: str) -> dict[str, Any]:
    graph = pycode.UtilGraph({r: {"funcs": pf.get("funcs", {}), "imports": pf.get("imports", {})} for r, pf in per_file.items()},
                             scan.dialect)
    owners: list[dict[str, Any]] = []
    stats = {"files": len(per_file), "notebooks": 0, "functions": 0, "owners": 0, "parse_errors": 0, "unresolved_sql": 0,
             "unparsed_sql": 0, "unresolved_util_args": 0}
    for rel in sorted(per_file):
        pf = per_file[rel]
        if pf.get("error"):
            stats["parse_errors"] += 1
        stats["unresolved_sql"] += pf.get("stats", {}).get("unresolved_sql", 0)
        stats["unparsed_sql"] += pf.get("stats", {}).get("unparsed_sql", 0)
        funcs = pf.get("funcs", {})
        stats["functions"] += sum(1 for k in funcs if k != "<module>")
        if pf.get("kind") == "notebook":
            stats["notebooks"] += 1
            direct: list[dict[str, Any]] = []
            util: list[dict[str, Any]] = []
            for fn in funcs.values():
                direct += [_direct(r, fn["defaults"], scan.dialect) for r in fn["sql"]]
                util += graph.inherited(rel, fn["calls"], fn["defaults"])
            owner = _owner(f"nb:{rel}", rel, 1, "notebook", direct, util)
            if owner:
                owners.append(owner)
            continue
        for key in sorted(funcs):
            fn = funcs[key]
            oid = f"py:{rel}" if key == "<module>" else f"py:{rel}:{key}"
            direct = [_direct(r, fn["defaults"], scan.dialect) for r in fn["sql"]]
            util = graph.inherited(rel, fn["calls"], fn["defaults"])
            owner = _owner(oid, rel, fn["line"], "module" if key == "<module>" else "function", direct, util)
            if owner:
                owners.append(owner)
    stats["owners"] = len(owners)
    stats["unresolved_util_args"] = graph.unresolved
    return {"scan": scan.name, "type": "python", "project": scan.project_name, "revision": revision, "owners": owners,
            "stats": stats}


def _direct(rec: dict[str, Any], defaults: dict[str, list[str]], dialect: str) -> dict[str, Any] | None:
    """A function's own record; a template with unbound parameters yields nothing (its callers carry it)."""
    if not rec.get("tpl"):
        return rec
    t = rec["tpl"]
    texts = pycode.bind_values(t.get("sql", []), defaults)
    reads = pycode.bind_values(t.get("reads", []), defaults)
    writes = pycode.bind_values(t.get("writes", []), defaults)
    if texts is None or reads is None or writes is None:
        return None
    r = {k: rec[k] for k in ("line", "unrolled", "heuristic")}
    rd, wr, cl = set(rec["reads"]) | {pycode.node_key(x) for x in reads}, set(rec["writes"]) | {
        pycode.node_key(x) for x in writes}, set(rec["calls"])
    for x in texts:
        if pycode.SQL_START_RE.match(x):
            res = analyze_sql(x, dialect)
            rd |= set(res["reads"])
            wr |= set(res["writes"])
            cl |= set(res["calls"])
    r.update({"reads": sorted(rd), "writes": sorted(wr), "calls": sorted(cl)})
    if rec.get("cell") is not None:
        r["cell"] = rec["cell"]
    return r


def _owner(oid: str, rel: str, line: int, kind: str, direct: list[Any], util: list[dict[str, Any]]) -> dict[str, Any] | None:
    d = [{**{k: v for k, v in r.items() if k != "text"}, "file": _owner_file(rel, r)} for r in direct
         if r and (r["reads"] or r["writes"] or r["calls"])]
    u = [{**r, "file": f"{rel}:{r['line']}"} for r in util]
    if not d and not u:
        return None
    d.sort(key=lambda r: (r["line"], repr(r["reads"]), repr(r["writes"])))
    return {"id": oid, "file": rel, "line": line, "kind": kind, "direct": d, "util": u}
