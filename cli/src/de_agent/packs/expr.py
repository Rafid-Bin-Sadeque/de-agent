"""Safe expressions for pack rules (`when:` per row, `rows:` per section).

Built on `ast` with a whitelist. Nothing is ever `eval`ed or compiled: the tree is walked by a tiny interpreter that knows
comparisons, `and`/`or`/`not`, number arithmetic, `in`, the word operators `contains` / `startswith` / `endswith`,
names (row fields, `count`, `p_<param>`), literals, `field['x']` and the functions `len`, `lower`, `age_h`, `age_d`, `now`.
Anything else is a PackError. Size, depth, step and time are bounded, so a hostile rule cannot hang or exhaust the process.
"""
from __future__ import annotations

import ast
import io
import time
import tokenize
from datetime import datetime, timezone
from typing import Any

from de_agent.packs.errors import PackError

MAX_LEN = 400
MAX_NODES = 120
MAX_DEPTH = 12
MAX_STEPS = 4000
MAX_SECONDS = 0.25
MAX_INT = 10**18
MAX_RESULT = 10**30
MAX_EXPONENT = 64
FUNCS = {"len": 1, "lower": 1, "age_h": 1, "age_d": 1, "now": 0}

# word operators are rewritten to Python operators that real rules may not use (rejected in the source text)
_WORD_OPS = {"contains": "@", "startswith": "<<", "endswith": ">>"}
_BANNED_OPS = {"@", "<<", ">>", "@=", "<<=", ">>=", ":=", "->", "...", ";", "`"}
_OP_NAMES = {ast.MatMult: "contains", ast.LShift: "startswith", ast.RShift: "endswith"}
_ARITH = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_CMP = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn)


def _rewrite(src: str) -> str:
    """contains / startswith / endswith between two operands -> @ / << / >> (precedence of arithmetic, above comparisons)."""
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError, ValueError) as ex:
        raise PackError(f"cannot parse expression: {type(ex).__name__}") from None
    spans: list[tuple[int, int, str]] = []
    prev = None
    for t in toks:
        if t.type == tokenize.OP and t.string in _BANNED_OPS:
            raise PackError(f"unsupported operator {t.string!r}")
        if t.type == tokenize.COMMENT:
            raise PackError("comments are not allowed in expressions")
        if (t.type == tokenize.NAME and t.string in _WORD_OPS and prev is not None
                and (prev.type in (tokenize.NUMBER, tokenize.STRING) or prev.string in (")", "]")
                     or (prev.type == tokenize.NAME and prev.string not in ("and", "or", "not", "in", "is", "if", "else")))):
            spans.append((t.start[1], t.end[1], _WORD_OPS[t.string]))
        if t.type not in (tokenize.NEWLINE, tokenize.NL, tokenize.ENDMARKER, tokenize.INDENT, tokenize.DEDENT):
            prev = t
    for start, end, op in reversed(spans):
        src = src[:start] + op + src[end:]
    return src


class Expr:
    """A validated expression. `eval(env)` is pure: no I/O, no attribute access, bounded work."""

    def __init__(self, source: str, tree: ast.Expression, names: set[str]) -> None:
        self.source = source
        self.tree = tree
        self.names = names

    def eval(self, env: dict[str, Any], now: datetime | None = None) -> Any:
        return _Interp(env, now).run(self.tree.body)

    def truthy(self, env: dict[str, Any], now: datetime | None = None) -> bool:
        return bool(self.eval(env, now))


def compile_expr(source: Any, mode: str = "row") -> Expr:
    """Parse and validate. mode `row`: names are row fields (or `p_<param>`); mode `rows`: only `count` (and `p_<param>`)."""
    if not isinstance(source, str) or not source.strip():
        raise PackError("expression must be a non-empty string")
    if len(source) > MAX_LEN:
        raise PackError(f"expression longer than {MAX_LEN} characters")
    if "\n" in source or "\r" in source or "\x00" in source:
        raise PackError("expression must be a single line")
    text = _rewrite(source.strip())
    try:
        tree = ast.parse(text, mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        raise PackError("syntax error in expression") from None
    names: set[str] = set()
    count = [0]

    def walk(n: ast.AST, depth: int) -> None:
        count[0] += 1
        if count[0] > MAX_NODES:
            raise PackError(f"expression has more than {MAX_NODES} nodes")
        if depth > MAX_DEPTH:
            raise PackError(f"expression nested deeper than {MAX_DEPTH}")
        d = depth + 1
        if isinstance(n, ast.BoolOp):
            for v in n.values:
                walk(v, d)
        elif isinstance(n, ast.UnaryOp):
            if not isinstance(n.op, (ast.Not, ast.USub, ast.UAdd)):
                raise PackError("unsupported unary operator")
            walk(n.operand, d)
        elif isinstance(n, ast.BinOp):
            if not isinstance(n.op, (*_ARITH, *_OP_NAMES)):
                raise PackError("unsupported operator")
            walk(n.left, d)
            walk(n.right, d)
        elif isinstance(n, ast.Compare):
            if not all(isinstance(o, _CMP) for o in n.ops):
                raise PackError("unsupported comparison (use ==, !=, <, <=, >, >=, in, not in)")
            walk(n.left, d)
            for c in n.comparators:
                walk(c, d)
        elif isinstance(n, ast.Name):
            if n.id.startswith("__") or n.id in FUNCS:
                raise PackError(f"name {n.id!r} is not allowed")
            if mode == "rows" and n.id != "count" and not n.id.startswith("p_"):
                raise PackError(f"`rows:` rules can only use `count` (and p_<param>), not {n.id!r}")
            names.add(n.id)
        elif isinstance(n, ast.Constant):
            if not (n.value is None or isinstance(n.value, (bool, str)) or
                    (isinstance(n.value, (int, float)) and abs(n.value) < MAX_INT)):
                raise PackError("unsupported literal")
        elif isinstance(n, (ast.List, ast.Tuple)):
            for e in n.elts:
                walk(e, d)
        elif isinstance(n, ast.Call):
            if not isinstance(n.func, ast.Name) or n.func.id not in FUNCS:
                raise PackError("only these functions may be called: " + ", ".join(sorted(FUNCS)))
            if n.keywords or len(n.args) != FUNCS[n.func.id]:
                raise PackError(f"{n.func.id}() takes {FUNCS[n.func.id]} positional argument(s)")
            for a in n.args:
                if isinstance(a, ast.Starred):
                    raise PackError("starred arguments are not allowed")
                walk(a, d)
        elif isinstance(n, ast.Subscript):
            ok = (isinstance(n.value, ast.Name) and n.value.id == "field" and isinstance(n.slice, ast.Constant)
                  and isinstance(n.slice.value, str))
            if not ok:
                raise PackError("the only subscript allowed is field['name']")
            if mode == "rows":
                raise PackError("`rows:` rules have no row fields")
        else:
            raise PackError(f"unsupported syntax: {type(n).__name__}")

    walk(tree.body, 0)
    return Expr(source, tree, names)


# ---- interpreter -------------------------------------------------------------------------------------------

def to_dt(v: Any) -> datetime | None:
    """datetime | ISO string | epoch seconds/milliseconds -> aware UTC datetime (None when it is not a time)."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        secs = v / 1000.0 if abs(v) > 1e11 else float(v)
        try:
            return datetime.fromtimestamp(secs, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(v, str):
        try:
            d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class _Interp:
    def __init__(self, env: dict[str, Any], now: datetime | None) -> None:
        self.env = env
        self.now = now or datetime.now(timezone.utc)
        self.steps = 0
        self.deadline = time.monotonic() + MAX_SECONDS

    def tick(self) -> None:
        self.steps += 1
        if self.steps > MAX_STEPS or (self.steps % 64 == 0 and time.monotonic() > self.deadline):
            raise PackError("expression evaluation exceeded its step/time budget")

    def run(self, n: ast.AST) -> Any:
        self.tick()
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Name):
            return self.env.get(n.id)
        if isinstance(n, (ast.List, ast.Tuple)):
            return [self.run(e) for e in n.elts]
        if isinstance(n, ast.BoolOp):
            val: Any = None
            for v in n.values:
                val = self.run(v)
                if isinstance(n.op, ast.And) and not val:
                    return val
                if isinstance(n.op, ast.Or) and val:
                    return val
            return val
        if isinstance(n, ast.UnaryOp):
            v = self.run(n.operand)
            if isinstance(n.op, ast.Not):
                return not v
            if not _is_num(v):
                return None
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BinOp):
            return self.binop(n)
        if isinstance(n, ast.Compare):
            left = self.run(n.left)
            for op, comp in zip(n.ops, n.comparators):
                right = self.run(comp)
                if not self.compare(op, left, right):
                    return False
                left = right
            return True
        if isinstance(n, ast.Subscript):
            return self.env.get(n.slice.value)  # type: ignore[attr-defined]
        if isinstance(n, ast.Call):
            return self.call(n.func.id, [self.run(a) for a in n.args])  # type: ignore[attr-defined]
        raise PackError(f"unsupported syntax: {type(n).__name__}")  # unreachable after validation

    def binop(self, n: ast.BinOp) -> Any:
        a, b = self.run(n.left), self.run(n.right)
        kind = _OP_NAMES.get(type(n.op))
        if kind == "contains":
            if isinstance(a, str):
                return b is not None and str(b) in a
            return isinstance(a, list) and b in a
        if kind in ("startswith", "endswith"):
            if not (isinstance(a, str) and isinstance(b, str)):
                return False
            return a.startswith(b) if kind == "startswith" else a.endswith(b)
        if not (_is_num(a) and _is_num(b)):
            return None
        op = n.op
        try:
            if isinstance(op, ast.Add):
                r = a + b
            elif isinstance(op, ast.Sub):
                r = a - b
            elif isinstance(op, ast.Mult):
                r = a * b
            elif isinstance(op, ast.Div):
                r = a / b
            elif isinstance(op, ast.FloorDiv):
                r = a // b
            elif isinstance(op, ast.Mod):
                r = a % b
            else:
                if abs(b) > MAX_EXPONENT or abs(a) > 10**9:
                    raise PackError("exponent too large")
                r = a ** b
        except ZeroDivisionError:
            return None
        except OverflowError:
            raise PackError("arithmetic overflow") from None
        if isinstance(r, complex) or abs(r) > MAX_RESULT:
            raise PackError("arithmetic result out of range")
        return r

    def compare(self, op: ast.cmpop, a: Any, b: Any) -> bool:
        if isinstance(op, (ast.In, ast.NotIn)):
            if isinstance(b, (list, tuple)):
                hit = a in b
            elif isinstance(b, str) and isinstance(a, str):
                hit = a in b
            else:
                hit = False
            return hit if isinstance(op, ast.In) else not hit
        if isinstance(a, datetime) != isinstance(b, datetime):  # one side is now() / a datetime: parse the other
            da, db = to_dt(a), to_dt(b)
            if da is not None and db is not None:
                a, b = da, db
        elif isinstance(a, datetime) and isinstance(b, datetime):
            a, b = to_dt(a), to_dt(b)
        if isinstance(op, ast.Eq):
            return a == b
        if isinstance(op, ast.NotEq):
            return a != b
        if a is None or b is None or isinstance(a, bool) or isinstance(b, bool):
            return False
        if _is_num(a) != _is_num(b) or (not _is_num(a) and type(a) is not type(b)):
            return False
        if isinstance(op, ast.Lt):
            return a < b
        if isinstance(op, ast.LtE):
            return a <= b
        if isinstance(op, ast.Gt):
            return a > b
        return a >= b

    def call(self, name: str, args: list[Any]) -> Any:
        if name == "now":
            return self.now
        v = args[0]
        if name == "len":
            return len(v) if isinstance(v, (str, list)) else None
        if name == "lower":
            return v.lower() if isinstance(v, str) else None
        d = to_dt(v)
        if d is None:
            return None
        secs = (self.now - d).total_seconds()
        return secs / 3600.0 if name == "age_h" else secs / 86400.0
