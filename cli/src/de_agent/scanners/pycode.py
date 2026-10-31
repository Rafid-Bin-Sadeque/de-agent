"""Python code scanner core (v0.2 section 2): per-function SQL, table and call records, plus util attribution.

`CodeScanner` reads ONE file with `ast` (never imports or runs it) and records, for every function and for the module body:
  - SQL strings given to execute/read_sql/text()/spark.sql and friends, analysed with sqlglot;
  - table references from pandas (`read_sql_table`, `to_sql`) and pyspark (`table`, `saveAsTable`, `insertInto`,
    `DeltaTable.forName`, `option("dbtable", ..)`, `jdbc`) and path nodes (`format(..).load(path)`, `.save(path)`, `parquet(path)`);
  - calls to other functions (local, or imported through an alias) with their argument values.
A string that depends on a function parameter is kept as a TEMPLATE (parameters are \\x01name\\x02 sentinels), so a caller that
passes a literal (`run_proc("etl.sp_x")`) can instantiate it. `UtilGraph` resolves those calls across files, up to MAX_HOPS deep.
"""
from __future__ import annotations

import ast
import itertools
import re
import warnings
from typing import Any

from de_agent.scanners.common import analyze_sql, heuristic_sql
from de_agent.scanners.pyexpr import CAP, UNK, Evaluator, _dotted, _name_of, bind_row, module_consts

MAX_HOPS = 2
MAX_RECS = 500
SENT_RE = re.compile(r"\x01(\w+)\x02")
SQL_START_RE = re.compile(
    r"(?is)\s*(with|select|insert|update|delete|merge|create|call|truncate|copy|drop|alter|vacuum|begin|\()")
EXEC_METHODS = {"execute", "executemany", "executescript", "exec_driver_sql", "execute_batch", "execute_values", "run",
                "get_records", "get_first", "get_pandas_df", "read_sql", "read_sql_query", "read_sql_query_chunks"}
READ_FILE_METHODS = {"load", "parquet", "csv", "json", "orc", "avro", "text", "save"}
WRITE_TABLE_METHODS = {"saveAsTable", "insertInto", "save_as_table"}
SQL_KWARGS = {"sql": (False, False), "query": (False, False), "Sql": (False, True), "QueryString": (False, True),
              "Sqls": (True, True)}  # keyword -> (a list of statements, an unreadable value is counted unresolved)
DELTA_WRITE_METHODS = {"merge", "update", "delete"}
PATH_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|/|dbfs:)", re.I)
SKIP_SEGMENTS = {"__pycache__", ".git", ".venv", "venv", "node_modules", "site-packages", ".tox", ".ipynb_checkpoints",
                 "build", "dist", ".mypy_cache", ".pytest_cache", ".ruff_cache"}


def sent(p: str) -> str:
    return f"\x01{p}\x02"


def norm_name(s: str) -> str:
    """Table name as the edges graph keys it: lowercase, quotes removed, `catalog.schema.table` cut to `schema.table`."""
    s = s.strip().replace('"', "").replace("`", "").replace("[", "").replace("]", "").lower()
    parts = [p for p in s.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) > 2 else ".".join(parts)


def path_node(p: str) -> str:
    """Path node key: lowercase, `s3a://`/`s3n://` folded to `s3://`, trailing slash on directory-like paths."""
    p = p.strip().strip("'\"").lower()
    p = re.sub(r"^s3[an]://", "s3://", p)
    last = p.rstrip("/").rsplit("/", 1)[-1]
    if "*" in p or "." in last:  # a glob or a file: keep as written
        return p
    return p.rstrip("/") + "/"


def is_path(s: str) -> bool:
    return bool(PATH_RE.match(s))


def node_key(s: str) -> str:
    return path_node(s) if is_path(s) else norm_name(s)


def module_names(rel: str) -> tuple[list[str], bool]:
    """Dotted module names a file can be imported as (every path suffix), and whether it is a package `__init__`."""
    parts = rel.removesuffix(".py").split("/")
    is_pkg = parts[-1] == "__init__"
    if is_pkg:
        parts = parts[:-1]
    return [".".join(parts[i:]) for i in range(len(parts))] if parts else [], is_pkg


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _chain(call_func: ast.AST) -> tuple[list[str], str]:
    """Attribute names down a call chain (`df.write.mode(..).save` -> [save, mode, write]) and the root name."""
    attrs: list[str] = []
    node = call_func
    while True:
        if isinstance(node, ast.Attribute):
            attrs.append(node.attr)
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Subscript):
            node = node.value
        else:
            break
    return attrs, (node.id if isinstance(node, ast.Name) else "")


def _direction(attrs: list[str]) -> str:
    if "write" in attrs or "writeStream" in attrs:
        return "w"
    if "read" in attrs or "readStream" in attrs:
        return "r"
    return ""


def alias_map(tree: ast.Module) -> dict[str, str]:
    """local name -> dotted import target; relative imports keep their leading dots (`.db.run_proc`)."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            for a in node.names:
                if a.name != "*":
                    out[a.asname or a.name] = base + ("." if node.module else "") + a.name
    return out


class Frame:
    """The function being walked: records, parameters, names derived from loops."""

    def __init__(self, key: str, line: int, params: list[str], defaults: dict[str, list[str]]) -> None:
        self.key, self.line, self.params, self.defaults = key, line, params, defaults
        self.sql: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.loopnames: set[str] = set()
        self.delta: dict[str, list[str]] = {}
        self._seen: set[str] = set()

    def add_sql(self, rec: dict[str, Any]) -> None:
        k = repr(sorted(rec.items(), key=lambda kv: kv[0]))
        if k not in self._seen and len(self.sql) < MAX_RECS:
            self._seen.add(k)
            self.sql.append(rec)

    def add_call(self, rec: dict[str, Any]) -> None:
        k = "c" + repr(sorted(rec.items(), key=lambda kv: kv[0]))
        if k not in self._seen and len(self.calls) < MAX_RECS:
            self._seen.add(k)
            self.calls.append(rec)


class CodeScanner:
    def __init__(self, tree: ast.Module, rel: str, dialect: str) -> None:
        self.tree, self.rel, self.dialect = tree, rel, dialect
        self.consts = module_consts(tree)
        self.ev = Evaluator(self.consts)
        self.aliases = alias_map(tree)
        self.local_funcs = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.local_classes = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
        self.frames: dict[str, Frame] = {}
        self.f: Frame = Frame("<module>", 1, [], {})
        self.cls: str | None = None
        self.stats = {"unresolved_sql": 0, "unparsed_sql": 0}

    # ---------------- entry ----------------
    def scan(self) -> dict[str, Any]:
        mod = Frame("<module>", 1, [], {})
        self.frames["<module>"] = mod
        self.f = mod
        self.block(self.tree.body, {})
        funcs = {}
        for key, fr in self.frames.items():
            if fr.sql or fr.calls or key == "<module>":
                funcs[key] = {"line": fr.line, "params": fr.params, "defaults": fr.defaults, "sql": fr.sql, "calls": fr.calls}
        return {"funcs": funcs, "imports": self.aliases, "stats": dict(self.stats)}

    # ---------------- statements ----------------
    def block(self, stmts: list[ast.stmt], scopes: dict[str, Any]) -> None:
        for s in stmts:
            self.stmt(s, scopes)

    def def_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        key = f"{self.cls}.{node.name}" if self.cls else node.name
        args = [a.arg for a in [*node.args.posonlyargs, *node.args.args]]
        static = any(_name_of(d) == "staticmethod" for d in node.decorator_list)
        if self.cls and args and not static:
            args = args[1:]  # self / cls
        kwonly = [a.arg for a in node.args.kwonlyargs]
        params = args + kwonly
        defaults: dict[str, list[str]] = {}
        pos = [a.arg for a in [*node.args.posonlyargs, *node.args.args]]
        for p, d in zip(reversed(pos), reversed(node.args.defaults)):
            vs, _ = self.ev.strings(d, {})
            if vs is not None:
                defaults[p] = vs
        for p, d in zip(kwonly, node.args.kw_defaults):
            if d is not None:
                vs, _ = self.ev.strings(d, {})
                if vs is not None:
                    defaults[p] = vs
        fr = Frame(key, node.lineno, params, defaults)
        self.frames[key] = fr
        prev, self.f = self.f, fr
        scopes = {p: [sent(p)] for p in params}
        self.block(node.body, scopes)
        self.f = prev

    def stmt(self, s: ast.stmt, scopes: dict[str, Any]) -> None:
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if self.f.key == "<module>":
                self.def_function(s)
            else:  # a nested function is part of its parent's behaviour; its own parameters are unknown
                inner = dict(scopes)
                for a in [*s.args.posonlyargs, *s.args.args, *s.args.kwonlyargs]:
                    inner[a.arg] = UNK
                self.block(s.body, inner)
            return
        if isinstance(s, ast.ClassDef):
            prev, self.cls = self.cls, s.name
            for b in s.body:
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)) and self.f.key == "<module>":
                    self.def_function(b)
                else:
                    self.stmt(b, scopes)
            self.cls = prev
            return
        if isinstance(s, ast.Assign):
            self.expr(s.value, scopes)
            for t in s.targets:
                self.bind_target(t, s.value, scopes)
            self._delta_assign(s.targets, s.value, scopes)
            return
        if isinstance(s, ast.AnnAssign):
            if s.value is not None:
                self.expr(s.value, scopes)
                self.bind_target(s.target, s.value, scopes)
            return
        if isinstance(s, ast.AugAssign):
            self.expr(s.value, scopes)
            if isinstance(s.target, ast.Name):
                if isinstance(s.op, ast.Add) and s.target.id in scopes:
                    vs, _ = self.ev.strings(ast.BinOp(left=ast.Name(id=s.target.id, ctx=ast.Load()), op=ast.Add(), right=s.value), scopes)
                    scopes[s.target.id] = vs if vs is not None else UNK
                else:
                    scopes[s.target.id] = UNK
            return
        if isinstance(s, ast.If):
            self.expr(s.test, scopes)
            a, b = dict(scopes), dict(scopes)
            self.block(s.body, a)
            self.block(s.orelse, b)
            self._merge(scopes, a, b)
            return
        if isinstance(s, (ast.For, ast.AsyncFor)):
            self.loop(s, scopes)
            return
        if isinstance(s, ast.While):
            self.expr(s.test, scopes)
            a = dict(scopes)
            self.block(s.body, a)
            self.block(s.orelse, dict(scopes))
            self._merge(scopes, a, dict(scopes))
            return
        if isinstance(s, (ast.With, ast.AsyncWith)):
            for item in s.items:
                self.expr(item.context_expr, scopes)
                if item.optional_vars is not None:
                    for n in ast.walk(item.optional_vars):
                        if isinstance(n, ast.Name):
                            scopes[n.id] = UNK
            self.block(s.body, scopes)
            return
        if isinstance(s, ast.Try) or s.__class__.__name__ == "TryStar":
            self.block(s.body, scopes)  # type: ignore[attr-defined]
            for h in s.handlers:  # type: ignore[attr-defined]
                self.block(h.body, dict(scopes))
            self.block(s.orelse, scopes)  # type: ignore[attr-defined]
            self.block(s.finalbody, scopes)  # type: ignore[attr-defined]
            return
        if s.__class__.__name__ == "Match":
            self.expr(s.subject, scopes)  # type: ignore[attr-defined]
            for case in s.cases:  # type: ignore[attr-defined]
                self.block(case.body, dict(scopes))
            return
        self.expr(s, scopes)

    @staticmethod
    def _merge(scopes: dict[str, Any], a: dict[str, Any], b: dict[str, Any]) -> None:
        for k in set(a) | set(b):
            va, vb = a.get(k, scopes.get(k, UNK)), b.get(k, scopes.get(k, UNK))
            if va == vb:
                scopes[k] = va
            elif isinstance(va, list) and isinstance(vb, list):
                scopes[k] = list(dict.fromkeys([*va, *vb]))[:CAP]
            else:
                scopes[k] = UNK

    def bind_target(self, t: ast.AST, value: ast.AST, scopes: dict[str, Any]) -> None:
        if isinstance(t, ast.Name):
            vs, _ = self.ev.strings(value, scopes)
            if vs is None:
                rows = self.ev.iter_rows(value, scopes) if isinstance(value, (ast.List, ast.Tuple, ast.Set)) else None
                vs = [str(r) for r in rows if isinstance(r, (str, int))] if rows and all(isinstance(r, (str, int)) for r in rows) else None
            scopes[t.id] = vs if vs is not None else UNK
            if _names(value) & self.f.loopnames:
                self.f.loopnames.add(t.id)
        else:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    scopes[n.id] = UNK

    def loop(self, s: ast.For | ast.AsyncFor, scopes: dict[str, Any]) -> None:
        self.expr(s.iter, scopes)
        target_names = [n.id for n in ast.walk(s.target) if isinstance(n, ast.Name)]
        rows = self.ev.iter_rows(s.iter, scopes)
        self.f.loopnames.update(target_names)
        if rows is not None:
            for row in rows:
                inner = bind_row(s.target, row, scopes)
                self.block(s.body, inner)
        else:
            inner = dict(scopes)
            it = s.iter
            param_sent = None
            if isinstance(it, ast.Name) and isinstance(scopes.get(it.id), list) and len(scopes[it.id]) == 1 \
                    and SENT_RE.fullmatch(str(scopes[it.id][0])):
                param_sent = scopes[it.id][0]  # `for p in procs` where procs is a parameter: p stands for its elements
            for n in target_names:
                inner[n] = [param_sent] if (param_sent and isinstance(s.target, ast.Name)) else UNK
            self.block(s.body, inner)
        self.block(s.orelse, dict(scopes))

    def expr(self, node: ast.AST, scopes: dict[str, Any]) -> None:
        if isinstance(node, ast.Call):
            self.on_call(node, scopes)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            elts = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
            self.comp(node.generators, 0, scopes, elts)
            return
        elif isinstance(node, ast.Lambda):
            inner = dict(scopes)
            for a in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                inner[a.arg] = UNK
            self.expr(node.body, inner)
            return
        for ch in ast.iter_child_nodes(node):
            self.expr(ch, scopes)

    def comp(self, gens: list[ast.comprehension], i: int, scopes: dict[str, Any], elts: list[ast.AST]) -> None:
        if i == len(gens):
            for e in elts:
                self.expr(e, scopes)
            return
        g = gens[i]
        self.expr(g.iter, scopes)
        names = [n.id for n in ast.walk(g.target) if isinstance(n, ast.Name)]
        self.f.loopnames.update(names)
        rows = self.ev.iter_rows(g.iter, scopes)
        if rows is not None:
            for row in rows:
                self.comp(gens, i + 1, bind_row(g.target, row, scopes), elts)
        else:
            inner = dict(scopes)
            for n in names:
                inner[n] = UNK
            self.comp(gens, i + 1, inner, elts)

    # ---------------- calls ----------------
    def spec(self, node: ast.AST, scopes: dict[str, Any]) -> list[str] | None:
        vs, _ = self.ev.strings(node, scopes)
        if vs is None and isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Name, ast.Call)):
            rows = self.ev.iter_rows(node, scopes) if not isinstance(node, ast.Set) else None
            if rows and all(isinstance(r, (str, int)) for r in rows):
                vs = [str(r) for r in rows]
        return vs[:CAP] if vs is not None else None

    def unrolled(self, node: ast.AST) -> bool:
        return bool(_names(node) & self.f.loopnames)

    def on_call(self, call: ast.Call, scopes: dict[str, Any]) -> None:
        name = _name_of(call.func)
        line = call.lineno
        is_attr = isinstance(call.func, ast.Attribute)
        kws = {k.arg: k.value for k in call.keywords if k.arg}
        # ---- SQL text
        sql_nodes: list[tuple[ast.AST, bool]] = []
        if (is_attr or name in ("read_sql", "read_sql_query")) and name in EXEC_METHODS and call.args:
            if not (isinstance(call.args[0], ast.Call) and _name_of(call.args[0].func) == "text"):
                sql_nodes.append((call.args[0], name != "run"))
        elif name == "sql" and is_attr and call.args:
            sql_nodes.append((call.args[0], True))
        elif name == "text" and call.args and not kws:
            sql_nodes.append((call.args[0], False))
        for kw, (multi, strict_kw) in SQL_KWARGS.items():
            if kw in kws and (is_attr or name in EXEC_METHODS or strict_kw):
                node = kws[kw]
                for leaf in (node.elts if multi and isinstance(node, (ast.List, ast.Tuple)) else [node]):
                    sql_nodes.append((leaf, strict_kw))
        for sn, strict in sql_nodes:
            for leaf in (sn.elts if isinstance(sn, (ast.List, ast.Tuple)) else [sn]):
                self.sql(leaf, scopes, line, strict)
        # ---- pandas / pyspark table and path references
        self.tables(call, name, is_attr, kws, scopes, line)
        # ---- call graph
        self.call_edge(call, name, kws, scopes, line)

    def sql(self, node: ast.AST, scopes: dict[str, Any], line: int, strict: bool) -> None:
        vs, _ = self.ev.strings(node, scopes)
        if vs is None:
            stringish = isinstance(node, (ast.JoinedStr, ast.BinOp)) or (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format")
            if stringish or (strict and isinstance(node, (ast.Name, ast.Attribute, ast.Subscript))):
                self.stats["unresolved_sql"] += 1
            return
        unrolled = self.unrolled(node)
        tpl: list[str] = []
        for v in vs:
            text = v.strip()
            if SENT_RE.search(text):
                if SENT_RE.match(text) or SQL_START_RE.match(text):
                    tpl.append(text)
                continue
            if not SQL_START_RE.match(text):
                continue
            self.f.add_sql(self._static(text, line, unrolled))
        if tpl:
            self.f.add_sql({"line": line, "unrolled": unrolled, "reads": [], "writes": [], "calls": [], "heuristic": False,
                            "tpl": {"sql": sorted(dict.fromkeys(tpl)), "reads": [], "writes": []}})

    def _static(self, text: str, line: int, unrolled: bool) -> dict[str, Any]:
        res = analyze_sql(text, self.dialect)
        heur = False
        if res["unparsed"]:
            self.stats["unparsed_sql"] += 1
            res = heuristic_sql(text)
            heur = True
        return {"line": line, "unrolled": unrolled, "reads": res["reads"], "writes": res["writes"], "calls": res["calls"],
                "heuristic": heur, "text": " ".join(text.split())[:120]}

    def _table_rec(self, side: str, node: ast.AST, scopes: dict[str, Any], line: int, schema: ast.AST | None = None) -> None:
        vs, _ = self.ev.strings(node, scopes)
        if vs is None:
            self.stats["unresolved_sql"] += 1
            return
        if schema is not None:
            ss, _ = self.ev.strings(schema, scopes)
            if ss is None:
                self.stats["unresolved_sql"] += 1
                return
            vs = [f"{s}.{v}" for s in ss for v in vs][:CAP]
        static = [node_key(v) for v in vs if not SENT_RE.search(v)]
        templ = [v for v in vs if SENT_RE.search(v)]
        rec: dict[str, Any] = {"line": line, "unrolled": self.unrolled(node), "reads": [], "writes": [], "calls": [],
                               "heuristic": False}
        rec["reads" if side == "r" else "writes"] = sorted(dict.fromkeys(static))
        if templ:
            rec["tpl"] = {"sql": [], "reads": templ if side == "r" else [], "writes": templ if side == "w" else []}
        if static or templ:
            self.f.add_sql(rec)

    def tables(self, call: ast.Call, name: str, is_attr: bool, kws: dict[str, ast.AST], scopes: dict[str, Any], line: int) -> None:
        args = call.args
        attrs, root = _chain(call.func)
        direction = _direction(attrs)
        if name == "read_sql_table" and args:
            self._table_rec("r", args[0], scopes, line, kws.get("schema") or (args[2] if len(args) > 2 else None))
        elif name == "to_sql" and is_attr and args:
            self._table_rec("w", args[0], scopes, line, kws.get("schema"))
        elif name == "table" and is_attr and args and (direction == "r" or re.search(r"spark|sess", root, re.I)):
            self._table_rec("r", args[0], scopes, line)
        elif name in WRITE_TABLE_METHODS and is_attr and args:
            self._table_rec("w", args[0], scopes, line)
        elif name == "forName" and args and root == "DeltaTable":
            self._table_rec("r", args[-1], scopes, line)
        elif name in DELTA_WRITE_METHODS and is_attr:
            for t in self._delta_of(call.func.value, scopes):  # type: ignore[attr-defined]
                self._table_rec("w", ast.Constant(value=t), {}, line)
        elif name == "jdbc" and direction and (len(args) > 1 or "table" in kws):
            self._table_rec(direction, kws.get("table") or args[1], scopes, line)
        elif name == "option" and len(args) >= 2 and direction and isinstance(args[0], ast.Constant):
            key = str(args[0].value).lower()
            if key in ("dbtable", "table"):
                self._table_rec(direction, args[1], scopes, line)
            elif key == "query":
                self.sql(args[1], scopes, line, True)
        elif name in READ_FILE_METHODS and is_attr and direction and (args or "path" in kws):
            node = kws.get("path") or args[0]
            vs, _ = self.ev.strings(node, scopes)
            if vs is None:
                self.stats["unresolved_sql"] += 1
                return
            paths = [path_node(v) for v in vs if is_path(v) or SENT_RE.search(v)]
            static = [p for p in paths if not SENT_RE.search(p)]
            templ = [v for v in vs if SENT_RE.search(v)]
            rec: dict[str, Any] = {"line": line, "unrolled": self.unrolled(node), "reads": [], "writes": [], "calls": [],
                                   "heuristic": False}
            rec["reads" if direction == "r" else "writes"] = sorted(dict.fromkeys(static))
            if templ:
                rec["tpl"] = {"sql": [], "reads": templ if direction == "r" else [], "writes": templ if direction == "w" else []}
            if static or templ:
                self.f.add_sql(rec)

    def _delta_assign(self, targets: list[ast.expr], value: ast.AST, scopes: dict[str, Any]) -> None:
        tabs = self._delta_of(value, scopes)
        if tabs and len(targets) == 1 and isinstance(targets[0], ast.Name):
            self.f.delta[targets[0].id] = tabs

    def _delta_of(self, node: ast.AST, scopes: dict[str, Any]) -> list[str]:
        """Table names behind a `DeltaTable.forName(..)` chain or a variable assigned from one."""
        if isinstance(node, ast.Name):
            return self.f.delta.get(node.id, [])
        while isinstance(node, (ast.Call, ast.Attribute)):
            if isinstance(node, ast.Call) and _name_of(node.func) == "forName" and node.args:
                vs, _ = self.ev.strings(node.args[-1], scopes)
                return [node_key(v) for v in (vs or []) if not SENT_RE.search(v)]
            node = node.func if isinstance(node, ast.Call) else node.value
            if isinstance(node, ast.Name):
                return self.f.delta.get(node.id, [])
        return []

    def call_edge(self, call: ast.Call, name: str, kws: dict[str, ast.AST], scopes: dict[str, Any], line: int) -> None:
        d = _dotted(call.func)
        callee, local = None, False
        if isinstance(call.func, ast.Name) and name in self.local_funcs:
            callee, local = name, True
        elif d and d[0] in ("self", "cls") and len(d) == 2 and self.cls:
            callee, local = f"{self.cls}.{d[1]}", True
        elif d and d[0] in self.local_classes and len(d) == 2:
            callee, local = f"{d[0]}.{d[1]}", True
        elif d and d[0] in self.aliases:
            callee = ".".join([self.aliases[d[0]], *d[1:]])
        if not callee:
            return
        args: list[list[str] | None] = []
        for a in call.args:
            if isinstance(a, ast.Starred):
                break
            args.append(self.spec(a, scopes))
        kw = {k: self.spec(v, scopes) for k, v in kws.items()}
        unrolled = any(self.unrolled(a) for a in [*call.args, *kws.values()])
        self.f.add_call({"callee": callee, "local": local, "line": line, "args": args, "kw": kw, "unrolled": unrolled})


def scan_tree(tree: ast.Module, rel: str, dialect: str) -> dict[str, Any]:
    return CodeScanner(tree, rel, dialect).scan()


def parse(src: str) -> ast.Module:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # scanned code is parsed, never run: its SyntaxWarnings are not ours
        return ast.parse(src)


# =========================== resolution across files ===========================

def bind_values(tpls: list[str], binding: dict[str, list[str]]) -> list[str] | None:
    """Substitute parameter sentinels in every template with the bound values; None when a parameter is unbound."""
    out: list[str] = []
    for t in tpls:
        names = list(dict.fromkeys(SENT_RE.findall(t)))
        if not names:
            out.append(t)
            continue
        if any(binding.get(n) is None for n in names):
            return None
        n = 1
        for p in names:
            n *= max(len(binding[p]), 1)
        if n > CAP:
            return None
        for combo in itertools.product(*[binding[p] for p in names]):
            m = dict(zip(names, combo))
            out.append(SENT_RE.sub(lambda mm, m=m: m[mm.group(1)], t))
    return list(dict.fromkeys(out))


class UtilGraph:
    """Call graph over every scanned file: which function a call names, and the SQL/table effects behind it."""

    def __init__(self, files: dict[str, dict[str, Any]], dialect: str, explicit: dict[str, str] | None = None) -> None:
        self.files = {r: f for r, f in files.items() if f.get("funcs")}
        self.dialect = dialect
        self.mod_of: dict[str, str] = {}       # rel -> primary module name
        self.is_pkg: dict[str, bool] = {}
        self.by_mod: dict[str, str] = {}       # module name -> rel (unique suffixes only)
        ambiguous: set[str] = set()
        for rel in sorted(files):
            names, pkg = module_names(rel)
            self.is_pkg[rel] = pkg
            primary = (explicit or {}).get(rel) or (names[0] if names else rel)
            self.mod_of[rel] = primary
            for n in [primary, *names]:
                if n in self.by_mod and self.by_mod[n] != rel:
                    ambiguous.add(n)
                self.by_mod.setdefault(n, rel)
        for n in ambiguous:
            if not any((explicit or {}).get(r) == n for r in files):
                self.by_mod.pop(n, None)
        for rel, n in (explicit or {}).items():
            self.by_mod[n] = rel
        self._analysis: dict[tuple[str, str], dict[str, Any]] = {}
        self.unresolved = 0

    # ---- resolving names
    def absolute(self, callee: str, rel: str) -> str:
        if not callee.startswith("."):
            return callee
        level = len(callee) - len(callee.lstrip("."))
        rest = callee.lstrip(".")
        base = self.mod_of.get(rel, "").split(".") if self.mod_of.get(rel) else []
        if not self.is_pkg.get(rel):
            base = base[:-1]
        base = base[: len(base) - (level - 1)] if level > 1 else base
        return ".".join([*base, rest]) if base else rest

    def lookup(self, callee: str, local: bool, rel: str, depth: int = 0) -> tuple[str, str] | None:
        """(file, function key) a call names, or None. Follows `from .x import f` re-exports through __init__ files."""
        if local:
            f = self.files.get(rel)
            return (rel, callee) if f and callee in f["funcs"] else None
        full = self.absolute(callee, rel)
        parts = full.split(".")
        for k in range(len(parts) - 1, 0, -1):
            mod = ".".join(parts[:k])
            target = self.by_mod.get(mod)
            if not target:
                continue
            key = ".".join(parts[k:])
            f = self.files.get(target)
            if f and key in f["funcs"]:
                return target, key
            imp = (self.files.get(target) or {}).get("imports", {}) if target in self.files else {}
            head = parts[k]
            if head in imp and depth < 3:
                again = imp[head] + ("." + ".".join(parts[k + 1:]) if parts[k + 1:] else "")
                return self.lookup(again, False, target, depth + 1)
        return None

    # ---- effects
    def _instantiate(self, rec: dict[str, Any], binding: dict[str, list[str]]) -> dict[str, Any] | None:
        tpl = rec.get("tpl")
        if not tpl:
            return rec
        texts = bind_values(tpl.get("sql", []), binding)
        reads = bind_values(tpl.get("reads", []), binding)
        writes = bind_values(tpl.get("writes", []), binding)
        if texts is None or reads is None or writes is None:
            self.unresolved += 1
            return None
        out = {"line": rec["line"], "unrolled": rec["unrolled"], "heuristic": rec["heuristic"],
               "reads": set(rec["reads"]) | {node_key(v) for v in reads}, "writes": set(rec["writes"]) | {node_key(v) for v in writes},
               "calls": set(rec["calls"])}
        for t in texts:
            if not SQL_START_RE.match(t):
                continue
            res = analyze_sql(t, self.dialect)
            if res["unparsed"]:
                res = heuristic_sql(t)
                out["heuristic"] = True
            out["reads"] |= set(res["reads"])
            out["writes"] |= set(res["writes"])
            out["calls"] |= set(res["calls"])
        out["unrolled"] = out["unrolled"] or len(texts) > 1
        return {**out, **{k: sorted(out[k]) for k in ("reads", "writes", "calls")}}

    def effects(self, rel: str, key: str, binding: dict[str, list[str]], depth: int, chain: list[tuple[str, str]]) -> list[dict[str, Any]]:
        fn = self.files[rel]["funcs"][key]
        label = f"{self.mod_of[rel]}.{key}"
        out: list[dict[str, Any]] = []
        for rec in fn["sql"]:
            inst = self._instantiate(rec, binding)
            if inst and (inst["reads"] or inst["writes"] or inst["calls"]):
                out.append({"via": [label], "hops": 1, **{k: inst[k] for k in ("reads", "writes", "calls", "heuristic", "unrolled")}})
        if depth < MAX_HOPS:
            for c in fn["calls"]:
                tgt = self.lookup(c["callee"], c["local"], rel)
                if not tgt or tgt in chain or tgt == (rel, key):
                    continue
                b2 = self.bind(tgt, c, binding)
                for e in self.effects(tgt[0], tgt[1], b2, depth + 1, [*chain, tgt]):
                    out.append({**e, "via": [label, *e["via"]], "hops": e["hops"] + 1,
                                "unrolled": e["unrolled"] or c["unrolled"]})
        return out

    def bind(self, tgt: tuple[str, str], call: dict[str, Any], outer: dict[str, list[str]]) -> dict[str, list[str]]:
        fn = self.files[tgt[0]]["funcs"][tgt[1]]
        params = fn["params"]
        b: dict[str, list[str]] = {}
        specs: dict[str, list[str] | None] = {}
        for p, a in zip(params, call["args"]):
            specs[p] = a
        for k, v in call["kw"].items():
            if k in params:
                specs[k] = v
        for p in params:
            if p in specs:
                v = specs[p]
                if v is not None:
                    got = bind_values(v, outer)
                    if got is not None:
                        b[p] = got
            elif p in fn["defaults"]:
                b[p] = fn["defaults"][p]
        return b

    def inherited(self, rel: str, calls: list[dict[str, Any]], outer: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
        """Effects a caller inherits through its util calls: records with `via` (`util:a.f` or `util:a.f>b.g`) and `hops`.
        `outer` binds the caller's own parameters (its defaults) when its call arguments are templates."""
        out: list[dict[str, Any]] = []
        for c in calls:
            tgt = self.lookup(c["callee"], c["local"], rel)
            if not tgt:
                continue
            b = self.bind(tgt, c, outer or {})
            for e in self.effects(tgt[0], tgt[1], b, 1, [tgt]):
                out.append({"via": "util:" + ">".join(e["via"]), "hops": e["hops"], "line": c["line"],
                            "reads": e["reads"], "writes": e["writes"], "calls": e["calls"], "heuristic": e["heuristic"],
                            "unrolled": bool(e["unrolled"] or c.get("unrolled"))})
        seen: set[str] = set()
        uniq = []
        for e in sorted(out, key=lambda e: (e["line"], e["via"], repr(e["reads"]), repr(e["writes"]), repr(e["calls"]))):
            k = repr((e["line"], e["via"], e["reads"], e["writes"], e["calls"]))
            if k not in seen:
                seen.add(k)
                uniq.append(e)
        return uniq
