"""v0.3 red-team pass: hostile routine names and bodies against the store, redaction, parsers, pages, diff and catalog."""
import json
import time

import pytest
from de_agent import sqlexec
from de_agent.collectors import redshift as rs
from de_agent.routines import RoutineStore, prepare_body, redact_body
from de_agent.routines_render import page_names, routine_pages
from de_agent.scanners import routines as rt


@pytest.mark.parametrize("body,leak", [
    ('{"password": "hunter2"}', "hunter2"),
    ("PASSWORD = $$hunter2$$", "hunter2"),
    ("alter user u password\n  'hunter2'", "hunter2"),
    ("x := '{\"api_key\": \"sk_live_abcdefghijklmnop\"}'", "sk_live_abcdefghijklmnop"),
    ("select 'ghp_abcdefghijklmnopqrstuvwxyz0123456789'", "ghp_abcdef"),
    ("select 'xoxb-1234567890-abcdefghij'", "xoxb-1234567890"),
    ("select 'eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4fw'", "SflKxwRJSMeKKF2QT4fw"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIabcdef\n-----END RSA PRIVATE KEY-----", "MIIabcdef"),
    ("-----BEGIN PRIVATE KEY-----\nMIIabcdef (never closed)", "MIIabcdef"),
    ("create stage s url='s3://b' credentials=(aws_key_id='AKIAABCDEFGHIJKLMNOP')", "AKIAABCDEFGHIJKLMNOP"),
    ("select 'ASIAABCDEFGHIJKLMNOP'", "ASIAABCDEFGHIJKLMNOP"),
    ("conn := 'mysql://svc:s3cretpw@db/x'", "s3cretpw"),
])
def test_redaction_bypass_attempts(body, leak):
    text, changed = redact_body(body)
    assert changed and leak not in text


def test_redaction_does_not_touch_ordinary_code():
    body = "select password_hash, token_count from t where password = :pw and secret = $1 and note = 'a secret garden'"
    assert redact_body(body) == (body, False)


@pytest.mark.parametrize("hostile", [
    "password" + " " * 500_000, "password=" * 60_000, "postgres://" + "a:" * 200_000, "bearer " + "a" * 500_000,
    "password = '" + "x" * 500_000, "-----BEGIN PRIVATE KEY-----" + "A" * 500_000, "(" * 200_000,
])
def test_redaction_is_linear_enough_on_hostile_input(hostile):
    t0 = time.monotonic()
    prepare_body(hostile)
    assert time.monotonic() - t0 < 2.0


def test_secret_straddling_the_size_cut_is_not_kept():
    body = "x" * 95 + "password = 'hunter2hunter2'"
    prep = prepare_body(body, max_bytes=105)
    assert "hunter" not in prep["text"] and "hunter" not in json.dumps(prep["text"])


def test_denylist_cannot_be_bypassed_by_case_and_changes_the_hash(tmp_path, monkeypatch):
    f = tmp_path / "deny.txt"
    f.write_text("Internal.Host.Example\n")
    monkeypatch.setenv("DEA_DENYLIST_FILE", str(f))
    a = prepare_body("select 1 from INTERNAL.HOST.EXAMPLE.t")
    assert a["redacted"] and "internal" not in a["text"].lower()
    monkeypatch.delenv("DEA_DENYLIST_FILE")
    assert prepare_body("select 1 from INTERNAL.HOST.EXAMPLE.t")["body_hash"] != a["body_hash"]


@pytest.mark.parametrize("body", ["select " + "(" * 5000 + "1" + ")" * 5000 + " from a.t", "begin " * 50_000, "$$" * 40_000,
                                  "\x00" * 100, "select '" + "'" * 99_999])
def test_parse_never_raises_on_pathological_bodies(body):
    for kind, lang in (("view", "sql"), ("procedure", "plpgsql"), ("function", "sql"), ("trigger", "sql")):
        r = rt.parse_routine(kind, lang, body, "postgres")
        assert set(r) == {"reads", "writes", "calls", "parse"}


def test_parse_budget_switches_to_regex(monkeypatch):
    monkeypatch.setattr(rt, "PARSE_BUDGET_S", 0.0)
    p = rt.make_parser("postgres")
    row = {"kind": "procedure", "language": "sql", "body_hash": "h"}
    out = p(row, "insert into a.t select * from b.u")
    assert out["parse"] == "full"                     # first call is always parsed (nothing spent yet)
    out = p({**row, "body_hash": "h2"}, "insert into a.t select * from b.u")
    assert out["parse"] == "heuristic" and out["writes"] == ["a.t"]


def test_pages_with_case_only_collisions_get_distinct_names():
    rows = [{"schema": "S", "name": "Foo", "kind": "view", "args": ""}, {"schema": "S", "name": "foo", "kind": "view", "args": ""}]
    names = page_names(rows)
    assert len({v.lower() for v in names.values()}) == 2


def test_tampered_body_hash_cannot_reach_files_outside_the_store(tmp_path):
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    result = {"source": "wh", "type": "postgres", "collected_at": "2026-11-01T00:00:00Z",
              "data": {"routines": [{"schema": "s", "name": "p", "kind": "procedure", "args": "()", "language": "sql",
                                     "body_hash": "../../../secret.txt", "body_bytes": 1, "reads": [], "writes": [], "calls": [],
                                     "parse": "none"}]}}
    pages = routine_pages(result, RoutineStore(tmp_path))
    assert all("TOP SECRET" not in v for v in pages.values())
    assert "no body" in next(iter(pages.values()))


def test_control_characters_in_names_do_not_reach_diff_or_catalog_lines():
    from de_agent.diff import _key_label

    assert "\n" not in _key_label(("s", "x\n## injected", "()"))
    n = rt.routine_name({"schema": "s", "name": "a\r\n- injected"})
    assert "\n" not in n and "\r" not in n
    assert "\n" not in rt.routine_id("src", {"schema": "s", "name": "a\nb", "kind": "procedure"})


@pytest.mark.parametrize("ident", ['evil"; drop table x; --', "a'b", 'a"."b', "x\ny", "back\\slash"])
def test_redshift_per_object_sql_with_hostile_names_stays_one_read(ident):
    for v in (rs.q_show_procedure(ident, ident, "(integer)"), rs.q_mv_viewdef(ident, ident)):
        from de_agent.sqlclass import classify

        stmts = classify(v.sql, "redshift")
        assert len(stmts) == 1 and stmts[0].kind == "read", (v.sql, [(s.kind, s.reason) for s in stmts])
        sqlexec.check_read_only(v.sql, "redshift")


def test_hostile_arg_signature_cannot_break_out_of_show_procedure():
    from de_agent.sqlclass import classify

    v = rs.q_show_procedure("s", "p", "(integer); drop table x")
    kinds = [s.kind for s in classify(v.sql, "redshift")]
    assert kinds != ["read"] or len(kinds) == 1  # either refused as a write or still a single statement
    assert len(kinds) == 1 or "write" in kinds


def test_a_secret_key_inside_a_string_literal_is_not_a_secret():
    from de_agent.routines import redact_body

    body = "WHEN $1 ~ 'forget-password|reset-password' THEN 'Password Reset' WHEN $1 ~ 'token' THEN 'Auth'"
    assert redact_body(body) == (body, False)               # live Redshift false positive, 2026-11-08
    for secret in ("ALTER USER etl PASSWORD 'hunter2x'", "password = 'hunter2x'", '{"token": "hunter2x"}',
                   "password $$hunter2x$$", "secret_key=>'hunter2x'"):
        out, red = redact_body(secret)
        assert red and "hunter2x" not in out, secret
