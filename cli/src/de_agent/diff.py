"""Snapshot vs previous snapshot -> "what changed" (M1a section 7). Generic over collectors."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from de_agent.collectors import get_collector
from de_agent.render import human_bytes, human_duration, signals_for
from de_agent.runner import result_status
from de_agent.snapshot import list_snapshots, load_result, snapshots_dir

REL_THRESHOLD = 0.20
ABS_BYTES = 100 * 1024 * 1024
ABS_SECONDS = 60
ABS_COUNT = 1000


RECOVERED_MARK = " (recovered:"


class DiffError(Exception):
    pass


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _abs_threshold(field: str) -> float:
    if field.endswith("_bytes"):
        return ABS_BYTES
    if field.endswith(("_s", "_seconds")):
        return ABS_SECONDS
    return ABS_COUNT


def numeric_change_significant(field: str, old: float, new: float) -> bool:
    """|delta| >= 20% AND an absolute delta that matters (bytes >= 100 MB, counts >= 1,000, seconds >= 60)."""
    if field.endswith("_pct"):
        return False  # derived from counts that are compared themselves
    delta = abs(new - old)
    if delta < _abs_threshold(field):
        return False
    return old == 0 or delta / abs(old) >= REL_THRESHOLD


def _fmt(field: str, v: Any) -> str:
    if v is None:
        return "-"
    if _is_num(v):
        if field.endswith("_bytes"):
            return human_bytes(v)
        if field.endswith(("_s", "_seconds")):
            return human_duration(v)
        return f"{v:,}" if isinstance(v, int) else f"{v:,.1f}"
    return str(v)


def _change(field: str, old: Any, new: Any) -> dict[str, Any] | None:
    if old == new:
        return None
    if _is_num(old) and _is_num(new):
        if not numeric_change_significant(field, old, new):
            return None
        pct = f" ({(new - old) / abs(old) * 100:+.0f}%)" if old else ""
        return {"field": field, "old": old, "new": new, "text": f"{field} {_fmt(field, old)} -> {_fmt(field, new)}{pct}"}
    return {"field": field, "old": old, "new": new, "text": f"{field} {_fmt(field, old)} -> {_fmt(field, new)}"}


def _key(row: dict[str, Any], keys: list[str]) -> tuple[Any, ...]:
    return tuple(row.get(k) for k in keys)


def _key_label(k: tuple[Any, ...]) -> str:
    return ".".join(str(x) for x in k)


def _row_hint(row: dict[str, Any]) -> str:
    bits = []
    for f, v in row.items():
        if f.endswith("_bytes") and _is_num(v):
            bits.append(_fmt(f, v))
            break
    if row.get("blocker_query") is not None:
        return f" ({str(row['blocker_query'])[:70]!r} blocks {str(row.get('blocked_query'))[:50]!r})"
    if row.get("state") and ("run_id" in row or "try" in row):
        bits.append(str(row["state"]))
        if _is_num(row.get("duration_s")):
            bits.append(human_duration(row["duration_s"]))
    return f" ({', '.join(bits)})" if bits else ""


def _norm_signal(s: str) -> str:
    return re.sub(r"\d+(\.\d+)?", "#", s)


def _dbt_recovery(od: dict[str, Any], nd: dict[str, Any], out: dict[str, Any]) -> None:
    """Failing nodes/tests of the old snapshot that a LATER dbt run executed successfully are recovered, not just cleared."""
    old_fail = {str(n["unique_id"]) for n in od.get("failing_nodes") or []}
    old_fail |= {str(t["unique_id"]) for t in od.get("tests") or [] if t.get("status") in FAIL_STATUSES}
    o_at, n_lr = (od.get("last_run") or {}).get("invocation_time"), nd.get("last_run") or {}
    if not old_fail or not o_at or not n_lr.get("invocation_time") or str(n_lr["invocation_time"]) <= str(o_at):
        return
    ok = set(n_lr.get("ok_ids") or [])
    healed = sorted(old_fail & ok)
    if not healed:
        return
    when = str(n_lr["invocation_time"]).replace("T", " ")[:16]
    text = f"dbt {', '.join(healed[:3])}{'...' if len(healed) > 3 else ''} (recovered: later {n_lr.get('command') or 'run'} succeeded at {when})"
    out["signals_recovered"].append(text)
    if len(healed) == len(old_fail):  # everything that failed is clean again: the old failure signals are superseded
        out["signals_cleared"] = [c for c in out["signals_cleared"] if not c.startswith("dbt last run:")
                                  and "dbt test(s) failing" not in c]


FAIL_STATUSES = {"fail", "error", "failed"}


def diff_results(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Structured diff of two results of the same source."""
    collector = get_collector(new["type"])
    volatile = set(getattr(collector, "volatile_fields", set()))
    windowed = set(getattr(collector, "windowed_sections", set()))  # sliding windows: rows age out, not "removed"
    ranking = set(getattr(collector, "ranking_sections", set()))  # top-N lists: entering/leaving is not news
    od, nd = old.get("data") or {}, new.get("data") or {}
    out: dict[str, Any] = {"signals_new": [], "signals_cleared": [], "signals_recovered": [], "scalars": [], "lists": {}}

    old_sig, new_sig = signals_for(old), signals_for(new)
    old_norm, new_norm = {_norm_signal(s) for s in old_sig}, {_norm_signal(s) for s in new_sig}
    out["signals_new"] = [s for s in new_sig if _norm_signal(s) not in old_norm]
    out["signals_cleared"] = [s for s in old_sig if _norm_signal(s) not in new_norm]
    # a failure signal that turned into its "(recovered: ...)" form is one RECOVERED line, not NEW + cleared
    for s in list(out["signals_new"]):
        if RECOVERED_MARK in s:
            base = _norm_signal(s.split(RECOVERED_MARK)[0].rstrip())
            out["signals_new"].remove(s)
            out["signals_cleared"] = [c for c in out["signals_cleared"] if _norm_signal(c) != base]
            if base not in {_norm_signal(x.split(RECOVERED_MARK)[0].rstrip()) for x in old_sig if RECOVERED_MARK in x}:
                out["signals_recovered"].append(s)
    if new.get("type") == "dbt":
        _dbt_recovery(od, nd, out)

    reused = getattr(collector, "reused_data", None)  # sections the NEW result copied from an earlier snapshot
    skip, skip_fields = reused(nd) if reused else (set(), {})

    estimated = set((od.get("meta") or {}).get("sizes_estimated") or []) | \
        set((nd.get("meta") or {}).get("sizes_estimated") or [])
    for section, keys in collector.list_keys.items():
        if section not in od or section not in nd or section in skip:
            continue
        vol = volatile
        if section in estimated:  # sizes came from relpages estimates on one side: byte fields are not comparable
            vol = volatile | {f for r in (od[section] or []) + (nd[section] or []) for f in r if f.endswith("_bytes")}
        orows = {_key(r, keys): r for r in od[section] or []}
        nrows = {_key(r, keys): r for r in nd[section] or []}
        added = [{"key": _key_label(k), "text": _key_label(k) + _row_hint(nrows[k])}
                 for k in sorted(nrows.keys() - orows.keys(), key=str)] if section not in ranking else []
        removed = [{"key": _key_label(k), "text": _key_label(k) + _row_hint(orows[k])}
                   for k in sorted(orows.keys() - nrows.keys(), key=str)] if section not in windowed | ranking else []
        changed = []
        for k in sorted(orows.keys() & nrows.keys(), key=str):
            fields = sorted((set(orows[k]) | set(nrows[k])) - set(keys) - vol)
            ch = [c for f in fields if (c := _change(f, orows[k].get(f), nrows[k].get(f)))]
            if ch:
                changed.append({"key": _key_label(k), "changes": ch})
        if added or removed or changed:
            out["lists"][section] = {"added": added, "removed": removed, "changed": changed}

    for section in sorted(set(od) & set(nd)):
        o, n = od[section], nd[section]
        if section in collector.list_keys or section == "meta" or not isinstance(o, dict) or not isinstance(n, dict):
            continue
        if section in skip:
            continue
        for f in sorted((set(o) | set(n)) - volatile - {"uptime_s"} - skip_fields.get(section, set())):
            if isinstance(o.get(f), (dict, list)) or isinstance(n.get(f), (dict, list)):
                continue
            c = _change(f, o.get(f), n.get(f))
            if c:
                out["scalars"].append({"section": section, **c})
        if section == "server" and _is_num(o.get("uptime_s")) and _is_num(n.get("uptime_s")) \
                and n["uptime_s"] < o["uptime_s"]:
            out["scalars"].append({"section": section, "field": "uptime_s", "old": o["uptime_s"], "new": n["uptime_s"],
                                   "text": "server restarted (uptime reset)"})
    return out


def _empty(d: dict[str, Any]) -> bool:
    return not (d["signals_new"] or d["signals_cleared"] or d["signals_recovered"] or d["scalars"] or d["lists"])


def _usable(hub: Path, sid: str, source: str) -> dict[str, Any] | None:
    r = load_result(hub, sid, source)
    return r if r and result_status(r) != "failed" else None


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def build_diff(hub: Path, source: str | None = None, since: str | None = None) -> dict[str, Any]:
    snaps = list_snapshots(hub)
    if not snaps:
        raise DiffError("no snapshots yet: run `dea snapshot` first")
    if since and since not in snaps:
        raise DiffError(f"unknown snapshot id {since!r}; known: {', '.join(snaps[-5:])}")
    names: list[str] = []
    for sid in snaps:
        for p in sorted((snapshots_dir(hub) / sid).glob("*.json")):
            if p.stem not in names:
                names.append(p.stem)
    if source:
        if source not in names:
            raise DiffError(f"no snapshot data for source {source!r}")
        names = [source]
    report: dict[str, Any] = {"sources": {}}
    for name in sorted(names):
        usable = [sid for sid in snaps if _usable(hub, sid, name)]
        if not usable:
            continue
        new_id = usable[-1]
        if since:
            if since >= new_id or _usable(hub, since, name) is None:
                report["sources"][name] = {"status": "no-baseline"}
                continue
            old_id = since
        else:
            if len(usable) < 2:
                report["sources"][name] = {"status": "no-baseline"}
                continue
            old_id = usable[-2]
        old, new = _usable(hub, old_id, name), _usable(hub, new_id, name)
        assert old and new
        d = diff_results(old, new)
        d.update(status="same" if _empty(d) else "changed", old_id=old_id, new_id=new_id,
                 old_at=old["collected_at"], new_at=new["collected_at"],
                 age=human_duration((_parse(new["collected_at"]) - _parse(old["collected_at"])).total_seconds()))
        report["sources"][name] = d
    return report


def render_markdown(report: dict[str, Any]) -> str:
    srcs = report["sources"]
    compared = {n: d for n, d in srcs.items() if d["status"] in ("same", "changed")}
    if not compared:
        return "No earlier snapshot to compare against yet.\n"
    olds = {d["old_at"] for d in compared.values()}
    first = next(iter(compared.values()))
    since = f"{first['old_at'].replace('T', ' ').rstrip('Z')[:16]} UTC ({first['age']})"
    if all(d["status"] == "same" for d in compared.values()):
        return f"No changes since {since}.\n" if len(olds) == 1 else "No changes since the previous snapshot.\n"
    lines = [f"# What changed since {since}" if len(olds) == 1 else "# What changed", ""]
    for name, d in compared.items():
        lines.append(f"## {name}" + ("" if len(olds) == 1 else f" (since {d['old_at']}, {d['age']})"))
        if d["status"] == "same":
            lines += ["- no changes", ""]
            continue
        for s in d["signals_new"]:
            lines.append(f"- NEW signal: {s}")
        for s in d["signals_recovered"]:
            lines.append(f"- RECOVERED signal: {s}")
        for s in d["signals_cleared"]:
            lines.append(f"- cleared signal: {s}")
        for c in d["scalars"]:
            lines.append(f"- {c['section']}: {c['text']}")
        for section, ch in d["lists"].items():
            for r in ch["added"]:
                lines.append(f"- {section}: added {r['text']}")
            for r in ch["removed"]:
                lines.append(f"- {section}: removed {r['text']}")
            for r in ch["changed"]:
                lines.append(f"- {section} {r['key']}: " + "; ".join(c["text"] for c in r["changes"]))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
