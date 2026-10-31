"""Shared static-evaluation helpers for the Python-reading scanners (airflow, python): constants, string unrolling.

Nothing here imports or runs scanned code; everything is `ast` over source text.
"""
from __future__ import annotations

import ast
import itertools
from typing import Any

DATASET_NAMES = {"Dataset", "Asset"}
CAP = 500  # max unrolled combinations per expression
_STR_METHODS = ("strip", "lstrip", "rstrip", "lower", "upper")


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
        """Values of `for x in <node>` when it is a constant list/tuple/dict-keys/range/`D.keys()`/`sorted(X)`; else None."""
        vals = self._iter_values(node, scopes)
        if vals is None and isinstance(node, ast.Call):
            rows = self.iter_rows(node, scopes)
            if rows is not None and all(isinstance(r, (str, int)) and not isinstance(r, bool) for r in rows):
                return list(rows)
        return vals

    def _iter_values(self, node: ast.AST, scopes: dict[str, Any]) -> list[Any] | None:
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

    def _seq(self, node: ast.AST, scopes: dict[str, Any]) -> list[Any] | None:
        """A list/tuple/dict constant behind `node` (a name, a literal, or a scoped list), else None."""
        if isinstance(node, ast.Name):
            if node.id in scopes:
                v = scopes[node.id]
                return list(v) if isinstance(v, list) else None
            return self.consts.get(node.id)
        if isinstance(node, (ast.List, ast.Tuple, ast.Dict)):
            try:
                return ast.literal_eval(node)
            except ValueError:
                return None
        return None

    def iter_rows(self, node: ast.AST, scopes: dict[str, Any]) -> list[Any] | None:
        """Rows of `for <target> in <node>`: scalars or tuples. Handles constants, `D.items()/keys()/values()`,
        `zip(A, B)`, `sorted(X)`, `list(X)`, `range(n)`. None when not enumerable or over CAP."""
        rows: list[Any] | None = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and not node.args \
                and node.func.attr in ("items", "keys", "values"):
            base = self._seq(node.func.value, scopes)
            if isinstance(base, dict):
                rows = {"items": list(base.items()), "keys": list(base), "values": list(base.values())}[node.func.attr]
        elif isinstance(node, ast.Call) and _name_of(node.func) in ("sorted", "list", "tuple", "reversed") and len(node.args) == 1:
            inner = self.iter_rows(node.args[0], scopes)
            if inner is not None:
                rows = sorted(inner, key=repr) if _name_of(node.func) == "sorted" else (
                    inner[::-1] if _name_of(node.func) == "reversed" else inner)
        elif isinstance(node, ast.Call) and _name_of(node.func) == "zip" and node.args and not node.keywords:
            cols = [self.iter_rows(a, scopes) for a in node.args]
            if all(c is not None for c in cols):
                rows = list(zip(*cols))
        elif isinstance(node, ast.Call) and _name_of(node.func) == "range":
            rows = self.iter_values(node, scopes)
        else:
            val = self._seq(node, scopes)
            if isinstance(val, dict):
                rows = list(val)
            elif isinstance(val, (list, tuple)):
                rows = list(val)
        if rows is None or len(rows) > CAP:
            return None
        return rows

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
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in _STR_METHODS and not node.args and not node.keywords:
                base, ub = self.strings(f.value, scopes)
                if base is None:
                    return None, ub
                return [getattr(b, f.attr)() for b in base], ub
            if _name_of(f) in ("dedent", "str") and len(node.args) == 1 and not node.keywords:
                return self.strings(node.args[0], scopes)
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


def bind_row(target: ast.AST, row: Any, scopes: dict[str, Any]) -> dict[str, Any]:
    """`scopes` extended with a loop target bound to ONE row: `for a, (b, c) in ...` binds each name to [value]."""
    out = dict(scopes)

    def put(t: ast.AST, v: Any) -> None:
        if isinstance(t, ast.Name):
            out[t.id] = [v] if isinstance(v, (str, int)) and not isinstance(v, bool) else UNK
        elif isinstance(t, (ast.Tuple, ast.List)):
            if isinstance(v, (tuple, list)) and len(v) == len(t.elts) and not any(isinstance(e, ast.Starred) for e in t.elts):
                for e, x in zip(t.elts, v):
                    put(e, x)
            else:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        out[n.id] = UNK
        else:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    out[n.id] = UNK

    put(target, row)
    return out
