"""Routines in the platform pages (v0.3 section 4): the summary block and one page per routine. Deterministic.

Every identifier and body comes from a remote engine: names are sanitized for file names and inline markdown, bodies go
in a fence longer than any backtick run inside them.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from de_agent.routines import RoutineStore, safe_component

KIND_ORDER = ("procedure", "function", "view", "materialized_view", "trigger")
MAX_CHANGES_SHOWN = 20
_CTRL = re.compile(r"[\x00-\x1f\x7f]+")


def md_inline(text: Any, limit: int = 160) -> str:
    """Safe inside a markdown line or table cell: no newlines, no pipes, no backticks, bounded."""
    s = _CTRL.sub(" ", str(text)).replace("|", "\\|").replace("`", "'").strip()
    return s if len(s) <= limit else s[: limit - 3] + "..."


def fence_for(body: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", body)), default=0)
    return "`" * max(3, longest + 1)


def qname(row: dict[str, Any]) -> str:
    return f"{row['schema']}.{row['name']}"


def row_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row.get("schema")), str(row.get("name")), str(row.get("args") or ""))


def page_names(rows: list[dict[str, Any]]) -> dict[tuple[str, str, str], str]:
    """{row key: file name without extension}. Colliding sanitized names (overloads, hostile look-alikes) get a hash suffix."""
    base = {row_key(r): safe_component(qname(r)) for r in rows}
    count: dict[str, int] = {}
    for b in base.values():
        count[b.lower()] = count.get(b.lower(), 0) + 1  # case-insensitive file systems: `Foo` and `foo` collide
    kinds: dict[str, list[str]] = {}
    for r in rows:
        kinds.setdefault(base[row_key(r)].lower(), []).append(str(r.get("kind")))
    out: dict[tuple[str, str, str], str] = {}
    for r in rows:
        k, b = row_key(r), base[row_key(r)]
        if count[b.lower()] == 1:
            out[k] = b
        elif len(set(kinds[b.lower()])) == len(kinds[b.lower()]) and b == b.lower():  # e.g. a trigger and its function share a name: the kind tells them apart
            out[k] = f"{b}~{safe_component(str(r.get('kind')))}"
        else:
            out[k] = f"{b}~{hashlib.sha1('|'.join(k).encode('utf-8')).hexdigest()[:6]}"  # noqa: S324
    return out


def routine_changes(prev: list[dict[str, Any]] | None, rows: list[dict[str, Any]]) -> dict[str, list[str]] | None:
    """added / removed / changed (body hash) routines versus the previous snapshot; None without a previous snapshot."""
    if prev is None:
        return None
    po = {row_key(r): r for r in prev}
    pn = {row_key(r): r for r in rows}
    fmt = lambda k, r: f"{k[0]}.{k[1]}{k[2]} ({r['kind']})"  # noqa: E731
    return {
        "added": [fmt(k, pn[k]) for k in sorted(pn.keys() - po.keys())],
        "removed": [fmt(k, po[k]) for k in sorted(po.keys() - pn.keys())],
        "changed": [fmt(k, pn[k]) for k in sorted(pn.keys() & po.keys())
                    if po[k].get("body_hash") and pn[k].get("body_hash")      # no body on one side = unknown, not changed
                    and po[k].get("body_hash") != pn[k].get("body_hash")],
    }


def routines_block(rows: list[dict[str, Any]], meta: dict[str, Any] | None = None,
                   changes: dict[str, list[str]] | None = None, with_pages: bool = False) -> list[str]:
    out = ["## Routines", ""]
    if not rows:
        return out + ["none", ""]
    kinds = [k for k in KIND_ORDER if any(r["kind"] == k for r in rows)]
    by_schema: dict[str, dict[str, int]] = {}
    for r in rows:
        by_schema.setdefault(str(r["schema"]), {}).setdefault(r["kind"], 0)
        by_schema[str(r["schema"])][r["kind"]] += 1
    out.append("| schema | " + " | ".join(kinds) + " | total |")
    out.append("|---|" + "---|" * (len(kinds) + 1))
    for sch in sorted(by_schema):
        c = by_schema[sch]
        out.append(f"| {md_inline(sch)} | " + " | ".join(str(c.get(k, 0)) for k in kinds) + f" | {sum(c.values())} |")
    out.append("")
    cov = {p: sum(1 for r in rows if r.get("parse") == p) for p in ("full", "heuristic", "none")}
    out.append(f"- parse coverage: full {cov['full']}, heuristic {cov['heuristic']}, none {cov['none']}")
    out.append(f"- truncated: {sum(1 for r in rows if r.get('truncated'))}, redacted: {sum(1 for r in rows if r.get('redacted'))}, "
               f"without a body: {sum(1 for r in rows if not r.get('body_hash'))}")
    m = meta or {}
    if m.get("routine_view_bodies_skipped"):
        out.append("- view bodies were skipped: a lock timeout on a relation the views read (retry later)")
    if m.get("routines_mv_unavailable"):
        out.append("- materialized views are listed as plain views: svv_mv_info could not be read")
    if m.get("routines_mv_bodies_missing"):
        out.append(f"- {m['routines_mv_bodies_missing']} materialized view(s) listed by name only: no body source answered")
    if with_pages:
        out.append("- one page per routine: `routines/` next to this page")
    out.append("")
    if changes is not None:
        n = sum(len(v) for v in changes.values())
        out += ["### Changed since the previous snapshot", ""]
        if not n:
            out += ["none", ""]
        else:
            for label in ("added", "removed", "changed"):
                for item in changes[label][:MAX_CHANGES_SHOWN]:
                    out.append(f"- {label}: {md_inline(item)}")
                if len(changes[label]) > MAX_CHANGES_SHOWN:
                    out.append(f"- {label}: ... {len(changes[label]) - MAX_CHANGES_SHOWN} more")
            out.append("")
    return out


def _link_targets(rows: list[dict[str, Any]], names: dict[tuple[str, str, str], str]) -> dict[str, str]:
    """qualified (lower) name -> page file name, only for names that map to exactly one routine page."""
    seen: dict[str, list[str]] = {}
    for r in rows:
        seen.setdefault(qname(r).lower(), []).append(names[row_key(r)])
    return {k: v[0] for k, v in seen.items() if len(v) == 1}


def routine_pages(result: dict[str, Any], store: RoutineStore, edges: dict[str, Any] | None = None) -> dict[str, str]:
    """{`<source>/routines/<name>.md`: markdown} for every routine of the result (paths relative to platform/)."""
    from de_agent.scanners.edges import callers
    from de_agent.scanners.routines import routine_id

    data = result.get("data") or {}
    rows = data.get("routines") or []
    source = str(result["source"])
    src_dir = safe_component(source)
    names = page_names(rows)
    targets = _link_targets(rows, names)
    pages: dict[str, str] = {}
    for r in rows:
        fname = names[row_key(r)]
        body = store.get(r.get("body_hash") or "") if r.get("body_hash") else None
        rid = routine_id(source, r)
        called_by = callers(edges, rid) if edges else []
        title = f"{r['kind']} {md_inline(qname(r))}{md_inline(r.get('args') or '')}"
        lines = [
            "---", f"source: {md_inline(source)}", "type: routine", f"kind: {r['kind']}",
            f"routine: {md_inline(qname(r))}", f"generated: {result['collected_at']}", "---", "",
            f"# {title}", "", "_Generated by dea snapshot; do not edit._", "",
            f"Part of [{md_inline(source)}](../../{src_dir}.md).", "",
            "| field | value |", "|---|---|",
            f"| kind | {r['kind']} |", f"| language | {md_inline(r.get('language') or '-')} |",
            f"| args | {md_inline(r.get('args') or '-')} |", f"| returns | {md_inline(r.get('returns') or '-')} |",
            f"| owner | {md_inline(r.get('owner') or '-')} |", f"| body hash | {(r.get('body_hash') or '-')[:16]} |",
            f"| size | {r.get('body_bytes', 0)} bytes{' (truncated)' if r.get('truncated') else ''}"
            f"{' (redacted)' if r.get('redacted') else ''} |",
            f"| parse | {r.get('parse', 'none')} |", "",
        ]
        for label, key in (("Reads", "reads"), ("Writes", "writes")):
            vals = r.get(key) or []
            lines += [f"## {label}", ""] + ([f"- `{md_inline(v)}`" for v in vals] or ["none"]) + [""]
        calls = r.get("calls") or []
        lines += ["## Calls", ""]
        for c in calls:
            tgt = next((v for k, v in targets.items() if k == str(c).lower() or k.endswith("." + str(c).lower())), None)
            lines.append(f"- [{md_inline(c)}]({tgt}.md)" if tgt else f"- `{md_inline(c)}`")
        if not calls:
            lines.append("none")
        lines += ["", "## Called by", ""]
        lines += [f"- `{md_inline(c)}`" for c in called_by] or ["none known (run `dea scan` to resolve callers)"]
        lines += ["", "## Body", ""]
        if body:
            fence = fence_for(body)
            lines += [f"{fence}sql", body, fence]
        else:
            lines += ["_no body (the engine exposes none, or the login cannot read it)_"]
        pages[f"{src_dir}/routines/{fname}.md"] = "\n".join(lines).rstrip("\n") + "\n"
    return pages
