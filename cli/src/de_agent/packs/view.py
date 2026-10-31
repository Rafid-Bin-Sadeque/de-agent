"""Rendering of pack results: signals and page body from the result's own `meta` (self-contained, deterministic)."""
from __future__ import annotations

import json
from typing import Any

MAX_TABLE_ROWS = 50
SEVERITY_PREFIX = {"info": "[info] ", "warn": "", "error": "[error] "}


def signals(data: dict[str, Any]) -> list[str]:
    meta = data.get("meta") or {}
    return [SEVERITY_PREFIX.get(s.get("severity", "warn"), "") + str(s.get("text", "")) for s in meta.get("signals") or []]


def _val(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".")
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (dict, list)):
        v = json.dumps(v, sort_keys=True, default=str, separators=(",", ":"))
    s = str(v).replace("\n", " ").replace("|", "\\|").strip()
    return (s[:117] + "...") if len(s) > 120 else s


def _title(name: str, spec: dict[str, Any]) -> str:
    return spec.get("title") or name.replace("_", " ")


def body(data: dict[str, Any]) -> list[str]:
    from de_agent.render import _table

    meta = data.get("meta") or {}
    out: list[str] = []
    if meta.get("pack"):
        note = " (fixtures only: not yet run against a live system)" if meta.get("validated") == "fixtures" else ""
        out += [f"_Pack `{meta['pack']}` v{meta.get('pack_version', 1)}, {meta.get('kind', '')}, "
                f"validated: {meta.get('validated', 'fixtures')}{note}._", ""]
    truncated = meta.get("truncated") or {}
    for name, spec in (meta.get("sections") or {}).items():
        if name not in data:
            continue
        rows = data[name] or []
        out += [f"## {_title(name, spec)}", ""]
        if not rows:
            out += ["none", ""]
            continue
        shown = rows[:MAX_TABLE_ROWS]
        cols = spec.get("columns") or list(dict.fromkeys(k for r in shown for k in r))
        if spec.get("render") == "list":
            for r in shown:
                out.append("- " + ", ".join(f"{c}: {_val(r.get(c))}" for c in cols))
        else:
            out += _table(cols, [[_val(r.get(c)) for c in cols] for r in shown])
        more = len(rows) - len(shown) + max(truncated.get(name, 0) - len(rows), 0)
        if more > 0:
            out.append(f"_(+{more} more not shown)_")
        out.append("")
    return out
