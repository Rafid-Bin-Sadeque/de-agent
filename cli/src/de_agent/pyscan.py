"""Heuristic scan of Python code for database access (docs/SAFETY.md, "Python scripts"). Stdlib only, Python 3.9 compatible.

HEURISTIC, not a sandbox: it reads the code a command is about to run (`python -c`, heredoc, herestring, `python file.py`),
and when that code imports a database driver it pulls out string literals that look like SQL. Writes are denied; an
`execute(...)` whose argument is not a literal is denied as unreadable (fail closed). Code that imports no driver is not
touched. It does not follow imports of local modules, does not see code written and run in separate commands, and cannot
read SQL built at run time from files or the network.
"""
from __future__ import annotations

import ast
import os
import re
import shlex

from de_agent.clients import DB_CLIENTS, PY_DRIVER_DIALECT, PY_DRIVERS

UNREADABLE = "<unreadable: {}>"
_DRIVER_RE = re.compile(
    r"^\s*(?:import|from)\s+(" + "|".join(re.escape(d) for d in sorted(PY_DRIVERS, key=len, reverse=True)) + r")\b",
    re.M)
_DYN_IMPORT_RE = re.compile(r"\b(?:__import__|import_module)\s*\(")
_BOTO_SQL_RE = re.compile(r"execute_statement|batch_execute_statement|redshift-data|redshift_data|start_query_execution")
_PANDAS_SQL_RE = re.compile(r"\b(?:read_sql\w*|to_sql)\s*\(")
_SQL_START = re.compile(
    r"^\s*(?:/\*.*?\*/\s*|--[^\n]*\n\s*|\(\s*)*(?:"
    r"select\b|with\s+(?:recursive\s+)?\w+\s+as\b|insert\s+(?:into|overwrite)\b|update\s+\S+\s+set\b|delete\s+from\b|"
    r"merge\s+into\b|create\b|drop\b|alter\b|truncate\b|grant\b|revoke\b|copy\b|call\b|vacuum\b|unload\b|"
    r"explain\b|reindex\b|refresh\s+materialized\b|lock\s+table\b|set\s+\w+\s*(?:=|to\b)|reset\b|"
    r"replace\s+into\b|do\s+\$|begin\s*;|commit\s*;|rollback\s*;|start\s+transaction\b|pragma\b|attach\b|detach\b|"
    r"show\b|describe\b)", re.I | re.S)
_EXEC_METHODS = {
    "execute", "executemany", "executescript", "execute_batch", "execute_values", "exec_driver_sql",
    "copy_expert", "copy_from", "copy_to", "execute_statement", "batch_execute_statement", "start_query_execution",
    "execute_async", "read_sql", "read_sql_query", "read_sql_table", "raw", "run_sql",
}
_DYNAMIC_CALLS = {"exec", "eval", "compile", "__import__", "import_module", "run_path", "run_module", "execfile"}
_OPEN_FUNCS = {"open", "read_text", "read_bytes", "copy", "copyfile", "copy2", "move", "symlink", "link",
               "dotenv_values", "fileinput"}
_NO_VALUE = object()


def _name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


class _Scan:
    def __init__(self, tree: ast.AST):
        self.tree = tree
        self.env = {}  # simple name -> list of literal strings (None entry = also assigned a non-literal)
        self._collect_env()

    def _collect_env(self) -> None:
        for n in ast.walk(self.tree):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        v = self.value(n.value)
                        self.env.setdefault(t.id, []).append(v)
            elif isinstance(n, (ast.AugAssign, ast.AnnAssign)) and isinstance(n.target, ast.Name):
                v = self.value(n.value) if getattr(n, "value", None) is not None else None
                self.env.setdefault(n.target.id, []).append(v)

    def value(self, node):
        """The literal string a node evaluates to, or None when it is not a plain literal."""
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.JoinedStr):
            parts = []
            for p in node.values:
                if isinstance(p, ast.Constant) and isinstance(p.value, str):
                    parts.append(p.value)
                else:
                    parts.append("__v__")
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            a = self.value(node.left)
            if a is None:
                return None
            if isinstance(node.op, ast.Mod):
                return a
            b = self.value(node.right)
            return None if b is None else a + b
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in ("format", "strip", "lstrip", "rstrip", "lower", "upper", "replace"):
                return self.value(f.value)
            if _name(f) in ("text", "SQL", "dedent", "str", "Composed", "clean") and node.args:
                return self.value(node.args[0])
        if isinstance(node, ast.Name):
            vals = self.env.get(node.id)
            if vals and all(isinstance(v, str) for v in vals):
                return vals[-1]
        return None

    def literals(self):
        """Every maximal string expression in the tree, evaluated."""
        out = []

        def visit(node):
            if isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp)):
                v = self.value(node)
                if v is not None:
                    out.append(v)
                    if not isinstance(node, ast.BinOp):
                        return
                    return
            for ch in ast.iter_child_nodes(node):
                visit(ch)

        visit(self.tree)
        return out


def _looks_like_sql(s: str) -> bool:
    return len(s) >= 4 and bool(_SQL_START.match(s))


def _dialect(code: str) -> str:
    m = _DRIVER_RE.search(code)
    if m:
        return PY_DRIVER_DIALECT.get(m.group(1).split(".")[0], "postgres")
    if "redshift" in code:
        return "redshift"
    return "postgres"


def has_driver(code: str) -> bool:
    return bool(_DRIVER_RE.search(code) or _DYN_IMPORT_RE.search(code)
                or (re.search(r"\bboto3\b", code) and _BOTO_SQL_RE.search(code)) or _PANDAS_SQL_RE.search(code))


def _regex_literals(code: str):
    return [m.group(2) for m in re.finditer(r"""(?s)(\"\"\"|'''|"|')(.*?)\1""", code)]


def analyze(code: str, nested):
    """Return [(source, sql)] for a piece of Python code. `nested(shell_text)` extracts SQL from a shell string."""
    out = []
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        tree = None
    driver = has_driver(code)
    dialect = _dialect(code)
    src = "python:" + dialect
    if tree is None:
        if driver:
            lits = [s for s in _regex_literals(code) if _looks_like_sql(s)]
            out += [(src, s) for s in lits]
            if re.search(r"\.(?:execute\w*|read_sql\w*)\s*\(\s*[^'\"\sfrbFRB]", code):
                out.append((src, UNREADABLE.format("python execute() with non-literal SQL")))
            if not lits and not out:
                out.append((src, UNREADABLE.format("python code that imports a database driver does not parse")))
        return out
    sc = _Scan(tree)
    dynamic = False
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        name = _name(n.func)
        if name in _DYNAMIC_CALLS:
            arg = n.args[0] if n.args else None
            if arg is None or sc.value(arg) is None:
                dynamic = True
        if driver and name in _EXEC_METHODS:
            arg = n.args[0] if n.args else None
            if arg is None:
                for kw in n.keywords:
                    if kw.arg in ("sql", "query", "statement", "operation", "command", "Sql", "QueryString"):
                        arg = kw.value
            if arg is None or sc.value(arg) is None:
                out.append((src, UNREADABLE.format("python execute() with non-literal SQL")))
        if driver and name == "to_sql":
            out.append((src, UNREADABLE.format("DataFrame.to_sql writes to the database")))
    if dynamic:
        out.append(("python", UNREADABLE.format("python exec/eval/dynamic import of non-literal code")))
    lits = sc.literals()
    if driver:
        out += [(src, s) for s in lits if _looks_like_sql(s)]
    # shell commands and argv lists that run a database client
    for s in lits:
        if re.search(r"\b(?:" + "|".join(re.escape(c) for c in sorted(DB_CLIENTS | {"aws"})) + r")\b", s) and " " in s:
            out += nested(s)
    for n in ast.walk(tree):
        if isinstance(n, (ast.List, ast.Tuple)) and n.elts and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in n.elts):
            parts = [e.value for e in n.elts]
            if os.path.basename(parts[0]) in DB_CLIENTS | {"aws", "bash", "sh", "zsh"} and len(parts) > 1:
                out += nested(" ".join(shlex.quote(p) for p in parts))
    return out


def secret_opens(code: str, is_secret_path):
    """Secret-looking path literals passed to open()/read_text()/copy() and friends. Returns the offending literals."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return [m for m in _regex_literals(code) if re.search(r"open\s*\(", code) and is_secret_path(m)]
    sc = _Scan(tree)
    hits = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and _name(n.func) in _OPEN_FUNCS:
            for sub in [*n.args, *[k.value for k in n.keywords], n.func]:
                for m in ast.walk(sub):
                    if isinstance(m, (ast.Constant, ast.JoinedStr, ast.Name)):
                        v = sc.value(m)
                        if v and is_secret_path(os.path.expanduser(v)):
                            hits.append(v)
    return hits
