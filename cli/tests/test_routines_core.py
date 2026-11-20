"""v0.3 section 1: row contract, normalization, redaction, content-addressed store, prune."""
from __future__ import annotations

import hashlib

import pytest
from de_agent.routines import (
    RoutineStore,
    finalize_rows,
    normalize_body,
    prepare_body,
    redact_body,
    referenced_hashes,
    safe_component,
    validate_routine_options,
)


def test_normalize_keeps_comments_strips_edges():
    raw = "\r\n\r\n-- keep me  \r\nselect 1;   \r\n\r\n\r\n"
    assert normalize_body(raw) == "-- keep me\nselect 1;"


def test_hash_is_of_normalized_redacted_text():
    a = prepare_body("select 1  \r\n")
    b = prepare_body("select 1")
    assert a["body_hash"] == b["body_hash"] == hashlib.sha256(b"select 1").hexdigest()
    assert a["body_bytes"] == len("select 1  \r\n")


# built at runtime so the source holds no key-shaped literal (GitHub push protection scans for them)
FAKE_KEY_ID = "AKIA" + "TEST" * 4
FAKE_SECRET = "fake" + "/" + "x" * 35


@pytest.mark.parametrize("body", [
    "connect('postgres://user:hunter2@db/x')",
    "x := 'password=hunter2 host=a';",
    "PASSWORD = 'hunter2'",
    "create user u password 'hunter2'",
    "key " + FAKE_KEY_ID,
    "AWS_SECRET_ACCESS_KEY=" + FAKE_SECRET,
    "copy t from 's3://b' credentials 'aws_iam_role=arn:aws:iam::1:role/x'",
    "headers := 'Authorization: Bearer abcdefghijklmnop1234567890'",
    "api_key: \"sk_live_abcdef\"",
])
def test_redaction_catches_secrets(body):
    text, changed = redact_body(body)
    assert changed and "<redacted>" in text
    for leak in ("hunter2", FAKE_KEY_ID, FAKE_SECRET,
                 "abcdefghijklmnop1234567890", "sk_live_abcdef", "role/x"):
        assert leak not in text


def test_redaction_leaves_placeholders_and_plain_code():
    body = "select password_hash, 'a token of appreciation' from users where pwd = $1 and password=%s"
    text, changed = redact_body(body)
    assert text == body and not changed


def test_denylist_entries_redacted(tmp_path, monkeypatch):
    f = tmp_path / "deny.txt"
    f.write_text("Acme-Internal-Host\n\nsecret-project\n")
    monkeypatch.setenv("DEA_DENYLIST_FILE", str(f))
    text, changed = redact_body("select * from acme-internal-host.t -- SECRET-PROJECT")
    assert changed and "internal" not in text.lower() and "secret-project" not in text.lower()
    prep = prepare_body("select 'Acme-Internal-Host'")
    assert prep["redacted"] and "Acme" not in prep["text"]


def test_redacted_text_is_what_is_hashed():
    prep = prepare_body("select 'password=hunter2'")
    assert "hunter2" not in prep["text"]
    assert prep["body_hash"] == hashlib.sha256(prep["text"].encode()).hexdigest()


def test_truncation_flag_and_secret_at_the_cut_is_not_half_kept():
    body = "x" * 90 + " password=hunter2secretvalue"
    prep = prepare_body(body, max_bytes=100)
    assert prep["truncated"] and len(prep["text"].encode()) <= 100
    assert "hunter" not in prep["text"]
    assert prep["body_bytes"] == len(body)
    assert hashlib.sha256(prep["text"].encode()).hexdigest() == prep["body_hash"]


def test_oversized_body_is_cut_and_still_stored(tmp_path):
    store = RoutineStore(tmp_path)
    big = "select 1;\n" * 200_000
    rows = finalize_rows([{"schema": "s", "name": "n", "kind": "function", "definition": big}],
                         {"routines_max_bytes": 1000}, store)
    r = rows[0]
    assert r["truncated"] and r["body_bytes"] == len(big)
    assert len(store.get(r["body_hash"]).encode()) <= 1000


def test_row_contract_and_key_dedupe(tmp_path):
    raw = [
        {"schema": "a", "name": "f", "kind": "function", "args": "(INTEGER, TEXT)", "language": "SQL",
         "returns": "INTEGER", "owner": "o", "definition": "select 1"},
        {"schema": "a", "name": "f", "kind": "function", "args": "(integer, text)", "language": "sql", "definition": "x"},
        {"schema": "a", "name": "v", "kind": "view", "args": "", "language": "sql", "definition": None},
        {"schema": "a", "name": "t", "kind": "trigger", "definition": "create trigger"},
    ]
    rows = finalize_rows(raw, {"routine_kinds": ["function", "view"]}, RoutineStore(tmp_path))
    assert [(r["name"], r["args"]) for r in rows] == [("f", "(integer, text)"), ("v", "")]
    f, v = rows
    assert set(f) == {"schema", "name", "kind", "args", "language", "returns", "owner", "body_hash", "body_bytes",
                      "truncated", "redacted", "reads", "writes", "calls", "parse"}
    assert f["language"] == "sql" and f["returns"] == "integer" and f["parse"] == "none"
    assert v["body_hash"] == "" and v["body_bytes"] == 0 and "definition" not in f


def test_routines_max_errors_instead_of_cutting():
    raw = [{"schema": "s", "name": f"n{i}", "kind": "view", "definition": "select 1"} for i in range(3)]
    with pytest.raises(ValueError, match="routines_max"):
        finalize_rows(raw, {"routines_max": 2}, None)


def test_store_is_content_addressed_and_shared(tmp_path):
    store = RoutineStore(tmp_path)
    raw = [{"schema": "s", "name": "a", "kind": "view", "definition": "select 1"},
           {"schema": "s", "name": "b", "kind": "view", "definition": "select 1"}]
    rows = finalize_rows(raw, {}, store)
    assert rows[0]["body_hash"] == rows[1]["body_hash"]
    files = list((tmp_path / ".dea" / "routines").rglob("*.sql"))
    assert len(files) == 1 and files[0].parent.name == rows[0]["body_hash"][:2]
    assert store.get(rows[0]["body_hash"]) == "select 1"


@pytest.mark.parametrize("bad", ["../../etc/passwd", "a" * 63, "A" * 64, "", "../" + "a" * 61, None])
def test_store_rejects_non_hash_keys(tmp_path, bad):
    store = RoutineStore(tmp_path)
    assert store.path(bad) is None and store.get(bad) is None
    store.put(bad, "x")  # no-op, no exception
    assert not (tmp_path / ".dea").exists()


def test_prune_drops_unreferenced_only(tmp_path):
    store = RoutineStore(tmp_path)
    keep, drop = "a" * 64, "b" * 64
    store.put(keep, "k")
    store.put(drop, "d")
    (store.root / "aa" / "stray.tmp").write_text("x")
    assert store.prune({keep}) == 2
    assert store.get(keep) == "k" and store.get(drop) is None
    assert not (store.root / "bb").exists()


def test_referenced_hashes_reads_every_source():
    res = [{"data": {"routines": [{"body_hash": "h1"}, {"body_hash": ""}]}}, {"data": {}}, {"data": {"routines": None}}]
    assert referenced_hashes(res) == {"h1", ""}


def test_snapshot_prune_removes_bodies_of_dropped_snapshots(tmp_path):
    import json

    from de_agent.snapshot import _prune, snapshots_dir

    store = RoutineStore(tmp_path)
    old, new = "1" * 64, "2" * 64
    store.put(old, "old")
    store.put(new, "new")
    for sid, h in (("20260101T000000Z", old), ("20260102T000000Z", new)):
        d = snapshots_dir(tmp_path) / sid
        d.mkdir(parents=True)
        (d / "wh.json").write_text(json.dumps({"data": {"routines": [{"body_hash": h}]}}))
    _prune(tmp_path, keep=1)
    assert store.get(old) is None and store.get(new) == "new"


@pytest.mark.parametrize("name,expect", [
    ("../../x", "____x"), ("a/b\\c", "a_b_c"), ("..", "_"), ("", "_"), ("ok.name$1", "ok.name$1"), ("a" * 300, "a" * 120),
    ("evil\x00name", "evil_name"),
])
def test_safe_component(name, expect):
    assert safe_component(name) == expect
    assert "/" not in safe_component(name) and ".." not in safe_component(name).replace("...", "")


def test_option_validation():
    validate_routine_options({"routines": False, "routine_kinds": ["view"], "routines_max": 10, "routines_max_bytes": 100})
    for bad in ({"routines": "yes"}, {"routine_kinds": []}, {"routine_kinds": ["x"]}, {"routines_max": 0},
                {"routines_max_bytes": 10**9}, {"routines_max": True}):
        with pytest.raises(ValueError):
            validate_routine_options(bad)


def test_concurrent_puts_of_the_same_body_do_not_collide(tmp_path):
    import threading

    store = RoutineStore(tmp_path)
    h = "c" * 64
    errs = []

    def go():
        try:
            for _ in range(50):
                store.put(h, "same")
                (store.root / h[:2] / f"{h}.sql").unlink(missing_ok=True)
        except Exception as ex:  # pragma: no cover
            errs.append(ex)

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errs == []
