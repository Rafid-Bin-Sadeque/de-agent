"""Table-centric lineage graph joined from every scan (M1c section 2, edges.py)."""
from __future__ import annotations

import fnmatch
import re
from collections import deque
from typing import Any

MAX_DEPTH = 3


def _add(tables: dict[str, dict[str, dict[str, Any]]], table: str, side: str, edge: dict[str, Any]) -> None:
    cur = tables.setdefault(table, {"writers": {}, "readers": {}})[side]
    cur.setdefault((edge["id"], edge["via"], edge.get("proc", ""), edge.get("source", ""), edge.get("target", "")), edge)


def _proc_index(sql_results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    idx: dict[str, dict[str, Any]] = {}
    for r in sql_results:
        for o in r["objects"]:
            if o["kind"] in ("procedure", "function"):
                idx[o["name"]] = {**o, "project": r["project"], "scan": r["scan"]}
    return idx


def _find_proc(idx: dict[str, dict[str, Any]], name: str) -> dict[str, Any] | None:
    if name in idx:
        return idx[name]
    hits = [v for k, v in idx.items() if k.endswith("." + name) or name.endswith("." + k)]
    return hits[0] if len(hits) == 1 else None


def s3_node(bucket: str, prefix: str = "") -> str:
    """Normalized S3 node key: lowercase, `s3://bucket/prefix/` with a trailing slash. Firehose `!{...}` expressions
    are cut: only the static head of the prefix names the node (cut back to a whole path segment)."""
    head = prefix.lstrip("/")
    if "!{" in head:
        head = head.split("!{", 1)[0]
        head = head.rsplit("/", 1)[0] + "/" if "/" in head else ""
    if head and not head.endswith("/"):
        head += "/"
    return f"s3://{bucket}/{head}".lower()


def _like(pattern: str, value: str) -> bool:
    return fnmatch.fnmatchcase(value.lower(), pattern.lower().replace("%", "*"))


def _is_pattern(*parts: str) -> bool:
    return any("%" in p or "*" in p for p in parts)


def _rename(rules: list[dict[str, Any]], schema: str, table: str) -> tuple[str, str, list[str]]:
    """Apply DMS `rename` transformations (their locators name the SOURCE object); other transformation actions are
    returned by name and never guessed."""
    skipped: list[str] = []
    new_schema, new_table = schema, table
    for r in rules:
        if r.get("type") != "transformation" or r.get("target") not in ("schema", "table"):
            continue
        if not _like(r.get("schema", "%"), schema):
            continue
        if r.get("target") == "table" and not _like(r.get("table", "%"), table):
            continue
        if r.get("action") != "rename":
            skipped.append(str(r.get("action")))
        elif r.get("value"):
            if r["target"] == "schema":
                new_schema = str(r["value"])
            else:
                new_table = str(r["value"])
    return new_schema, new_table, skipped


def _aws_edges(platform: list[dict[str, Any]], put_node: Any, unresolved: list[dict[str, str]]) -> dict[str, int]:
    """DMS / Firehose / Glue edges from the latest `type: aws` snapshots (see M3 section 3)."""
    n = {"dms_tasks": 0, "firehose_streams": 0, "glue_tables": 0}
    s3_written: dict[str, bool] = {}
    glue: list[tuple[str, str, dict[str, Any]]] = []
    for snap in sorted(platform, key=lambda r: r["source"]):
        src, data = snap["source"], snap.get("data") or {}
        ref = f"snapshot {snap.get('snapshot_id', '?')}"
        for t in data.get("dms_tasks") or []:
            n["dms_tasks"] += 1
            did = f"dms:{t['id']}"
            base = {"id": did, "task": str(t["id"]), "project": src, "scan": src, "via": "dms", "file": f"{ref} dms_tasks"}
            rules = t.get("rules") or []
            target = t.get("target") or {}
            for r in rules:
                if r.get("type") != "selection" or r.get("action") != "include":
                    continue
                sch, tbl = str(r.get("schema") or "%"), str(r.get("table") or "%")
                pattern = _is_pattern(sch, tbl)
                tsch, ttbl, skipped = _rename(rules, sch, tbl)
                extra = {"pattern": True} if pattern else {}
                if skipped:
                    unresolved.append({"kind": "dms-transform", "name": f"{sorted(set(skipped))[0]} on {sch}.{tbl}",
                                       "referenced_by": did})
                skey = f"{sch}.{tbl}".lower()
                if target.get("bucket"):
                    folder = str(target.get("folder") or "").strip("/")
                    tkey = s3_node(target["bucket"], "/".join(x for x in (folder, tsch, ttbl) if x) + "/")
                    s3_written[tkey] = True
                elif target.get("engine"):
                    tkey = f"{tsch}.{ttbl}".lower()
                else:
                    unresolved.append({"kind": "dms-target", "name": str(t["id"]), "referenced_by": did})
                    put_node(skey, "readers", {**base, **extra})
                    continue
                # one table-pair edge per rule: source -> target, the task id is an attribute, not a shared node
                put_node(skey, "readers", {**base, **extra, "target": tkey})
                put_node(tkey, "writers", {**base, **extra, "source": skey})
        for f in data.get("firehose_streams") or []:
            if not f.get("bucket"):
                continue
            n["firehose_streams"] += 1
            key = s3_node(f["bucket"], str(f.get("prefix") or ""))
            s3_written[key] = True
            put_node(key, "writers", {"id": f"firehose:{f['stream']}", "project": src, "scan": src, "via": "firehose",
                                      "file": f"{ref} firehose_streams"})
        for t in data.get("glue_tables") or []:
            if t.get("location", "").startswith("s3://"):
                glue.append((src, ref, t))
    for src, ref, t in sorted(glue, key=lambda x: (x[0], x[2]["database"], str(x[2]["name"]))):
        m = re.match(r"^s3://([^/]+)/?(.*)$", t["location"])
        loc = s3_node(m.group(1), m.group(2))
        gid = f"glue:{t['database']}.{t['name']}".lower()
        hit = False
        for node in sorted(s3_written):
            if node.startswith(loc) or loc.startswith(node):  # the table's location and the written prefix overlap
                put_node(node, "readers", {"id": gid, "project": src, "scan": src, "via": "glue catalog",
                                           "file": f"{ref} glue_tables"})
                hit = True
        if hit:
            n["glue_tables"] += 1
            # the catalog table becomes a table node other readers (athena, dbt sources) can hang off
            put_node(f"{t['database']}.{t['name']}".lower(), "writers", {
                "id": gid, "project": src, "scan": src, "via": "glue catalog", "file": f"{ref} glue_tables"})
    return n


def build_edges(results: list[dict[str, Any]], platform: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    tables: dict[str, dict[str, dict[str, Any]]] = {}
    unresolved: list[dict[str, str]] = []
    dag_links: list[dict[str, str]] = []
    unqualified = 0
    sql_results = [r for r in results if r["type"] == "sql"]
    procs = _proc_index(sql_results)

    def put(table: str, side: str, edge: dict[str, Any]) -> None:
        nonlocal unqualified
        if "." not in table:
            unqualified += 1
            return
        _add(tables, table, side, edge)

    for r in sorted(results, key=lambda r: r["scan"]):
        proj, scan = r["project"], r["scan"]
        if r["type"] == "airflow":
            outlets: dict[str, list[str]] = {}
            inlets: dict[str, list[str]] = {}
            by_dag_ids = {d["dag_id"] for d in r["dags"]}
            for d in r["dags"]:
                did = f"dag:{d['dag_id']}"
                base = {"id": did, "project": proj, "scan": scan, "file": f"{d['file']}:{d['line']}"}
                covered_w: set[str] = set()
                covered_r: set[str] = set()
                for rec in d["sql"]:
                    via = "unrolled" if rec.get("unrolled") else "sql"
                    for t in rec["writes"]:
                        covered_w.add(t)
                        put(t, "writers", {**base, "via": via, "file": f"{d['file']}:{rec['line']}"})
                    for t in rec["reads"]:
                        covered_r.add(t)
                        put(t, "readers", {**base, "via": via, "file": f"{d['file']}:{rec['line']}"})
                for t in d["writes"]:
                    if t not in covered_w:
                        put(t, "writers", {**base, "via": "sql_file"})
                for t in d["reads"]:
                    if t not in covered_r:
                        put(t, "readers", {**base, "via": "sql_file"})
                for ds in d["dataset_writes"]:
                    e = {**base, "via": "dataset"}
                    if ds["unrolled"]:
                        e["unrolled"] = True
                    put(ds["table"], "writers", e)
                for ds in d["dataset_reads"]:
                    e = {**base, "via": "dataset"}
                    if ds["unrolled"]:
                        e["unrolled"] = True
                    put(ds["table"], "readers", e)
                for name in d["calls"]:
                    p = _find_proc(procs, name)
                    if p is None:
                        unresolved.append({"kind": "proc", "name": name, "referenced_by": did})
                        continue
                    for t in p["writes"]:
                        put(t, "writers", {**base, "via": "proc", "proc": p["name"]})
                    for t in p["reads"]:
                        put(t, "readers", {**base, "via": "proc", "proc": p["name"]})
                for u in d["outlets"]:
                    outlets.setdefault(u, []).append(d["dag_id"])
                for u in d["inlets"]:
                    inlets.setdefault(u, []).append(d["dag_id"])
                for s in d["sensors"]:
                    dag_links.append({"from": f"dag:{s}", "to": did, "via": "sensor",
                                      **({} if s in by_dag_ids else {"external": "true"})})
                for t_ in d["triggers"]:
                    dag_links.append({"from": did, "to": f"dag:{t_}", "via": "trigger"})
            for u, producers in outlets.items():
                for prod in producers:
                    for cons in inlets.get(u, []):
                        dag_links.append({"from": f"dag:{prod}", "to": f"dag:{cons}", "via": f"dataset {u}"})
        elif r["type"] == "dbt":
            for m in r["models"]:
                mid = f"dbt:{m['name']}"
                base = {"id": mid, "project": proj, "scan": scan, "via": "dbt", "file": m["file"]}
                put(m["relation"], "writers", base)
                for p in m["parents"]:
                    put(p, "readers", base)
            for e in r["exposures"]:
                for t in e["reads"]:
                    put(t, "readers", {"id": f"exposure:{e['name']}", "project": proj, "scan": scan, "via": "dbt",
                                       "file": e["file"]})
        elif r["type"] == "sql":
            for o in r["objects"]:
                oid = f"{o['kind'].split()[-1] if o['kind'] != 'materialized view' else 'mview'}:{o['name']}"
                base = {"id": oid, "project": proj, "scan": scan, "via": "sql", "file": f"{o['file']}:{o['line']}"}
                if o["kind"] not in ("procedure", "function"):
                    put(o["name"], "writers", base)
                else:
                    for t in o["writes"]:
                        put(t, "writers", {**base, "heuristic": True} if o.get("heuristic") else base)
                for t in o["reads"]:
                    put(t, "readers", {**base, "heuristic": True} if o.get("heuristic") else base)
    aws_counts: dict[str, int] = {}
    if platform:
        def put_node(key: str, side: str, edge: dict[str, Any]) -> None:
            _add(tables, key, side, edge)

        aws_counts = _aws_edges([p for p in platform if p.get("type") == "aws"], put_node, unresolved)
    out_tables: dict[str, Any] = {}
    for t in sorted(tables):
        w = sorted(tables[t]["writers"].values(), key=lambda e: (e["id"], e["via"], e.get("proc", "")))
        rd = sorted(tables[t]["readers"].values(), key=lambda e: (e["id"], e["via"], e.get("proc", "")))
        out_tables[t] = {"writers": w, "readers": rd}
        if rd and not w:
            unresolved.append({"kind": "table", "name": t,
                               "referenced_by": ", ".join(dict.fromkeys(e["id"] for e in rd))})
    cross = []
    for t, v in out_tables.items():
        for w in {e["id"]: e for e in v["writers"]}.values():
            for rd in {e["id"]: e for e in v["readers"]}.values():
                if w["project"] != rd["project"]:
                    cross.append({"table": t, "writer": w["id"], "writer_project": w["project"], "reader": rd["id"],
                                  "reader_project": rd["project"]})
    unresolved.sort(key=lambda u: (u["kind"], u["name"], u["referenced_by"]))
    unresolved_dedup = [u for i, u in enumerate(unresolved) if u not in unresolved[:i]]
    stats: dict[str, Any] = {"tables": len(out_tables), "unqualified_tables_skipped": unqualified,
                             "cross_project_edges": len(cross), "unresolved_names": len(unresolved_dedup)}
    for k, v in aws_counts.items():
        stats[f"aws.{k}"] = v
    for r in results:
        for k, v in r.get("stats", {}).items():
            if k.startswith(("unresolved", "unparsed", "unattributed")):
                stats[f"{r['scan']}.{k}"] = v
    return {"tables": out_tables, "cross_project": cross, "dag_links": sorted(
        dag_links, key=lambda x: (x["from"], x["to"], x["via"])), "unresolved": unresolved_dedup, "stats": stats}


def downstream(edges: dict[str, Any], table: str, depth: int = MAX_DEPTH) -> list[dict[str, Any]]:
    """Readers of `table` and, transitively, of the tables those readers write; a reader's depth = its hop count."""
    tables = edges["tables"]
    written_by: dict[str, set[str]] = {}
    for t, v in tables.items():
        for w in v["writers"]:
            written_by.setdefault(w["id"], set()).add(t)
    out: list[dict[str, Any]] = []
    seen_nodes: set[tuple[str, str]] = set()
    q: deque[tuple[str, int, list[str]]] = deque([(table, 0, [table])])
    seen_tables = {table}
    while q:
        t, d, path = q.popleft()
        if d + 1 > depth:
            continue
        own = {w["id"] for w in tables.get(t, {}).get("writers", [])}
        for rd in tables.get(t, {}).get("readers", []):
            if (rd["id"], t) in seen_nodes or rd["id"] in own:  # a reader that also writes the table (watermark/self-read) is not downstream of it
                continue
            seen_nodes.add((rd["id"], t))
            out.append({"depth": d + 1, "reader": rd["id"], "project": rd["project"], "reads": t,
                        "path": [*path, f"[{rd['id']}]"]})
            nxt = [rd["target"]] if rd.get("target") else sorted(written_by.get(rd["id"], ()))  # DMS edges are table pairs
            for t2 in nxt:
                if t2 not in seen_tables:
                    seen_tables.add(t2)
                    q.append((t2, d + 1, [*path, f"[{rd['id']}]", t2]))
    return out
