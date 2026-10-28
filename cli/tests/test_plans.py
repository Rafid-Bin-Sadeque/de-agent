"""Plans, approvals, apply, query (M2 section 5)."""
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest
from de_agent import approval
from de_agent.applyplan import ApplyRefused, apply_plan, authorize
from de_agent.cli import app
from de_agent.collectors.base import CollectorError
from de_agent.config import load_config
from de_agent.extract import extract_sql
from de_agent.plans import (
    check_plan,
    eval_expect,
    load_plan,
    parse_plan,
    plan_hashes,
    plan_state,
    plans_dir,
    statements,
)
from de_agent.sqlclass import classify
from typer.testing import CliRunner

CFG = """version: 1
sources:
  - name: wh
    type: postgres
    conn: {host: localhost, port: 5432, dbname: d, user: ro, password: pgpass}
  - name: wh_rw
    type: postgres
    writable: true
    conn: {host: localhost, port: 5432, dbname: d, user: rw, password: pgpass}
"""

PLAN = """---
id: fix-one
title: Restore the renamed column
risk: medium
sources: [wh_rw, wh]
statement_timeout: 120s
---
## Why
The column was renamed. Evidence: `column "customer_id" does not exist`.
```sql source=wh_rw
drop table illustrative_only;
```
## Steps
### 1. Rename it back
```sql source=wh_rw
ALTER TABLE public.orders RENAME COLUMN cust_id TO customer_id;
```
```sql verify source=wh expect="= 1"
SELECT count(*) FROM information_schema.columns WHERE table_name='orders' AND column_name='customer_id';
```
### 2. Index it
```sql source=wh_rw transaction: false
CREATE INDEX CONCURRENTLY IF NOT EXISTS orders_cid ON public.orders (customer_id);
```
```sql verify source=wh expect="between 1 and 5"
select 3;
```
## Rollback
```sql source=wh_rw
ALTER TABLE public.orders RENAME COLUMN customer_id TO cust_id;
```
## Not doing
Dropping the table.
```sql source=wh_rw
truncate raw.orders;
```
"""

KEY = b"unit-test-key"


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    h = tmp_path / "hub"
    (h / ".dea" / "plans").mkdir(parents=True)
    (h / "de-agent.yaml").write_text(CFG)
    (h / ".dea" / "plans" / "fix-one.md").write_text(PLAN)
    return h


def cfg_of(hub):
    return load_config(hub / "de-agent.yaml")


def approve_plan(hub, plan_id="fix-one", key=KEY):
    plan = load_plan(hub, plan_id)
    kp = Path(approval.key_path())
    kp.parent.mkdir(parents=True, exist_ok=True)
    kp.write_bytes(key + b"\n")
    data = approval.make_approval(plan.id, plan_hashes(plan), "tester@example.com", "2026-10-27T00:00:00Z", key)
    approval.write_approval(str(plans_dir(hub) / f"{plan.id}.approved"), data)
    return data


# ---- parser

def test_parse_fences_verify_transaction_rollback():
    p = parse_plan(PLAN)
    assert (p.id, p.risk, p.sources, p.statement_timeout_s) == ("fix-one", "medium", ["wh_rw", "wh"], 120)
    assert [s.n for s in p.steps] == [1, 2] and p.steps[0].title == "Rename it back"
    b = p.steps[0].blocks
    assert [(x.verify, x.source, x.expect, x.transaction) for x in b] == [
        (False, "wh_rw", None, True), (True, "wh", "= 1", True)]
    assert p.steps[1].blocks[0].transaction is False  # `transaction: false` form
    assert p.steps[1].blocks[1].expect == "between 1 and 5"
    assert len(p.rollback) == 1 and p.rollback[0].sql.startswith("ALTER TABLE")
    # SQL in Why / Not doing is illustrative and never collected
    assert all("illustrative_only" not in x.sql and "truncate" not in x.sql.lower()
               for x in [*b, *p.steps[1].blocks, *p.rollback])
    assert p.errors == []


def test_parse_transaction_equals_form_and_unnumbered_step():
    p = parse_plan("---\nid: a\ntitle: t\nrisk: low\nsources: [s]\n---\n## Steps\n### Do it\n"
                   "```sql source=s transaction=false\nvacuum t;\n```\n")
    assert p.steps[0].n == 1 and p.steps[0].title == "Do it" and p.steps[0].blocks[0].transaction is False


def test_parse_reports_bad_fences():
    p = parse_plan("---\nid: a\ntitle: t\nrisk: low\nsources: [s]\n---\n## Steps\n"
                   "```sql source=s\nselect 1;\n```\n### 1. x\n```sql source=s bogus=1 expect=\"=1\"\nselect 1;\n```\n")
    joined = "\n".join(p.errors)
    assert "outside a numbered step" in joined and "unknown fence attribute" in joined and "expect= is only valid" in joined


# ---- check

def test_check_ok_lists_statements_and_hashes(hub):
    plan = load_plan(hub, "fix-one")
    rep = check_plan(plan, cfg_of(hub), "fix-one")
    assert rep.ok, rep.errors
    assert rep.sources == ["wh_rw", "wh"]
    kinds = [(r.where, r.kind, r.type, r.verify) for r in rep.rows]
    assert ("step 1", "write", "ALTER", False) in kinds and ("step 1", "read", "SELECT", True) in kinds
    assert len(rep.hashes) == len(set(rep.hashes)) == 5


def test_check_errors(hub):
    cfg = cfg_of(hub)

    def errs(text):
        return check_plan(parse_plan(text), cfg).errors

    head = "---\nid: x\ntitle: t\nrisk: {risk}\nsources: [wh_rw, wh]\n---\n## Steps\n### 1. s\n"
    # verify that classifies as a write
    e = errs(head.format(risk="medium") + "```sql verify source=wh expect=\"= 0\"\ndelete from t where id=1;\n```\n")
    assert any("verify block must be read-only" in x for x in e)
    # unknown source, missing source
    assert any("unknown source 'nope'" in x for x in errs(head.format(risk="medium") + "```sql source=nope\nselect 1;\n```\n"))
    assert any("no source=" in x for x in errs(head.format(risk="medium") + "```sql\nselect 1;\n```\n"))
    # destructive in a low-risk plan
    for sql in ("drop table t", "truncate t", "delete from t"):
        assert any("destructive" in x for x in errs(head.format(risk="low") + f"```sql source=wh_rw\n{sql};\n```\n")), sql
    assert not any("destructive" in x for x in errs(head.format(risk="low") + "```sql source=wh_rw\ndelete from t where id = 1;\n```\n"))
    assert not any("destructive" in x for x in errs(head.format(risk="high") + "```sql source=wh_rw\ndrop table t;\n```\n"))
    # write against a source that is not writable
    assert any("not `writable: true`" in x for x in errs(head.format(risk="medium") + "```sql source=wh\nupdate t set a = 1;\n```\n"))
    # unparseable and meta-commands
    assert errs(head.format(risk="medium") + "```sql source=wh_rw\nthis is not sql at all (\n```\n")
    assert any("meta-commands" in x for x in errs(head.format(risk="medium") + "```sql source=wh_rw\n\\dt\n```\n"))
    # bad expect, bad risk, id mismatch handled by cli; frontmatter checks
    assert any("unsupported expect" in x for x in errs(
        head.format(risk="medium") + "```sql verify source=wh expect=\"roughly 3\"\nselect 1;\n```\n"))
    assert any("risk must be" in x for x in errs(head.format(risk="bad") + "```sql source=wh_rw\nselect 1;\n```\n"))


def test_hash_parity_with_sql_guard(hub):
    """Plan statement hashes equal what sql_guard computes from the same SQL arriving through psql -c."""
    plan = load_plan(hub, "fix-one")
    for b in [x for s in plan.steps for x in s.blocks] + plan.rollback:
        for text, st in statements(b.sql):
            via_guard = [s.hash for _src, sql in extract_sql(f'psql -c "{text}"') for s in classify(sql)]
            assert via_guard == [st.hash], text
    # whitespace and case-insensitive keyword normalisation is shared
    assert statements("DELETE   FROM t WHERE id=1")[0][1].hash == classify("delete from t where id = 1")[0].hash


def test_expect_parsing():
    cases = [("= 0", 0, True), ("= 0", 1, False), ("> 0", 3, True), ("> 0", 0, False), (">= 2", 2, True),
             ("< 5", 4, True), ("<= 5", 6, False), ("!= 3", 3, False), ("between 1 and 5", 5, True),
             ("BETWEEN 1 AND 5", 6, False), ("between -2 and 2", 0, True), ("is null", None, True),
             ("is null", 0, False), ("is not null", 0, True), ("is not null", None, False), ("= 0", None, False),
             ("= 1.5", 1.5, True), ("> 0", "7", True), ("> 0", "abc", False), ("= 0", True, False)]
    for expect, value, want in cases:
        assert eval_expect(expect, value) is want, (expect, value)
    with pytest.raises(ValueError):
        eval_expect("roughly", 1)


# ---- MAC

def test_mac_create_verify(hub):
    data = approve_plan(hub)
    assert approval.verify_data(data, KEY, "fix-one") == (True, "")
    got, why = approval.read_approval(str(plans_dir(hub) / "fix-one.approved"), KEY, "fix-one")
    assert got and not why


def test_mac_tamper_hash_list_edited_statement_and_swapped_key(hub):
    cfg = cfg_of(hub)
    approve_plan(hub)
    path = plans_dir(hub) / "fix-one.approved"
    good = json.loads(path.read_text())
    # edited hash list
    bad = dict(good, hashes=[*good["hashes"], "0" * 64])
    assert approval.verify_data(bad, KEY, "fix-one")[0] is False
    path.write_text(json.dumps(bad))
    with pytest.raises(ApplyRefused, match="bad MAC"):
        authorize(hub, cfg, load_plan(hub, "fix-one"), False, KEY)
    # edited approver / timestamp
    for field, val in (("approver", "someone-else"), ("approved_at", "2030-01-01T00:00:00Z")):
        assert approval.verify_data(dict(good, **{field: val}), KEY, "fix-one")[0] is False
    # swapped key
    path.write_text(json.dumps(good))
    with pytest.raises(ApplyRefused, match="bad MAC"):
        authorize(hub, cfg, load_plan(hub, "fix-one"), False, b"another-key")
    # no key at all
    with pytest.raises(ApplyRefused, match="no approval key"):
        authorize(hub, cfg, load_plan(hub, "fix-one"), False, None)
    # edited statement after approval: MAC is fine, but the statement hash is not in the approved set
    path.write_text(json.dumps(good))
    p = plans_dir(hub) / "fix-one.md"
    p.write_text(PLAN.replace("RENAME COLUMN cust_id TO customer_id;\n```\n```sql verify",
                              "RENAME COLUMN cust_id TO customer_id2;\n```\n```sql verify"))
    with pytest.raises(ApplyRefused, match="not in the approved set"):
        authorize(hub, cfg, load_plan(hub, "fix-one"), False, KEY)


def test_not_approved_is_refused(hub):
    with pytest.raises(ApplyRefused, match="dea approve fix-one"):
        authorize(hub, cfg_of(hub), load_plan(hub, "fix-one"), False, KEY)


def test_key_created_0600_and_xdg_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert approval.load_key() is None
    k = approval.load_key(create=True)
    p = tmp_path / "xdg" / "de-agent" / "approve.key"
    assert p.is_file() and (p.stat().st_mode & 0o777) == 0o600 and approval.load_key() == k
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert approval.key_path() == str(tmp_path / "home" / ".config" / "de-agent" / "approve.key")


# ---- apply

class Fake:
    def __init__(self, fail_on=None, verify_values=None):
        self.calls = []
        self.fail_on = fail_on
        self.verify_values = list(verify_values or [])

    def write(self, conn, texts, exec_target, timeout, transaction):
        self.calls.append(("write", conn.user, list(texts), timeout, transaction))
        if self.fail_on and any(self.fail_on in t for t in texts):
            raise CollectorError("boom")
        return [3 for _ in texts]

    def read(self, conn, sql, exec_target, timeout):
        self.calls.append(("read", conn.user, sql, timeout))
        return [{"v": self.verify_values.pop(0) if self.verify_values else 1}]


def test_apply_runs_in_order_with_verifies_and_writes_applied_json(hub):
    approve_plan(hub)
    f = Fake(verify_values=[1, 3])
    res = apply_plan(hub, cfg_of(hub), load_plan(hub, "fix-one"), key=KEY, write_fn=f.write, read_fn=f.read)
    assert res.status == "ok"
    kinds = [c[0] for c in f.calls]
    assert kinds == ["write", "read", "write", "read"]  # step 1 statements, step 1 verify, step 2, step 2 verify
    assert f.calls[0][1] == "rw" and f.calls[1][1] == "ro" and f.calls[0][3] == 120.0
    assert f.calls[0][4] is True and f.calls[2][4] is False  # transaction flag honoured
    rec = json.loads((plans_dir(hub) / "fix-one.applied.json").read_text())
    assert rec["status"] == "ok" and [s["status"] for s in rec["steps"]] == ["ok", "ok"]
    assert rec["steps"][0]["rowcount"] == 3 and rec["steps"][0]["verify"][0]["ok"] is True
    assert rec["approved_by"] == "tester@example.com" and rec["steps"][1]["duration_s"] is not None
    assert plan_state(hub, "fix-one", KEY) == "applied"


def test_apply_stops_on_statement_error(hub):
    approve_plan(hub)
    f = Fake(fail_on="CONCURRENTLY", verify_values=[1])
    res = apply_plan(hub, cfg_of(hub), load_plan(hub, "fix-one"), key=KEY, write_fn=f.write, read_fn=f.read)
    assert res.status == "failed"
    rec = json.loads((plans_dir(hub) / "fix-one.applied.json").read_text())
    assert [s["status"] for s in rec["steps"]] == ["ok", "failed"] and rec["steps"][1]["error"] == "boom"
    assert plan_state(hub, "fix-one", KEY) == "failed"


def test_apply_stops_on_failed_verify_and_skips_later_steps(hub):
    approve_plan(hub)
    f = Fake(verify_values=[0])  # step 1 verify expects = 1
    res = apply_plan(hub, cfg_of(hub), load_plan(hub, "fix-one"), key=KEY, write_fn=f.write, read_fn=f.read)
    assert res.status == "failed"
    rec = json.loads((plans_dir(hub) / "fix-one.applied.json").read_text())
    assert [s["status"] for s in rec["steps"]] == ["failed", "skipped"]
    assert "verify failed" in rec["steps"][0]["error"] and rec["steps"][0]["verify"][0]["ok"] is False
    assert [c[0] for c in f.calls] == ["write", "read"]  # nothing from step 2 ran


def test_apply_refuses_unapproved_and_never_calls_executor(hub):
    f = Fake()
    with pytest.raises(ApplyRefused):
        apply_plan(hub, cfg_of(hub), load_plan(hub, "fix-one"), key=KEY, write_fn=f.write, read_fn=f.read)
    assert f.calls == []


def test_apply_refuses_second_run_and_rollback_uses_rollback_section(hub):
    approve_plan(hub)
    f = Fake(verify_values=[1, 3])
    apply_plan(hub, cfg_of(hub), load_plan(hub, "fix-one"), key=KEY, write_fn=f.write, read_fn=f.read)
    with pytest.raises(ApplyRefused, match="already applied"):
        apply_plan(hub, cfg_of(hub), load_plan(hub, "fix-one"), key=KEY, write_fn=f.write, read_fn=f.read)
    f.calls.clear()
    res = apply_plan(hub, cfg_of(hub), load_plan(hub, "fix-one"), rollback=True, key=KEY, write_fn=f.write,
                     read_fn=f.read)
    assert res.mode == "rollback" and res.status == "ok"
    assert len(f.calls) == 1 and "customer_id TO cust_id" in f.calls[0][2][0]
    assert plan_state(hub, "fix-one", KEY) == "rolled back"


def test_rollback_statements_must_be_approved_too(hub):
    approve_plan(hub)
    path = plans_dir(hub) / "fix-one.md"
    path.write_text(PLAN.replace("TO cust_id;\n```\n## Not doing", "TO cust_id_x;\n```\n## Not doing"))
    with pytest.raises(ApplyRefused, match="not in the approved set"):
        apply_plan(hub, cfg_of(hub), load_plan(hub, "fix-one"), rollback=True, key=KEY, write_fn=Fake().write)


def test_plan_states(hub):
    assert plan_state(hub, "fix-one", KEY) == "draft"
    r = CliRunner().invoke(app, ["plan", "check", "fix-one"], env={"DEA_HUB": str(hub)})
    assert r.exit_code == 0, r.output
    assert plan_state(hub, "fix-one", KEY) == "checked"
    approve_plan(hub)
    assert plan_state(hub, "fix-one", KEY) == "approved"
    assert plan_state(hub, "fix-one", b"other") == "checked"  # bad MAC grants nothing
    out = CliRunner().invoke(app, ["plans"], env={"DEA_HUB": str(hub), "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"]})
    assert "fix-one" in out.output and "Restore the renamed column" in out.output


def test_cli_plan_check_exit_codes(hub):
    runner = CliRunner()
    env = {"DEA_HUB": str(hub)}
    ok = runner.invoke(app, ["plan", "check", "fix-one"], env=env)
    assert ok.exit_code == 0 and "OK: no errors" in ok.output and "ALTER" in ok.output
    (plans_dir(hub) / "bad.md").write_text(PLAN.replace("id: fix-one", "id: bad").replace("risk: medium", "risk: low")
                                           .replace("drop table illustrative_only;", "")
                                           .replace("ALTER TABLE public.orders RENAME COLUMN cust_id TO customer_id;",
                                                    "DROP TABLE public.orders;"))
    bad = runner.invoke(app, ["plan", "check", "bad"], env=env)
    assert bad.exit_code == 1 and "destructive DROP in a plan marked risk: low" in bad.output
    assert runner.invoke(app, ["plan", "check", "nope"], env=env).exit_code == 2


# ---- dea apply / approve CLI

def test_cli_apply_refuses_without_approval(hub):
    r = CliRunner().invoke(app, ["apply", "fix-one"], env={"DEA_HUB": str(hub)})
    assert r.exit_code == 2 and "dea approve fix-one" in r.output


def test_cli_apply_dry_run_prints_and_runs_nothing(hub, monkeypatch):
    approve_plan(hub)
    from de_agent import sqlexec

    monkeypatch.setattr(sqlexec, "run_write", lambda *a, **k: pytest.fail("dry run must not write"))
    r = CliRunner().invoke(app, ["apply", "fix-one", "--dry-run"], env={"DEA_HUB": str(hub)})
    assert r.exit_code == 0 and "dry run: nothing executed" in r.output and "RENAME COLUMN" in r.output
    assert not (plans_dir(hub) / "fix-one.applied.json").exists()


def test_approve_refuses_without_tty(hub):
    r = CliRunner().invoke(app, ["approve", "fix-one"], env={"DEA_HUB": str(hub)})
    assert r.exit_code == 2 and "run this in your own terminal" in r.output
    assert not (plans_dir(hub) / "fix-one.approved").exists()


def test_approve_refuses_with_piped_stdin_subprocess(hub):
    """No flag or env var can bypass the TTY requirement (including piping the id in)."""
    env = dict(os.environ, DEA_HUB=str(hub), DEA_APPROVE_YES="1", YES="1")
    p = subprocess.run([sys.executable, "-c", "from de_agent.cli import app; app()", "approve", "fix-one", "--yes"],
                       input="fix-one\n", capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 2
    p = subprocess.run([sys.executable, "-c", "from de_agent.cli import app; app()", "approve", "fix-one"],
                       input="fix-one\n", capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 2 and "run this in your own terminal" in p.stderr
    assert not (plans_dir(hub) / "fix-one.approved").exists()


def _pty_run(argv, env, cwd, answer, timeout=60):
    import pty

    master, slave = pty.openpty()
    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd, close_fds=True)
    os.close(slave)
    buf = b""
    sent = False
    deadline = time.time() + timeout
    while time.time() < deadline:
        r, _, _ = select.select([master], [], [], 0.5)
        if r:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            if not sent and b"to approve it" in buf:
                os.write(master, (answer + "\n").encode())
                sent = True
        elif proc.poll() is not None:
            break
    proc.wait(timeout=10)
    os.close(master)
    return proc.returncode, buf.decode(errors="replace")


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pty")
def test_approve_through_pty_writes_signed_file(hub, tmp_path):
    env = dict(os.environ, DEA_HUB=str(hub), XDG_CONFIG_HOME=str(tmp_path / "xdg"), HOME=str(tmp_path / "h"))
    argv = [sys.executable, "-c", "from de_agent.cli import app; app()", "approve", "fix-one"]
    code, out = _pty_run(argv, env, hub, "fix-one")
    assert code == 0, out
    assert "OK: no errors" in out and "approved fix-one" in out
    f = plans_dir(hub) / "fix-one.approved"
    data = json.loads(f.read_text())
    key_file = tmp_path / "xdg" / "de-agent" / "approve.key"
    assert key_file.is_file() and (key_file.stat().st_mode & 0o777) == 0o600
    key = key_file.read_bytes().strip()
    assert approval.verify_data(data, key, "fix-one") == (True, "")
    assert set(data["hashes"]) == set(plan_hashes(load_plan(hub, "fix-one"))) and data["approver"]


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pty")
def test_approve_wrong_id_cancels(hub, tmp_path):
    env = dict(os.environ, DEA_HUB=str(hub), XDG_CONFIG_HOME=str(tmp_path / "xdg"), HOME=str(tmp_path / "h"))
    argv = [sys.executable, "-c", "from de_agent.cli import app; app()", "approve", "fix-one"]
    code, out = _pty_run(argv, env, hub, "yes")
    assert code == 1 and "cancelled" in out
    assert not (plans_dir(hub) / "fix-one.approved").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pty")
def test_approve_refuses_plan_with_errors(hub, tmp_path):
    (plans_dir(hub) / "fix-one.md").write_text(PLAN.replace("risk: medium", "risk: low").replace(
        "RENAME COLUMN cust_id TO customer_id;", "RENAME COLUMN cust_id TO customer_id;\nDROP TABLE x;"))
    env = dict(os.environ, DEA_HUB=str(hub), XDG_CONFIG_HOME=str(tmp_path / "xdg"), HOME=str(tmp_path / "h"))
    argv = [sys.executable, "-c", "from de_agent.cli import app; app()", "approve", "fix-one"]
    code, out = _pty_run(argv, env, hub, "fix-one", timeout=20)
    assert code == 1 and "refusing to approve" in out and not (plans_dir(hub) / "fix-one.approved").exists()


# ---- dea query

def test_query_refuses_writes_without_connecting(hub, monkeypatch):
    from de_agent import sqlexec

    monkeypatch.setattr(sqlexec, "run_query", lambda *a, **k: pytest.fail("must not connect"))
    r = CliRunner().invoke(app, ["query", "wh", "delete from t where id = 1"], env={"DEA_HUB": str(hub)})
    assert r.exit_code == 2 and "refused: DELETE is a write" in r.output
    r = CliRunner().invoke(app, ["query", "wh", "select 1; drop table t"], env={"DEA_HUB": str(hub)})
    assert r.exit_code == 2 and "DROP" in r.output
    r = CliRunner().invoke(app, ["query", "wh", "select 1; select 2"], env={"DEA_HUB": str(hub)})
    assert r.exit_code == 2 and "exactly one statement" in r.output
    r = CliRunner().invoke(app, ["query", "wh_rw", "select 1"], env={"DEA_HUB": str(hub)})
    assert r.exit_code == 2 and "write-capable" in r.output
    r = CliRunner().invoke(app, ["query", "nope", "select 1"], env={"DEA_HUB": str(hub)})
    assert r.exit_code == 2 and "unknown source" in r.output


def test_query_row_cap_and_formats(hub, monkeypatch):
    from de_agent import sqlexec

    seen = {}

    def fake(conn, sql, exec_target, timeout):
        seen["sql"] = sql
        return [{"id": i, "name": f"n{i}", sqlexec.TOTAL_COL: 7} for i in range(3)]

    monkeypatch.setattr(sqlexec, "run_query", fake)
    env = {"DEA_HUB": str(hub)}
    r = CliRunner().invoke(app, ["query", "wh", "select id, name from t", "--limit", "3"], env=env)
    assert r.exit_code == 0 and "limit 3" in seen["sql"] and "count(*) over ()" in seen["sql"]
    assert "(4 more rows not shown; raise --limit)" in r.output and sqlexec.TOTAL_COL not in r.output
    assert "n2" in r.output and "n3" not in r.output
    j = CliRunner().invoke(app, ["query", "wh", "select id from t", "--limit", "3", "--format", "json"], env=env)
    data = json.loads(j.output)
    assert len(data["rows"]) == 3 and data["total"] == 7 and data["more_rows"] == 4
    c = CliRunner().invoke(app, ["query", "wh", "select id from t", "--limit", "3", "--format", "csv"], env=env)
    assert c.output.splitlines()[0] == "id,name" and len(c.stdout.splitlines()) >= 4


def test_query_limited_default_limit_is_200_and_surfaces_errors(hub, monkeypatch):
    from de_agent import sqlexec

    monkeypatch.setattr(sqlexec, "run_query", lambda *a, **k: (_ for _ in ()).throw(CollectorError("relation missing")))
    r = CliRunner().invoke(app, ["query", "wh", "select * from nope"], env={"DEA_HUB": str(hub)})
    assert r.exit_code == 1 and "query failed: relation missing" in r.output
    with pytest.raises(CollectorError, match="refused"):
        sqlexec.query_limited(cfg_of(hub).source("wh").conn, "update t set a = 1")


def test_writable_source_is_not_snapshotted(hub):
    from de_agent.snapshot import take_snapshot

    cfg = cfg_of(hub)
    seen = []

    def qf(conn, sql, *a, **k):
        seen.append(conn.user)
        raise CollectorError("no db in this test")

    out = take_snapshot(hub, cfg, query_fn=qf)
    assert {r["source"] for r in out.results} == {"wh"} and set(seen) <= {"ro"}


def test_writable_only_on_postgres():
    from de_agent.config import ConfigError, parse_config

    with pytest.raises(ConfigError, match="writable"):
        parse_config("version: 1\nsources:\n  - {name: d, type: dbt, writable: true, options: {target_dir: t}}\n")
