"""Airflow scanner (M1c section 2): DAGs, datasets, SQL, util-symbol usage from Python source via `ast` only.

Nothing is imported or executed. Anything dynamic that cannot be resolved from module-level constants is recorded as
dynamic / counted as unresolved and never guessed.
"""
from __future__ import annotations

import ast
import itertools
import re
import warnings
from pathlib import Path
from typing import Any

from de_agent.scanners.common import analyze_sql, rel_to

HOOK_METHODS = {"run", "get_records", "get_first", "get_pandas_df", "execute", "executemany"}
OPERATOR_RE = re.compile(r"(Operator|Sensor)$")
DATASET_NAMES = {"Dataset", "Asset"}
CAP = 500  # max unrolled combinations per expression


class DatasetConst(str):
    """Module-level `X = Dataset("uri")`: a str (the URI) that remembers it is a Dataset reference."""


class _Unknown:
    """Loop variable over something we cannot enumerate."""


UNK = _Unknown()


def _name_of(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _dotted(node: ast.AST) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return parts[::-1]
    return None


def _unparse(node: ast.AST, limit: int = 80) -> str:
    try:
        return ast.unparse(node)[:limit]
    except Exception:  # pragma: no cover
        return "?"


def pattern_of(node: ast.AST) -> str:
    """`export_{x}` style pattern for a non-constant id expression."""
    if isinstance(node, ast.JoinedStr):
        out = ""
        for v in node.values:
            if isinstance(v, ast.Constant):
                out += str(v.value)
            elif isinstance(v, ast.FormattedValue):
                out += "{" + _unparse(v.value, 30) + "}"
        return out
    if isinstance(node, ast.Constant):
        return str(node.value)
    return "{" + _unparse(node, 40) + "}"


class Evaluator:
    """Resolve expressions to string values using module-level constants and loop/parameter scopes."""

    def __init__(self, consts: dict[str, Any]) -> None:
        self.consts = consts

    def iter_values(self, node: ast.AST, scopes: dict[str, Any]) -> list[Any] | None:
        """Values of `for x in <node>` when it is a constant list/tuple/dict-keys/range; else None."""
        val: Any = None
        if isinstance(node, ast.Name):
            if node.id in scopes and isinstance(scopes[node.id], list):
                val = scopes[node.id]
            else:
                val = self.consts.get(node.id)
        elif isinstance(node, (ast.List, ast.Tuple)):
            try:
                val = ast.literal_eval(node)
            except ValueError:
                return None
        elif isinstance(node, ast.Call) and _name_of(node.func) == "range" and node.args:
            args = []
            for a in node.args:
                vs, _ = self.strings(a, scopes)
                if not vs or len(vs) != 1 or not vs[0].lstrip("-").isdigit():
                    return None
                args.append(int(vs[0]))
            r = range(*args)
            return list(r) if len(r) <= CAP else None
        if isinstance(val, dict):
            val = list(val.keys())
        if isinstance(val, (list, tuple)) and len(val) <= CAP and all(isinstance(v, (str, int)) for v in val):
            return list(val)
        return None

    def strings(self, node: ast.AST, scopes: dict[str, Any]) -> tuple[list[str] | None, bool]:
        """(possible values, used_a_loop_or_param) ; values None when dynamic."""
        if isinstance(node, ast.Constant):
            return ([node.value] if isinstance(node.value, str) else [str(node.value)]), False
        if isinstance(node, ast.Name):
            if node.id in scopes:
                v = scopes[node.id]
                if v is UNK or not isinstance(v, list):
                    return None, True
                return [str(x) for x in v], True
            c = self.consts.get(node.id)
            if isinstance(c, (str, int)):
                return [str(c)], False
            return None, False
        if isinstance(node, ast.JoinedStr):
            parts: list[list[str]] = []
            unrolled = False
            for v in node.values:
                if isinstance(v, ast.Constant):
                    parts.append([str(v.value)])
                elif isinstance(v, ast.FormattedValue):
                    vs, u = self.strings(v.value, scopes)
                    if vs is None:
                        return None, u
                    unrolled |= u
                    parts.append(vs)
            return self._product(parts), unrolled
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            a, ua = self.strings(node.left, scopes)
            b, ub = self.strings(node.right, scopes)
            if a is None or b is None:
                return None, ua or ub
            return self._product([a, b]), ua or ub
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            base, ub = self.strings(node.func.value, scopes)
            if base is None:
                return None, ub
            argv = []
            for a in [*node.args, *[k.value for k in node.keywords if k.arg]]:
                vs, u = self.strings(a, scopes)
                if vs is None:
                    return None, u
                ub |= u
                argv.append(vs)
            if len(node.args) + len([k for k in node.keywords if k.arg]) != len(node.args) + len(node.keywords):
                return None, ub
            out = []
            kwn = [k.arg for k in node.keywords if k.arg]
            for combo in itertools.islice(itertools.product(*argv), CAP):
                pos, kw = combo[:len(node.args)], dict(zip(kwn, combo[len(node.args):]))
                for b in base:
                    try:
                        out.append(b.format(*pos, **kw))
                    except (IndexError, KeyError, ValueError):
                        return None, ub
            return out, ub
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            base = self.consts.get(node.value.id)
            ks, _ = self.strings(node.slice, scopes)
            if isinstance(base, dict) and ks and len(ks) == 1 and ks[0] in {str(k) for k in base}:
                item = next(v for k, v in base.items() if str(k) == ks[0])
                if isinstance(item, (str, int)):
                    return [str(item)], False
        return None, False

    @staticmethod
    def _product(parts: list[list[str]]) -> list[str] | None:
        n = 1
        for p in parts:
            n *= max(len(p), 1)
        if n > CAP:
            return None
        return ["".join(c) for c in itertools.product(*parts)]


def module_consts(tree: ast.Module) -> dict[str, Any]:
    """Module-level NAME = <literal> assigned exactly once (then simple f-string/concat of earlier constants)."""
    counts: dict[str, int] = {}
    for node in tree.body:
        for tgt in _targets(node):
            counts[tgt] = counts.get(tgt, 0) + 1
    consts: dict[str, Any] = {}
    ev = Evaluator(consts)
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            name, value = node.target.id, node.value
        else:
            continue
        if counts.get(name) != 1:
            continue
        try:
            consts[name] = ast.literal_eval(value)
            continue
        except (ValueError, SyntaxError):
            pass
        if isinstance(value, (ast.JoinedStr, ast.BinOp)):
            vs, _ = ev.strings(value, {})
            if vs and len(vs) == 1:
                consts[name] = vs[0]
        elif isinstance(value, ast.Call) and _name_of(value.func) in DATASET_NAMES:
            kw = {k.arg: k.value for k in value.keywords if k.arg}
            arg = kw.get("uri") or (value.args[0] if value.args else None) or kw.get("name")
            vs, _ = ev.strings(arg, {}) if arg is not None else (None, False)
            if vs and len(vs) == 1:
                consts[name] = DatasetConst(vs[0])
    return consts


def _targets(node: ast.stmt) -> list[str]:
    out: list[str] = []
    if isinstance(node, ast.Assign):
        for t in node.targets:
            out += [n.id for n in ast.walk(t) if isinstance(n, ast.Name)]
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
        out.append(node.target.id)
    elif isinstance(node, (ast.For, ast.With, ast.If, ast.Try)):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign):
                out += [n.id for t in sub.targets for n in ast.walk(t) if isinstance(n, ast.Name)]
    return out


class Instance:
    """One discovered DAG construction (before expansion over factory call sites / loops)."""

    def __init__(self, call: ast.Call | None, body: list[ast.stmt] | None, container: ast.AST | None,
                 loop_scopes: dict[str, Any], taskflow: ast.FunctionDef | None = None, var: str | None = None) -> None:
        self.call, self.body, self.container = call, body, container
        self.loop_scopes, self.taskflow, self.var = loop_scopes, taskflow, var


class FileScanner:
    def __init__(self, src: str, rel: str, dialect: str) -> None:
        self.rel, self.dialect = rel, dialect
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # scanned code is parsed, never run: its SyntaxWarnings are not ours
            self.tree = ast.parse(src)
        self.consts = module_consts(self.tree)
        self.ev = Evaluator(self.consts)
        self.aliases: dict[str, str] = {}  # local name -> qualified dotted name
        self.star_imports = 0
        self.dynamic_calls = 0
        self.funcs: dict[str, ast.FunctionDef] = {}
        self._collect_imports_and_funcs()
        self.fsql: dict[str, list[dict[str, Any]]] = {}
        self.stats = {"unresolved_sql": 0, "unresolved_datasets": 0, "unparsed_sql": 0}

    # ---------------- imports, symbols ----------------
    def _collect_imports_and_funcs(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.aliases[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for a in node.names:
                    if a.name == "*":
                        self.star_imports += 1
                    else:
                        self.aliases[a.asname or a.name] = f"{node.module}.{a.name}"
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs.setdefault(node.name, node)  # type: ignore[arg-type]

    def uses(self) -> list[str]:
        out: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Attribute):
                d = _dotted(node)
                if d and d[0] in self.aliases:
                    out.add(".".join([self.aliases[d[0]], *d[1:]]))
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in self.aliases:
                out.add(self.aliases[node.id])
            elif isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, (ast.Call, ast.Subscript)) or (
                        isinstance(f, ast.Name) and f.id in ("getattr", "__import__")) or (
                        isinstance(f, ast.Attribute) and f.attr == "import_module"):
                    self.dynamic_calls += 1
        return sorted(out)

    def defs(self) -> list[dict[str, Any]]:
        out = []
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.name.startswith("_"):
                doc = (ast.get_docstring(node) or "").strip().splitlines()
                out.append({"name": node.name, "line": node.lineno,
                            "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                            "doc": doc[0].strip()[:140] if doc else ""})
        return out

    # ---------------- DAG discovery ----------------
    def discover(self) -> list[Instance]:
        found: list[Instance] = []

        def is_dag_call(n: ast.AST) -> bool:
            return isinstance(n, ast.Call) and _name_of(n.func) == "DAG"

        def rec(node: ast.AST, container: ast.AST | None, scopes: dict[str, Any]) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(_name_of(d.func if isinstance(d, ast.Call) else d) == "dag" for d in node.decorator_list):
                    dcall = next((d for d in node.decorator_list
                                  if isinstance(d, ast.Call) and _name_of(d.func) == "dag"), None)
                    found.append(Instance(dcall, node.body, container, dict(scopes), taskflow=node))  # type: ignore[arg-type]
                    return
                container, scopes = node, {}
            if isinstance(node, ast.With):
                for item in node.items:
                    if is_dag_call(item.context_expr):
                        found.append(Instance(item.context_expr, node.body, container, dict(scopes)))  # type: ignore[arg-type]
            elif isinstance(node, ast.Assign) and is_dag_call(node.value):
                tgt = node.targets[0].id if isinstance(node.targets[0], ast.Name) else None
                found.append(Instance(node.value, None, container, dict(scopes), var=tgt))  # type: ignore[arg-type]
            elif isinstance(node, ast.Return) and node.value is not None and is_dag_call(node.value):
                found.append(Instance(node.value, None, container, dict(scopes)))  # type: ignore[arg-type]
            if isinstance(node, ast.For):
                it = self.ev.iter_values(node.iter, scopes)
                names = [n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)]
                inner = dict(scopes)
                for n in names:
                    inner[n] = it if (it is not None and isinstance(node.target, ast.Name)) else UNK
                for ch in node.body:
                    rec(ch, container, inner)
                for ch in node.orelse:
                    rec(ch, container, scopes)
                return
            for ch in ast.iter_child_nodes(node):
                rec(ch, container, scopes)

        rec(self.tree, None, {})
        return found

    def call_sites(self, fn: ast.FunctionDef) -> list[tuple[ast.Call, dict[str, Any]]]:
        sites: list[tuple[ast.Call, dict[str, Any]]] = []

        def rec(node: ast.AST, scopes: dict[str, Any], inside_fn: bool) -> None:
            if node is fn:
                return
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scopes, inside_fn = {}, True
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == fn.name:
                sites.append((node, dict(scopes)))
            if isinstance(node, ast.For):
                it = self.ev.iter_values(node.iter, scopes)
                inner = dict(scopes)
                for n in (x.id for x in ast.walk(node.target) if isinstance(x, ast.Name)):
                    inner[n] = it if (it is not None and isinstance(node.target, ast.Name)) else UNK
                for ch in node.body:
                    rec(ch, inner, inside_fn)
                for ch in node.orelse + [node.iter]:
                    rec(ch, scopes, inside_fn)
                return
            for ch in ast.iter_child_nodes(node):
                rec(ch, scopes, inside_fn)

        rec(self.tree, {}, False)
        return sites

    # ---------------- per-DAG analysis ----------------
    def scan(self) -> dict[str, Any]:
        instances = self.discover()
        # SQL inside module-level helper functions that are not themselves DAG containers
        container_fns = {id(i.container) for i in instances if i.container is not None} | {
            id(i.taskflow) for i in instances if i.taskflow is not None}
        for name, fn in self.funcs.items():
            if id(fn) in container_fns:
                continue
            rec = self._new_acc()
            self._visit_body(fn.body, {}, rec, None)
            self.fsql[name] = rec["sql"]
        dags: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for inst in instances:
            for d in self._expand(inst):
                if d["dag_id"] in seen_ids:
                    continue
                seen_ids.add(d["dag_id"])
                dags.append(d)
        # attach helper-function SQL: referenced from a DAG body, else (single-DAG file) to that DAG
        referenced: set[str] = set()
        for d, names in ((d, d.pop("_names")) for d in dags):
            for n in sorted(names & set(self.fsql)):
                referenced.add(n)
                self._merge_sql(d, self.fsql[n])
        loose = [s for n, v in self.fsql.items() if n not in referenced for s in v]
        unattributed = 0
        if loose:
            if len(dags) == 1:
                self._merge_sql(dags[0], loose)
            else:
                unattributed = len(loose)
        for d in dags:
            self._finish_dag(d)
        return {"dags": dags, "uses": self.uses(), "star_imports": self.star_imports,
                "dynamic_calls": self.dynamic_calls, "defs": self.defs(),
                "stats": {**self.stats, "unattributed_sql": unattributed}}

    def _new_acc(self) -> dict[str, Any]:
        return {"sql": [], "operators": set(), "outlets": [], "inlets": [], "sensors": set(), "triggers": set(),
                "pools": set(), "names": set(), "searchpaths": set(), "has_task": False}

    def _expand(self, inst: Instance) -> list[dict[str, Any]]:
        """One or more DAG entries for a discovered construction (factory call sites, loops)."""
        fn = inst.container
        base_scopes = dict(inst.loop_scopes)
        bindings: list[dict[str, Any]] = []
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sites = self.call_sites(fn)  # type: ignore[arg-type]
            params = [a.arg for a in fn.args.args]
            if not sites:
                bindings = [{p: UNK for p in params}]
            for site, site_scopes in sites:
                b: dict[str, Any] = {}
                for p, a in zip(params, site.args):
                    vs, _ = self.ev.strings(a, site_scopes)
                    b[p] = vs if vs is not None else UNK
                for kw in site.keywords:
                    if kw.arg in params:
                        vs, _ = self.ev.strings(kw.value, site_scopes)
                        b[kw.arg] = vs if vs is not None else UNK
                for p, dflt in zip(reversed(params), reversed(fn.args.defaults)):
                    if p not in b:
                        vs, _ = self.ev.strings(dflt, {})
                        b[p] = vs if vs is not None else UNK
                bindings.append(b)
            base_scopes = {}
        else:
            bindings = [{}]
        out: list[dict[str, Any]] = []
        for b in bindings:
            scopes = {**base_scopes, **b}
            names = [k for k, v in scopes.items() if isinstance(v, list)]
            for combo in itertools.islice(itertools.product(*[scopes[n] for n in names]), CAP):
                conc = {**scopes, **{n: [v] for n, v in zip(names, combo)}}
                out.append(self._build(inst, conc, bool(names) or bool(b)))
        return out

    def _dag_id(self, inst: Instance, scopes: dict[str, Any]) -> tuple[str, bool, str | None]:
        node: ast.AST | None = None
        if inst.call is not None:
            node = inst.call.args[0] if inst.call.args else next(
                (k.value for k in inst.call.keywords if k.arg == "dag_id"), None)
        if node is None:
            if inst.taskflow is not None:
                return inst.taskflow.name, False, None
            return "?", True, "?"
        vs, unrolled = self.ev.strings(node, scopes)
        pat = pattern_of(node)
        if vs and len(vs) == 1:
            return vs[0], bool(unrolled), (pat if unrolled else None)
        return pat, True, pat

    def _build(self, inst: Instance, scopes: dict[str, Any], from_params: bool) -> dict[str, Any]:
        dag_id, dynamic, pattern = self._dag_id(inst, scopes)
        node = inst.taskflow or inst.call
        line = (inst.taskflow.lineno if inst.taskflow else inst.call.lineno) if node is not None else 0  # type: ignore[union-attr]
        acc = self._new_acc()
        d: dict[str, Any] = {"dag_id": dag_id, "dynamic": dynamic, "pattern": pattern if dynamic else None,
                             "file": self.rel, "line": line, "schedule": "", "tags": [], "owner": ""}
        # DAG-level kwargs (schedule, tags, default_args, template_searchpath)
        kws = {k.arg: k.value for k in (inst.call.keywords if inst.call is not None else []) if k.arg}
        sched = kws.get("schedule") or kws.get("schedule_interval") or kws.get("timetable")
        if sched is not None:
            self._datasets(sched, scopes, acc, "inlets", line)
            vs, _ = self.ev.strings(sched, scopes)
            d["schedule"] = vs[0] if vs and len(vs) == 1 and isinstance(sched, ast.Constant) else (
                "datasets" if acc["inlets"] and not isinstance(sched, ast.Constant) else _unparse(sched, 60))
        if "tags" in kws:
            try:
                d["tags"] = sorted(str(t) for t in ast.literal_eval(kws["tags"]))
            except (ValueError, SyntaxError):
                tv = self.ev.iter_values(kws["tags"], scopes)
                d["tags"] = sorted(str(t) for t in tv) if tv else []
        da = kws.get("default_args")
        if da is not None:
            if isinstance(da, ast.Dict):
                for k, v in zip(da.keys, da.values):
                    if isinstance(k, ast.Constant) and k.value == "owner":
                        ov, _ = self.ev.strings(v, scopes)
                        d["owner"] = ov[0] if ov and len(ov) == 1 else ""
            elif isinstance(da, ast.Name):
                dd = self.consts.get(da.id)
                if isinstance(dd, dict):
                    d["owner"] = str(dd.get("owner", ""))
        if "template_searchpath" in kws:
            vs = self.ev.iter_values(kws["template_searchpath"], scopes)
            if vs is None:
                one, _ = self.ev.strings(kws["template_searchpath"], scopes)
                vs = one or []
            acc["searchpaths"].update(str(v) for v in vs)
        body: list[ast.AST]
        if inst.body is not None:
            body = list(inst.body)
        elif isinstance(inst.container, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = list(inst.container.body)
        else:
            body = list(self.tree.body)
        self._visit_body(body, scopes, acc, inst.var)
        for n in body:
            for sub in ast.walk(n):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                    acc["names"].add(sub.id)
        d.update({"operators": sorted(acc["operators"]), "outlets": acc["outlets"], "inlets": acc["inlets"],
                  "sensors": sorted(acc["sensors"]), "triggers": sorted(acc["triggers"]),
                  "pools": sorted(acc["pools"]), "sql": acc["sql"], "searchpaths": sorted(acc["searchpaths"]),
                  "_names": acc["names"]})
        return d

    @staticmethod
    def _merge_sql(d: dict[str, Any], recs: list[dict[str, Any]]) -> None:
        have = {(r["line"], r.get("text", r.get("file", ""))) for r in d["sql"]}
        for r in recs:
            key = (r["line"], r.get("text", r.get("file", "")))
            if key not in have:
                d["sql"].append(r)
                have.add(key)

    def _finish_dag(self, d: dict[str, Any]) -> None:
        reads: set[str] = set()
        writes: set[str] = set()
        calls: set[str] = set()
        refs: list[dict[str, Any]] = []
        for r in sorted(d["sql"], key=lambda r: (r["line"], r.get("text", ""))):
            if r.get("file"):
                refs.append(r)
                continue
            reads.update(r["reads"])
            writes.update(r["writes"])
            calls.update(r["calls"])
            if r["unparsed"]:
                self.stats["unparsed_sql"] += 1
        d["sql_files"] = [{"path": r["file"], "line": r["line"]} for r in refs]
        d["sql"] = [r for r in d["sql"] if not r.get("file")]
        d["reads"], d["writes"], d["calls"] = sorted(reads), sorted(writes), sorted(calls)

    # ---------------- body visiting ----------------
    def _visit_body(self, body: list[ast.AST], scopes: dict[str, Any], acc: dict[str, Any], var: str | None) -> None:
        for n in body:
            self._visit(n, scopes, acc, var)

    def _visit(self, node: ast.AST, scopes: dict[str, Any], acc: dict[str, Any], var: str | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                tgt = dec.func if isinstance(dec, ast.Call) else dec
                if (_dotted(tgt) or [""])[0] == "task" or _name_of(tgt) == "task":
                    acc["operators"].add("@task")
                if isinstance(dec, ast.Call):
                    self._call(dec, scopes, acc, var)
            self._visit_body(node.body, scopes, acc, var)
            return
        if isinstance(node, ast.For):
            it = self.ev.iter_values(node.iter, scopes)
            inner = dict(scopes)
            for n in (x.id for x in ast.walk(node.target) if isinstance(x, ast.Name)):
                inner[n] = it if (it is not None and isinstance(node.target, ast.Name)) else UNK
            self._visit(node.iter, scopes, acc, var)
            for ch in node.body:
                self._visit(ch, inner, acc, var)
            for ch in node.orelse:
                self._visit(ch, scopes, acc, var)
            return
        if isinstance(node, ast.Call):
            self._call(node, scopes, acc, var)
        for ch in ast.iter_child_nodes(node):
            self._visit(ch, scopes, acc, var)

    def _call(self, call: ast.Call, scopes: dict[str, Any], acc: dict[str, Any], var: str | None) -> None:
        name = _name_of(call.func)
        if name == "DAG":
            return
        kws = {k.arg: k.value for k in call.keywords if k.arg}
        if var is not None:  # `dag = DAG(...)` style: only calls bound to this DAG count as its tasks
            bound = kws.get("dag")
            if (OPERATOR_RE.search(name) or "task_id" in kws) and not (isinstance(bound, ast.Name) and bound.id == var):
                return
        if OPERATOR_RE.search(name):
            acc["operators"].add(name)
        line = call.lineno
        if "outlets" in kws:
            self._datasets(kws["outlets"], scopes, acc, "outlets", line)
        if "inlets" in kws:
            self._datasets(kws["inlets"], scopes, acc, "inlets", line)
        for key, bucket in (("external_dag_id", "sensors"), ("trigger_dag_id", "triggers"), ("pool", "pools")):
            if key in kws:
                vs, _ = self.ev.strings(kws[key], scopes)
                if vs:
                    acc[bucket].update(vs)
        if "template_searchpath" in kws:
            vs, _ = self.ev.strings(kws["template_searchpath"], scopes)
            acc["searchpaths"].update(vs or [])
        sql_nodes: list[ast.AST] = []
        if "sql" in kws:
            sql_nodes.append(kws["sql"])
        if isinstance(call.func, ast.Attribute) and call.func.attr in HOOK_METHODS and call.args:
            sql_nodes.append(call.args[0])
        for sn in sql_nodes:
            for leaf in (sn.elts if isinstance(sn, (ast.List, ast.Tuple)) else [sn]):
                self._sql(leaf, scopes, acc, line, explicit="sql" in kws and leaf is kws["sql"])

    def _datasets(self, node: ast.AST, scopes: dict[str, Any], acc: dict[str, Any], bucket: str, line: int) -> None:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(self.consts.get(sub.id), DatasetConst):
                acc[bucket].append({"uri": str(self.consts[sub.id]), "unrolled": False, "line": line})
            elif isinstance(sub, ast.Call) and _name_of(sub.func) in DATASET_NAMES:
                kw = {k.arg: k.value for k in sub.keywords if k.arg}
                arg = kw.get("uri") or (sub.args[0] if sub.args else None) or kw.get("name")
                vs, unrolled = self.ev.strings(arg, scopes) if arg is not None else (None, False)
                if not vs:
                    self.stats["unresolved_datasets"] += 1
                    acc[bucket].append({"uri": None, "text": _unparse(sub, 60), "line": line})
                    continue
                for v in vs:
                    acc[bucket].append({"uri": v, "unrolled": bool(unrolled), "line": line})

    def _sql(self, node: ast.AST, scopes: dict[str, Any], acc: dict[str, Any], line: int, explicit: bool) -> None:
        vs, unrolled = self.ev.strings(node, scopes)
        if vs is None:
            stringish = isinstance(node, (ast.JoinedStr, ast.BinOp)) or (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format")
            if stringish or (explicit and isinstance(node, ast.Name)):
                self.stats["unresolved_sql"] += 1
            return
        for v in vs:
            text = v.strip()
            if re.fullmatch(r"[\w./{}-]+\.sql", text):
                acc["sql"].append({"file": text, "line": line})
                continue
            if not re.match(r"(?is)\s*(with|select|insert|update|delete|merge|create|call|truncate|copy|drop|alter|vacuum|begin|\()", text):
                continue
            res = analyze_sql(text, self.dialect)
            res.update({"line": line, "unrolled": bool(unrolled), "text": " ".join(text.split())[:120]})
            acc["sql"].append(res)


def scan_python(src: str, rel: str, dialect: str) -> dict[str, Any]:
    try:
        fs = FileScanner(src, rel, dialect)
    except (SyntaxError, ValueError) as ex:
        return {"rel": rel, "error": f"{type(ex).__name__}: {str(ex)[:80]}", "dags": [], "uses": [], "star_imports": 0,
                "dynamic_calls": 0, "defs": [], "stats": {}}
    res = fs.scan()
    res["rel"] = rel
    return res


# ---------------- assembly ----------------

def _folder(rel: str, dag_folders: list[str]) -> str:
    for f in sorted(dag_folders, key=len, reverse=True):
        f = f.strip("/")
        if rel == f or rel.startswith(f + "/"):
            inner = rel[len(f):].lstrip("/")
            return inner.split("/")[0] if "/" in inner else "."
    return "."


def dataset_matcher(patterns: list[str]):  # noqa: ANN201
    rxs = []
    for p in patterns:
        rx = re.escape(p)
        for ph in ("db", "schema", "table"):
            rx = rx.replace(re.escape("{" + ph + "}"), f"(?P<{ph}>[^/?#]+)")
        rxs.append(re.compile(rx + r"\Z"))

    def match(uri: str) -> str | None:
        for rx in rxs:
            m = rx.match(uri)
            if m:
                g = m.groupdict()
                return f"{g['schema']}.{g['table']}".lower() if g.get("schema") else g["table"].lower()
        return None

    return match


def util_module_name(root: Path, entry: str, file: Path) -> str:
    base = (root / entry).parent
    mod = file.resolve().relative_to(base.resolve()).with_suffix("").as_posix().replace("/", ".")
    return mod.removesuffix(".__init__")


def assemble(scan: Any, root: Path, per_file: dict[str, dict[str, Any]], util_files: dict[str, str],
             dag_files: set[str], revision: str) -> dict[str, Any]:
    """Join per-file results: resolve datasets, .sql refs and util symbol usage."""
    match = dataset_matcher(scan.dataset_tables)
    sql_cache: dict[str, dict[str, Any] | None] = {}

    def read_sql(rel_ref: str, dag_file: str, searchpaths: list[str]) -> dict[str, Any] | None:
        dag_dir = (root / dag_file).parent
        cands = [dag_dir / rel_ref, root / rel_ref]
        cands += [root / f / rel_ref for f in scan.dag_folders]
        for sp in searchpaths:
            p = Path(sp)
            cands.append(p / rel_ref if p.is_absolute() else dag_dir / sp / rel_ref)
        for c in cands:
            key = c.as_posix()
            if key not in sql_cache:
                try:
                    txt = c.read_text(encoding="utf-8") if c.is_file() else None
                except OSError:
                    txt = None
                sql_cache[key] = analyze_sql(txt, scan.dialect) if txt is not None else None
            if sql_cache[key] is not None:
                return sql_cache[key]
        return None

    symbols: dict[str, dict[str, Any]] = {}
    for rel, modname in sorted(util_files.items()):
        for d in per_file.get(rel, {}).get("defs", []):
            symbols[f"{modname}.{d['name']}"] = {"file": rel, "line": d["line"], "kind": d["kind"], "doc": d["doc"],
                                                 "dags": [], "files": []}
    dags: list[dict[str, Any]] = []
    stats = {"dag_files": len(dag_files), "dags": 0, "dynamic_dags": 0, "unresolved_calls": 0,
             "unresolved_sql": 0, "unresolved_datasets": 0, "unparsed_sql": 0, "unattributed_sql": 0,
             "unresolved_sql_files": 0, "parse_errors": 0}
    for rel in sorted(dag_files):
        pf = per_file.get(rel)
        if not pf:
            continue
        if pf.get("error"):
            stats["parse_errors"] += 1
        stats["unresolved_calls"] += pf["star_imports"] + pf["dynamic_calls"]
        for k in ("unresolved_sql", "unresolved_datasets", "unparsed_sql", "unattributed_sql"):
            stats[k] += pf["stats"].get(k, 0)
        used = set()
        for u in pf["uses"]:
            parts = u.split(".")
            for k in range(len(parts), 1, -1):
                cand = ".".join(parts[:k])
                if cand in symbols:
                    used.add(cand)
                    break
        for sym in used:
            symbols[sym]["files"].append(rel)
        for d in pf["dags"]:
            d = dict(d)
            d["folder"] = _folder(rel, scan.dag_folders)
            reads, writes, calls = set(d["reads"]), set(d["writes"]), set(d["calls"])
            searchpaths = d.pop("searchpaths", [])
            for ref in d.pop("sql_files", []):
                res = read_sql(ref["path"], rel, searchpaths)
                if res is None:
                    stats["unresolved_sql_files"] += 1
                    continue
                reads.update(res["reads"])
                writes.update(res["writes"])
                calls.update(res["calls"])
                if res["unparsed"]:
                    stats["unparsed_sql"] += 1
            d["dataset_writes"], d["dataset_reads"] = [], []
            for bucket, target in (("outlets", "dataset_writes"), ("inlets", "dataset_reads")):
                for ds in d[bucket]:
                    if ds.get("uri"):
                        t = match(ds["uri"])
                        if t:
                            d[target].append({"table": t, "uri": ds["uri"], "unrolled": ds.get("unrolled", False)})
            d["outlets"] = sorted({o["uri"] for o in d["outlets"] if o.get("uri")})
            d["inlets"] = sorted({o["uri"] for o in d["inlets"] if o.get("uri")})
            d["reads"], d["writes"], d["calls"] = sorted(reads), sorted(writes), sorted(calls)
            d["sql"] = [{k: v for k, v in r.items() if k != "text"} for r in d["sql"]]
            dags.append(d)
            stats["dags"] += 1
            stats["dynamic_dags"] += 1 if d["dynamic"] else 0
        dag_ids_in_file = sorted({d["dag_id"] for d in pf["dags"]})
        for sym in used:
            symbols[sym]["dags"].extend({"dag_id": i, "file": rel} for i in dag_ids_in_file)
    dags.sort(key=lambda d: (d["folder"], d["dag_id"], d["file"], d["line"]))
    for s in symbols.values():
        s["files"] = sorted(set(s["files"]))
        s["dags"] = sorted({(x["dag_id"], x["file"]) for x in s["dags"]})
        s["dags"] = [{"dag_id": a, "file": b} for a, b in s["dags"]]
    return {"scan": scan.name, "type": "airflow", "project": scan.project_name, "revision": revision,
            "dags": dags, "symbols": dict(sorted(symbols.items())), "stats": stats}


def scan_files(scan: Any, root: Path) -> tuple[dict[str, Path], dict[str, str], set[str]]:
    """(rel -> path of every .py to parse, rel -> module name for util files, rels that are DAG files)."""
    files: dict[str, Path] = {}
    dag_rels: dict[str, Path] = {}
    util: dict[str, str] = {}
    for f in scan.dag_folders:
        base = root / f
        for p in sorted(base.rglob("*.py")) if base.is_dir() else []:
            if "__pycache__" in p.parts:
                continue
            rel = rel_to(root, p)
            files[rel] = p
            dag_rels[rel] = p
    for entry in scan.util_packages:
        base = root / entry
        for p in sorted(base.rglob("*.py")) if base.is_dir() else []:
            if "__pycache__" in p.parts:
                continue
            rel = rel_to(root, p)
            files[rel] = p
            util[rel] = util_module_name(root, entry, p)
    return files, util, set(dag_rels)
