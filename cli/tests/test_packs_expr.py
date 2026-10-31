"""Safe expression evaluator: semantics, and hostile input is a pack error, never execution."""
import time
from datetime import datetime, timezone

import pytest
from de_agent.packs import expr
from de_agent.packs.errors import PackError
from de_agent.packs.expr import compile_expr

NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)


def ev(src, env=None, mode="row"):
    return compile_expr(src, mode).eval(env or {}, NOW)


def test_comparisons_boolean_logic_and_arithmetic():
    assert ev("a > 3 and b == 'x'", {"a": 5, "b": "x"}) is True
    assert ev("a > 3 or not b", {"a": 1, "b": ""}) is True
    assert ev("(a + 2) * 3 - 1 == 20", {"a": 5}) is True
    assert ev("a / 4 >= 2.5", {"a": 10}) is True
    assert ev("a % 3 == 1 and a // 3 == 2", {"a": 7}) is True
    assert ev("2 ** 3 == 8") is True
    assert ev("-a < 0", {"a": 1}) is True
    assert ev("1 < a <= 5", {"a": 5}) is True


def test_in_and_word_operators():
    assert ev("s in ['a', 'b']", {"s": "a"}) is True
    assert ev("s not in ('a', 'b')", {"s": "c"}) is True
    assert ev("msg contains 'quota'", {"msg": "monthly quota exceeded"}) is True
    assert ev("msg startswith 'month'", {"msg": "monthly quota"}) is True
    assert ev("msg endswith 'quota'", {"msg": "monthly quota"}) is True
    assert ev("lower(msg) contains 'quota' and not msg contains 'x'", {"msg": "QUOTA hit"}) is True
    assert ev("tags contains 'x'", {"tags": ["x", "y"]}) is True
    assert ev("msg contains 'q'", {"msg": None}) is False
    assert ev("msg startswith 'q'", {"msg": 5}) is False


def test_a_field_called_contains_is_just_a_field():
    assert ev("contains == 1", {"contains": 1}) is True


def test_names_literals_subscript_and_functions():
    assert ev("field['odd name'] == 3", {"odd name": 3}) is True
    assert ev("len(name) == 3 and lower(name) == 'abc'", {"name": "ABC"}) is True
    assert ev("missing == None") is True
    assert ev("paused == True", {"paused": True}) is True


def test_none_is_safe_in_ordering_and_arithmetic():
    assert ev("x > 3", {"x": None}) is False
    assert ev("x + 1 > 3", {"x": None}) is False
    assert ev("x / 0 > 3", {"x": 5}) is False
    assert ev("x > 'a'", {"x": 5}) is False


def test_time_functions():
    assert ev("age_h(ts) > 5", {"ts": "2026-10-01T06:00:00Z"}) is True
    assert ev("age_h(ts) < 5", {"ts": "2026-10-01T08:00:00Z"}) is True
    assert ev("age_d(ts) >= 2", {"ts": "2026-09-29 11:00:00"}) is True
    assert ev("age_h(ts) > 1", {"ts": None}) is False
    assert ev("age_h(ts) > 1", {"ts": "not a time"}) is False
    assert ev("age_h(ts) > 1", {"ts": 1759300800}) is True        # epoch seconds
    assert ev("age_h(ts) > 1", {"ts": 1759300800000}) is True     # epoch milliseconds
    assert ev("ts < now()", {"ts": "2026-10-01T06:00:00Z"}) is True
    assert ev("now() > ts", {"ts": "2026-10-01T13:00:00Z"}) is False


def test_rows_mode_sees_count_and_params_only():
    assert ev("count > 0", {"count": 2}, "rows") is True
    assert ev("count > p_limit", {"count": 2, "p_limit": 1}, "rows") is True
    with pytest.raises(PackError, match="only use"):
        compile_expr("n > 0", "rows")
    with pytest.raises(PackError):
        compile_expr("field['x'] > 0", "rows")


HOSTILE = [
    "__import__('os').system('id')",
    "__import__('os')",
    "x.__class__",
    "().__class__.__bases__[0].__subclasses__()",
    "x.y",
    "'a'.upper()",
    "lambda: 1",
    "(lambda: 1)()",
    "[i for i in range(3)]",
    "{i for i in x}",
    "{i: 1 for i in x}",
    "(i for i in x)",
    "getattr(x, 'a')",
    "open('/etc/passwd')",
    "eval('1')",
    "exec('1')",
    "compile('1', 'f', 'eval')",
    "print(x)",
    "len(x, y)",
    "len(*x)",
    "len(x=1)",
    "now(1)",
    "x[0]",
    "x['a']",
    "field[0]",
    "field['a'].b",
    "field[x]",
    "field['a':'b']",
    "f'{x}'",
    "x if y else z",
    "x := 1",
    "yield x",
    "await x",
    "x is None",
    "x is not None",
    "a; b",
    "import os",
    "x = 1",
    "{'a': 1}",
    "{1, 2}",
    "b'bytes'",
    "1j",
    "...",
    "__builtins__",
    "__name__",
    "x @ y",              # typed by the user: reserved for the word operators
    "x << 2",
    "x >> 2",
    "x & 1",
    "x | 1",
    "x ^ 1",
    "~x",
    "10 ** 10 ** 10",
    "9 ** 9 ** 9",
    "2 ** 100000",
    "10 ** 100",
    "(10 ** 9) ** 64 * 10 ** 9 * 10 ** 9 * 10 ** 9 * 10 ** 9",
    "99999999999999999999999 + 1",
    "1e999 > 1",
    "(" * 300 + "1" + ")" * 300,
    "-" * 300 + "1",
    "not " * 200 + "x",
    "+".join(["1"] * 200),
    "x" + " + x" * 130,
    "a and " * 130 + "b",
    "[[[[[[[[[[[[[[1]]]]]]]]]]]]]]",
    "x" * 500,
    "a\nb",
    "a # comment",
    "\x00",
    "",
    "   ",
    "if x: y",
    "x in",
    "x contains",
    "not",
    "x == == 1",
    "‮ x",
]


@pytest.mark.parametrize("src", HOSTILE)
def test_hostile_input_is_a_pack_error(src):
    t0 = time.monotonic()
    with pytest.raises(PackError):
        compile_expr(src).eval({"x": 1, "y": 2, "z": 3, "a": 1, "b": 2}, NOW)
    assert time.monotonic() - t0 < 1.0


def test_sequence_repetition_never_allocates():
    t0 = time.monotonic()
    assert ev("'a' * 10 ** 9") is None          # arithmetic is numbers only: no string/list repetition
    assert ev("[0] * 10 ** 9") is None
    assert ev("s * 1000000000", {"s": "a"}) is None
    assert time.monotonic() - t0 < 0.5


def test_non_string_expression_is_an_error():
    for bad in (None, 5, ["x"], {"a": 1}):
        with pytest.raises(PackError):
            compile_expr(bad)


def test_exponent_guard_applies_at_evaluation_time_too():
    e = compile_expr("a ** b > 1")
    with pytest.raises(PackError, match="exponent"):
        e.eval({"a": 10, "b": 1000}, NOW)
    with pytest.raises(PackError, match="exponent"):
        e.eval({"a": 10**12, "b": 2}, NOW)
    assert e.eval({"a": 2, "b": 10}, NOW) is True


def test_evaluation_budget_is_enforced(monkeypatch):
    monkeypatch.setattr(expr, "MAX_STEPS", 5)
    with pytest.raises(PackError, match="budget"):
        compile_expr("a + b + c + d + e + f + g > 1").eval({k: 1 for k in "abcdefg"}, NOW)


def test_evaluation_time_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(expr, "MAX_SECONDS", -1.0)  # deadline already passed: the 64th step stops it
    def tree(n):
        return "a" if n == 0 else f"({tree(n - 1)} + {tree(n - 1)})"

    src = tree(5) + " > 1"  # 32 leaves, depth 6: well inside the size limits
    with pytest.raises(PackError, match="budget"):
        compile_expr(src).eval({"a": 1}, NOW)


def test_the_whole_hostile_corpus_runs_fast():
    t0 = time.monotonic()
    for src in HOSTILE:
        try:
            compile_expr(src).eval({"x": 1}, NOW)
        except PackError:
            pass
    assert time.monotonic() - t0 < 3.0


def test_no_expression_can_reach_builtins_or_modules(tmp_path):
    marker = tmp_path / "pwned"
    for src in (f"__import__('os').system('touch {marker}')", f"open('{marker}', 'w')"):
        with pytest.raises(PackError):
            compile_expr(src).eval({}, NOW)
    assert not marker.exists()
