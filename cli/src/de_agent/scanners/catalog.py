"""Render catalog/** pages (M1c section 3). Pure functions: same inputs -> byte-identical output, no wall-clock time."""
from __future__ import annotations

import json
from typing import Any

SPLIT_OVER = 300
CAP = 12


def _fm(rev: str, **kv: str) -> str:
    lines = ["---", f"generated: {rev}"] + [f"{k}: {v}" for k, v in kv.items()] + ["---", ""]
    return "\n".join(lines)


def _list(items: list[str], cap: int = CAP) -> str:
    if not items:
        return "—"
    shown = items[:cap]
    return ", ".join(shown) + (f" +{len(items) - cap} more" if len(items) > cap else "")


def _dag_line(d: dict[str, Any]) -> str:
    name = d["dag_id"] + (" [dynamic]" if d["dynamic"] else "")
    ins = [*d["inlets"], *[f"sensor:{s}" for s in d["sensors"]]]
    outs = [*d["outlets"], *[f"trigger:{t}" for t in d["triggers"]]]
    return " · ".join([
        name, d["schedule"] or "—", d["owner"] or "—", ",".join(d["tags"]) or "—", f"{d['file']}:{d['line']}",
        f"in: {_list(ins)}", f"out: {_list(outs)}", f"writes: {_list(d['writes'] + [x['table'] for x in d['dataset_writes'] if x['table'] not in d['writes']])}",
        f"reads: {_list(d['reads'] + [x['table'] for x in d['dataset_reads'] if x['table'] not in d['reads']])}",
    ])


def _airflow_section(dags: list[dict[str, Any]]) -> list[str]:
    by_tag: dict[str, list[dict[str, Any]]] = {}
    for d in dags:
        by_tag.setdefault(d["tags"][0] if d["tags"] else "(untagged)", []).append(d)
    out: list[str] = []
    for tag in sorted(by_tag):
        out += [f"### {tag}", ""]
        out += [f"- {_dag_line(d)}" for d in sorted(by_tag[tag], key=lambda d: (d["dag_id"], d["file"]))]
        out.append("")
    return out


def render_airflow(res: dict[str, Any]) -> dict[str, str]:
    scan, rev = res["scan"], res["revision"]
    dags = res["dags"]
    st = res["stats"]
    summary = (f"{st['dags']} DAGs ({st['dynamic_dags']} dynamic) in {st['dag_files']} files; "
               f"unresolved: {st['unresolved_calls']} calls, {st['unresolved_sql']} dynamic SQL, "
               f"{st['unresolved_datasets']} datasets, {st['unparsed_sql']} unparsed SQL.")
    folders: dict[str, list[dict[str, Any]]] = {}
    for d in dags:
        folders.setdefault(d["folder"], []).append(d)
    head = _fm(rev, scan=scan, type="airflow")
    if len(dags) <= SPLIT_OVER:
        body = [f"# Airflow catalog: {scan}", "", summary, ""]
        for f in sorted(folders):
            body += [f"## {f}", ""] + _airflow_section(folders[f])
        return {f"catalog/airflow/{scan}.md": head + "\n".join(body).rstrip("\n") + "\n"}
    files: dict[str, str] = {}
    idx = [f"# Airflow catalog: {scan}", "", summary, "", "Split by folder (over 300 DAGs):", ""]
    for f in sorted(folders):
        slug = "root" if f == "." else f.replace("/", "_")
        idx.append(f"- [{f}]({slug}.md): {len(folders[f])} DAGs")
        body = [f"# Airflow catalog: {scan} / {f}", ""] + _airflow_section(folders[f])
        files[f"catalog/airflow/{scan}/{slug}.md"] = _fm(rev, scan=scan, type="airflow", folder=f) + "\n".join(body).rstrip("\n") + "\n"
    files[f"catalog/airflow/{scan}/index.md"] = head + "\n".join(idx).rstrip("\n") + "\n"
    return files


def render_symbols(res: dict[str, Any]) -> dict[str, str]:
    scan = res["scan"]
    rows = []
    for name, s in res["symbols"].items():
        ids = sorted({d["dag_id"] for d in s["dags"]})
        rows.append((-len(ids), name, s, ids))
    rows.sort(key=lambda r: (r[0], r[1]))
    lines = [f"# Symbol index: {scan}", "",
             f"{len(rows)} util symbols; `used by` = DAGs in files that reference the symbol.", ""]
    for neg, name, s, ids in rows:
        shown = ", ".join(ids[:8]) + ("…" if len(ids) > 8 else "")
        lines.append(f"- {name} ({s['file']}:{s['line']}) — {s['doc'] or '—'} — used by {-neg} DAGs" + (f": {shown}" if ids else ""))
    return {f"catalog/symbols/{scan}.md": _fm(res["revision"], scan=scan, type="symbols") + "\n".join(lines) + "\n"}


def render_dbt(res: dict[str, Any], scan: str) -> dict[str, str]:
    lines = [f"# dbt catalog: {scan}", "",
             f"{res['stats']['models']} models, {res['stats']['sources']} sources, {res['stats']['exposures']} exposures "
             f"(dbt {res.get('dbt_version')}, manifest {res['manifest_sha']}).", ""]
    for m in res["models"]:
        lines.append(f"- {m['name']} · {m['materialization']} · {m['relation']} · upstream: {_list(m['parents'])} · "
                     f"downstream: {m['downstream']}")
    if res["sources"]:
        lines += ["", "## Sources", ""] + [f"- {s['source']}.{s['name']} → {s['relation']}" for s in res["sources"]]
    if res["exposures"]:
        lines += ["", "## Exposures", ""] + [f"- {e['name']} · reads: {_list(e['reads'])}" for e in res["exposures"]]
    return {f"catalog/dbt/{scan}.md": _fm(res["revision"], scan=scan, type="dbt") + "\n".join(lines) + "\n"}


def render_sql(res: dict[str, Any]) -> dict[str, str]:
    scan = res["scan"]
    lines = [f"# SQL catalog: {scan}", "", f"{res['stats']['objects']} objects in {res['stats']['files']} files "
             f"({res['stats']['heuristic']} heuristic).", ""]
    for o in res["objects"]:
        mark = " [heuristic]" if o.get("heuristic") else ""
        lines.append(f"- {o['name']} · {o['kind']} ({o['file']}:{o['line']}) · reads: {_list(o['reads'])} · "
                     f"writes: {_list(o['writes'])}{mark}")
    return {f"catalog/sql/{scan}.md": _fm(res["revision"], scan=scan, type="sql") + "\n".join(lines) + "\n"}


def render_edges(edges: dict[str, Any], rev: str) -> dict[str, str]:
    lines = ["# Edges: who writes and reads each table", "",
             "Derived by `dea scan`. `←` writers, `→` readers; `dea edges <table>` for detail.", "", "## Tables", ""]
    for t, v in edges["tables"].items():
        w = ", ".join(dict.fromkeys(e["id"] for e in v["writers"])) or "—"
        r = ", ".join(dict.fromkeys(e["id"] for e in v["readers"])) or "—"
        pat = " (pattern)" if any(e.get("pattern") for e in v["writers"] + v["readers"]) else ""
        lines.append(f"{t}{pat} ← {w} | → {r}")
    lines += ["", "## Cross-project", ""]
    if edges["cross_project"]:
        for c in edges["cross_project"]:
            lines.append(f"- {c['table']}: {c['writer']} [{c['writer_project']}] → {c['reader']} [{c['reader_project']}]")
    else:
        lines.append("none")
    lines += ["", "## DAG links", ""]
    links = edges["dag_links"]
    lines += [f"- {k['from']} → {k['to']} ({k['via']}{', external' if k.get('external') else ''})" for k in links] or ["none"]
    lines += ["", "## Unresolved", "",
              "Names referenced but never written anywhere in the scanned projects: external sources or typos.", ""]
    lines += [f"- {u['kind']} {u['name']} (referenced by {u['referenced_by']})" for u in edges["unresolved"]] or ["none"]
    lines += ["", "## Counts", ""] + [f"- {k}: {v}" for k, v in sorted(edges["stats"].items())]
    md = _fm(rev, type="edges") + "\n".join(lines) + "\n"
    js = json.dumps({"generated": rev, **edges}, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    return {"catalog/edges.md": md, "catalog/edges.json": js}


def render_symbols_json(symbols_by_scan: dict[str, dict[str, Any]], rev: str) -> dict[str, str]:
    return {"catalog/symbols.json": json.dumps({"generated": rev, "scans": symbols_by_scan}, indent=1, sort_keys=True,
                                               ensure_ascii=False) + "\n"}
