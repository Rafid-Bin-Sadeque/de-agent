"""Table-centric lineage graph joined from every scan (M1c section 2, edges.py)."""
from __future__ import annotations

import fnmatch
import re
from collections import deque
from typing import Any

from de_agent.scanners.pycode import is_path

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


class Resolver:
    """Resolves a `CALL x.y` to live routines and/or a repo-scanned procedure (v0.3 section 3.2).

    Order: (1) the connection -> source mapping of the scan, (2) a unique live match, (3) a unique repo match,
    (4) unresolved, listing every candidate."""

    def __init__(self, repo: dict[str, dict[str, Any]], live: list[dict[str, Any]] | None,
                 connections: dict[str, dict[str, str]] | None) -> None:
        self.repo = repo
        self.connections = connections or {}
        self.nodes: list[dict[str, Any]] = []
        for n in live or []:
            if n["kind"] not in ("procedure", "function", "view", "materialized_view", "trigger"):
                continue
            schema = str(n.get("schema") or "")
            rq, wq = [], []
            qualified = False
            for side, out in (("reads", rq), ("writes", wq)):
                for t in n.get(side, []):
                    q, was = _qualify(t, schema)
                    qualified = qualified or was
                    out.append(q)
            self.nodes.append({"id": n["id"], "source": n["source"], "kind": n["kind"], "name": n["qname"],
                               "args": n.get("args", ""), "reads": sorted(set(rq)), "writes": sorted(set(wq)),
                               "calls": list(n.get("calls", [])), "heuristic": qualified or n.get("parse") == "heuristic",
                               "parse": n.get("parse", "none"), "live": True})

    @staticmethod
    def _match(nodes: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
        name = name.lower()
        return [n for n in nodes if n["kind"] in ("procedure", "function")
                and (n["name"] == name or n["name"].endswith("." + name))]

    def resolve(self, name: str, conn: str | None = None, scan: str | None = None,
                source: str | None = None) -> tuple[list[dict[str, Any]], list[str]]:
        """(resolved nodes, candidates). Nodes are live dicts or the repo record; empty when unresolved."""
        pool = [n for n in self.nodes if source is None or n["source"] == source]
        mapped = self.connections.get(scan or "", {}).get(conn or "") if conn else None
        if mapped:
            hit = self._match([n for n in pool if n["source"] == mapped], name)
            if hit:
                return hit, []
        live = self._match(pool, name)
        if live:
            by_q = {(n["source"], n["name"]) for n in live}
            if len(by_q) == 1:  # a unique routine (overloads of it count as one)
                return live, []
        repo = _find_proc(self.repo, name) if source is None else None
        if repo is not None:
            return [repo], []
        cands = sorted({n["id"] for n in live} | {f"repo:{v['name']}" for k, v in self.repo.items()
                                                   if k == name or k.endswith("." + name) or name.endswith("." + k)})
        return [], cands


def _qualify(name: str, schema: str) -> tuple[str, bool]:
    if "." in name or not schema or is_path(name):
        return name, False
    return f"{schema.lower()}.{name}", True


def _call_edges(put: Any, calls: list[dict[str, str]], unresolved: list[dict[str, Any]], rsv: Resolver,
                base: dict[str, Any], name: str, conn: str | None, scan: str, edge: dict[str, Any]) -> None:
    """Edges of one `CALL name` from owner `base` (a DAG / script): table edges through the routine body (as before) plus,
    for a live routine, a `calls` edge so the routine is a node on the path."""
    nodes, cands = rsv.resolve(name, conn, scan)
    if not nodes:
        u: dict[str, Any] = {"kind": "proc", "name": name, "referenced_by": base["id"]}
        if cands:
            u["candidates"] = cands
        unresolved.append(u)
        return
    for p in nodes:
        pe = {**edge, "proc": p["name"]}
        for t in p["writes"]:
            put(t, "writers", pe)
        for t in p["reads"]:
            put(t, "readers", pe)
        if p.get("live"):
            calls.append({"from": base["id"], "to": p["id"], "via": edge.get("config") or "call", "file": edge.get("file", "")})


def _live_edges(put: Any, calls: list[dict[str, str]], unresolved: list[dict[str, Any]], rsv: Resolver) -> None:
    """Table edges, calls and trigger edges of the live routines themselves."""
    for n in rsv.nodes:
        base = {"id": n["id"], "project": n["source"], "scan": n["source"], "via": "routine",
                "file": f"{n['source']}:{n['name']}"}
        if n["heuristic"]:
            base["heuristic"] = True
        if n["kind"] in ("view", "materialized_view"):
            put(n["name"], "writers", base)
        elif n["kind"] != "trigger":
            for t in n["writes"]:
                put(t, "writers", base)
        for t in n["reads"]:
            put(t, "readers", base)
        for name in n["calls"]:
            hit, cands = rsv.resolve(name, source=n["source"])
            if not hit:
                u = {"kind": "proc", "name": name, "referenced_by": n["id"]}
                if cands:
                    u["candidates"] = cands
                unresolved.append(u)
                continue
            for h in hit:
                calls.append({"from": n["id"], "to": h["id"], "via": "trigger" if n["kind"] == "trigger" else "call",
                              "file": base["file"]})
        if n["kind"] == "trigger":
            m = re.match(r"^on (.+)$", n["args"] or "")
            if m:  # the table a trigger fires on: a write to it reaches the trigger's function
                schema = n["name"].rsplit(".", 1)[0] if "." in n["name"] else ""
                tbl = f"{schema}.{m.group(1).lower()}" if schema else m.group(1).lower()
                put(tbl, "readers", base)


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


def _record_edges(put: Any, rsv: Resolver, calls: list[dict[str, str]], unresolved: list[dict[str, Any]],
                  base: dict[str, Any], rec: dict[str, Any], via: str, file: str) -> None:
    """Edges for one SQL/table record (`reads`, `writes`, `calls`) of an owner."""
    e = {**base, "via": via, "file": file}
    for k in ("hops",):
        if rec.get(k):
            e[k] = rec[k]
    if rec.get("heuristic"):
        e["heuristic"] = True
    if rec.get("unrolled"):
        e["unrolled"] = True
    for t in rec["writes"]:
        put(t, "writers", e)
    for t in rec["reads"]:
        put(t, "readers", e)
    for name in rec["calls"]:
        _call_edges(put, calls, unresolved, rsv, base, name, rec.get("conn_id"), base.get("scan", ""), e)


def build_edges(results: list[dict[str, Any]], platform: list[dict[str, Any]] | None = None,
                ignore_tables: list[str] | tuple[str, ...] = (), routines: list[dict[str, Any]] | None = None,
                connections: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    tables: dict[str, dict[str, dict[str, Any]]] = {}
    unresolved: list[dict[str, Any]] = []
    calls: list[dict[str, str]] = []
    dag_links: list[dict[str, str]] = []
    unqualified = 0
    sql_results = [r for r in results if r["type"] == "sql"]
    procs = _proc_index(sql_results)
    rsv = Resolver(procs, routines, connections)

    def put(table: str, side: str, edge: dict[str, Any]) -> None:
        nonlocal unqualified
        if "." not in table and not is_path(table):
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
                conn_of: dict[str, str | None] = {}
                for rec in d["sql"]:
                    for cname in rec.get("calls", []):
                        conn_of.setdefault(cname, rec.get("conn_id"))
                for name in d["calls"]:
                    _call_edges(put, calls, unresolved, rsv, base, name, conn_of.get(name), scan, {**base, "via": "proc"})
                for cc in d.get("config_calls", []):  # routines named by a config constant the DAG imports
                    _call_edges(put, calls, unresolved, rsv, base, cc["name"], None, scan,
                                {**base, "via": "proc", "file": cc["file"], "config": cc["via"]})
                for ue in d.get("util_edges", []):
                    _record_edges(put, rsv, calls, unresolved, base, ue, ue["via"], f"{d['file']}:{ue['line']}")
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
        elif r["type"] == "python":
            for o in r["owners"]:
                base = {"id": o["id"], "project": proj, "scan": scan}
                dvia = "notebook" if o["kind"] == "notebook" else "python"
                for rec in o["direct"]:
                    _record_edges(put, rsv, calls, unresolved, base, rec, dvia, rec["file"])
                for rec in o["util"]:
                    _record_edges(put, rsv, calls, unresolved, base, rec, rec["via"], rec["file"])
        elif r["type"] == "terraform":
            for n in r["nodes"]:
                base = {"id": n["id"], "project": proj, "scan": scan, "via": "terraform", "file": f"{n['file']}:{n['line']}"}
                for k in n["reads"]:
                    _add(tables, k, "readers", {**base, "unclear": True} if k in n["unclear"] else base)
                for k in n["writes"]:
                    _add(tables, k, "writers", base)
                for pr in n["pairs"]:
                    extra = {"pattern": True} if pr.get("pattern") else {}
                    _add(tables, pr["source"], "readers", {**base, **extra, "target": pr["target"]})
                    _add(tables, pr["target"], "writers", {**base, **extra, "source": pr["source"]})
            unresolved.extend({"kind": u["kind"], "name": u["name"], "referenced_by": u["referenced_by"]}
                              for u in r["unresolved_refs"])
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
    _live_edges(put, calls, unresolved, rsv)
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
    unresolved_dedup = [u for i, u in enumerate(unresolved) if u not in unresolved[:i]]  # noqa: E501
    stats: dict[str, Any] = {"tables": len(out_tables), "unqualified_tables_skipped": unqualified,
                             "cross_project_edges": len(cross), "unresolved_names": len(unresolved_dedup)}
    for k, v in aws_counts.items():
        stats[f"aws.{k}"] = v
    for r in results:
        for k, v in r.get("stats", {}).items():
            if k.startswith(("unresolved", "unparsed", "unattributed")):
                stats[f"{r['scan']}.{k}"] = v
    calls_out = sorted({(c["from"], c["to"], c["via"], c["file"]): c for c in calls}.values(),
                       key=lambda c: (c["from"], c["to"], c["via"], c["file"]))
    if routines is not None:
        stats["routines"] = len(rsv.nodes)
        stats["calls"] = len(calls_out)
    out = {"tables": out_tables, "cross_project": cross, "dag_links": sorted(
        dag_links, key=lambda x: (x["from"], x["to"], x["via"])), "unresolved": unresolved_dedup, "stats": stats,
        "calls": calls_out}
    if rsv.nodes:
        out["routines"] = {n["id"]: {"kind": n["kind"], "source": n["source"], "name": n["name"], "args": n["args"],
                                     "reads": n["reads"], "writes": n["writes"], "calls": n["calls"],
                                     "parse": n["parse"]} for n in rsv.nodes}
    if ignore_tables:
        out["ignore_tables"] = sorted({t.lower() for t in ignore_tables})
    return out


def downstream(edges: dict[str, Any], table: str, depth: int = MAX_DEPTH,
               ignore: list[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """Readers of `table` and, transitively, of the tables those readers write; a reader's depth = its hop count.
    Tables in `ignore` (default: `edges.ignore_tables` from the config, stored in the graph) are bookkeeping: a reader that
    writes one does not carry the traversal into it. Asking about an ignored table directly still lists its readers."""
    tables = edges["tables"]
    skip = {t.lower() for t in (edges.get("ignore_tables", []) if ignore is None else ignore)}
    written_by: dict[str, set[str]] = {}
    for t, v in tables.items():
        for w in v["writers"]:
            written_by.setdefault(w["id"], set()).add(t)
    callees: dict[str, set[str]] = {}
    for c in edges.get("calls", []):
        callees.setdefault(c["from"], set()).add(c["to"])
    if callees:  # a trigger / routine also writes what the routines it calls write
        for nid in list(callees):
            seen, stack = {nid}, list(callees[nid])
            while stack:
                cur = stack.pop()
                if cur in seen:
                    continue
                seen.add(cur)
                written_by.setdefault(nid, set()).update(written_by.get(cur, ()))
                stack.extend(callees.get(cur, ()))
    out: list[dict[str, Any]] = []
    seen_nodes: set[tuple[str, str]] = set()
    q: deque[tuple[str, int, list[str]]] = deque([(table, 0, [table])])
    seen_tables = {table}
    while q:
        t, d, path = q.popleft()
        if d + 1 > depth:
            continue
        own = {w["id"] for w in tables.get(t, {}).get("writers", [])}
        for rd in sorted(tables.get(t, {}).get("readers", []), key=lambda e: e["via"] != "routine"):  # a live routine claims its tables before a DAG that calls it
            if (rd["id"], t) in seen_nodes or rd["id"] in own:  # a reader that also writes the table (watermark/self-read) is not downstream of it
                continue
            seen_nodes.add((rd["id"], t))
            out.append({"depth": d + 1, "reader": rd["id"], "project": rd["project"], "reads": t,
                        "path": [*path, f"[{rd['id']}]"]})
            nxt = [rd["target"]] if rd.get("target") else sorted(written_by.get(rd["id"], ()))  # DMS edges are table pairs
            for t2 in nxt:
                if t2 not in seen_tables and t2 not in skip:
                    seen_tables.add(t2)
                    q.append((t2, d + 1, [*path, f"[{rd['id']}]", t2]))
    return out


def callers(edges: dict[str, Any], node_id: str) -> list[str]:
    """Every node that calls `node_id`, directly or through other routines (DAGs, scripts, routines, triggers)."""
    by_to: dict[str, set[str]] = {}
    for c in edges.get("calls", []):
        by_to.setdefault(c["to"], set()).add(c["from"])
    seen: set[str] = set()
    stack = [node_id]
    while stack:
        for f in sorted(by_to.get(stack.pop(), ())):
            if f not in seen:
                seen.add(f)
                stack.append(f)
    return sorted(seen)


def find_routines(edges: dict[str, Any], spec: str) -> list[str]:
    """Routine ids matching `proc:schema.name`, `function:...`, `view:...`, `trigger:...` or a full `proc:<source>:schema.name`."""
    kind, _, rest = spec.partition(":")
    rest = rest.lower()
    out = []
    for rid in sorted(edges.get("routines", {})):
        rk, _, tail = rid.partition(":")
        if rk != kind:
            continue
        if tail == rest or tail.split(":", 1)[-1] == rest or tail.split(":", 1)[-1].endswith("." + rest):
            out.append(rid)
    return out
