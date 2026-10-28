"""M2b: diff and shell plan steps (parse, check, hash, apply, rollback, verify, redaction). Fictional Acme only."""
import hashlib
import hmac
import json
import subprocess

import pytest
from de_agent import approval, stepexec
from de_agent.applyplan import ApplyRefused, apply_plan, dry_run_lines
from de_agent.config import load_config
from de_agent.plans import (
    check_plan,
    load_plan,
    parse_plan,
    plan_hashes,
    plan_state,
    plans_dir,
    render_report,
)

CFG = """version: 1
sources:
  - name: wh
    type: postgres
    conn: {host: localhost, port: 5432, dbname: d, user: ro, password: pgpass}
  - name: wh_rw
    type: postgres
    writable: true
    conn: {host: localhost, port: 5432, dbname: d, user: rw, password: pgpass}
scans:
  - {name: pipelines, type: airflow, path: repo, dag_folders: [dags]}
"""
KEY = b"unit-test-key"
DAG = "import os\nimport sys\nSELECT_SQL = 'select customer_id from orders'\n"
DAG_NEW = "import os\nimport sys\nSELECT_SQL = 'select cust_id from orders'\n"
PATCH = """--- a/dags/extract_shop.py
+++ b/dags/extract_shop.py
@@ -1,3 +1,3 @@
 import os
 import sys
-SELECT_SQL = 'select customer_id from orders'
+SELECT_SQL = 'select cust_id from orders'
"""
PATCH2 = """--- a/dags/other.py
+++ b/dags/other.py
@@ -1 +1 @@
-x = 1
+x = 2
"""


def plan_text(steps, rollback="", risk="medium", sources="[wh_rw, wh]", pid="fix-code"):
    return f"---\nid: {pid}\ntitle: Fix the code way\nrisk: {risk}\nsources: {sources}\n---\n## Why\nbecause\n```diff file=x repo=y\n" \
           f"-a\n+b\n```\n## Steps\n{steps}\n## Rollback\n{rollback}\n"


def diff_fence(patch=PATCH, file="dags/extract_shop.py", repo="pipelines"):
    return f"```diff file={file} repo={repo}\n{patch}```\n"


STEPS = ("### 1. Patch the extractor\n" + diff_fence() + "### 2. Trigger it\n"
         "```shell exec=local cwd=. timeout=30\necho triggered extract_shop\n```\n"
         "```shell verify expect_stdout~=\"hello .*\" expect_exit=0\necho hello world\n```\n")


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    h = tmp_path / "hub"
    (h / ".dea" / "plans").mkdir(parents=True)
    (h / "de-agent.yaml").write_text(CFG)
    r = h / "repo"
    (r / "dags").mkdir(parents=True)
    (r / "dags" / "extract_shop.py").write_text(DAG)
    (r / "dags" / "other.py").write_text("x = 1\n")
    git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run([*git, "commit", "-qm", "init"], cwd=r, check=True)
    return h


def cfg_of(hub):
    return load_config(hub / "de-agent.yaml")


def put(hub, text, pid="fix-code"):
    (plans_dir(hub) / f"{pid}.md").write_text(text)
    return load_plan(hub, pid)


def approve(hub, plan, key=KEY, hashes=None):
    kp = approval.key_path()
    import os

    os.makedirs(os.path.dirname(kp), exist_ok=True)
    open(kp, "wb").write(key + b"\n")
    d = approval.make_approval(plan.id, hashes if hashes is not None else plan_hashes(plan), "t@example.com",
                               "2026-10-27T00:00:00Z", key)
    approval.write_approval(str(plans_dir(hub) / f"{plan.id}.approved"), d)


def commits(hub):
    return subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=hub / "repo", capture_output=True, text=True).stdout.strip()


def rollback_sql():
    return "```sql source=wh_rw\nselect 1;\n```\n"


# ---------------------------------------------------------------- parse

def test_parse_diff_and_shell_blocks_and_ignore_illustrative_diffs():
    plan = parse_plan(plan_text(STEPS))
    assert plan.errors == []
    d, sh, ver = plan.steps[0].blocks[0], plan.steps[1].blocks[0], plan.steps[1].blocks[1]
    assert (d.kind, d.repo, d.file) == ("diff", "pipelines", "dags/extract_shop.py") and d.sql.endswith("\n")
    assert (sh.kind, sh.exec, sh.cwd, sh.timeout, sh.sql) == ("shell", "local", ".", 30, "echo triggered extract_shop")
    assert ver.verify and ver.expect_exit == 0 and ver.expect_stdout == "hello .*"
    assert len(plan.steps[0].blocks) == 1  # the ```diff in ## Why never became a step


def test_parse_reports_bad_ext_attributes():
    bad = "### 1. x\n```shell exec=carrier-pigeon timeout=soon expect_exit=x expect_stdout~=(\necho hi\n```\n"
    errs = parse_plan(plan_text(bad)).errors
    assert any("exec must be" in e for e in errs) and any("timeout" in e for e in errs)
    assert any("expect_exit" in e for e in errs) and any("regex" in e for e in errs)
    assert any("unknown fence attribute" in e for e in parse_plan(plan_text("### 1. x\n```diff nope=1\n+a\n```\n")).errors)


# ---------------------------------------------------------------- check

def test_check_ok_with_all_three_step_kinds(hub):
    steps = ("### 1. Add the column\n```sql source=wh_rw\nALTER TABLE public.orders ADD COLUMN cust_id int;\n```\n" + STEPS)
    plan = put(hub, plan_text(steps, rollback_sql()))
    rep = check_plan(plan, cfg_of(hub), "fix-code", hub=hub)
    assert rep.ok, rep.errors
    assert [r.type for r in rep.rows] == ["ALTER", "DIFF", "SHELL", "SHELL", "SELECT"]
    assert len(rep.hashes) == 5 and "DIFF" in render_report(plan, rep)


def test_check_patch_that_does_not_apply_and_file_mismatch(hub):
    bad = PATCH.replace("-SELECT_SQL = 'select customer_id", "-SELECT_SQL = 'select nothing")
    rep = check_plan(put(hub, plan_text("### 1. p\n" + diff_fence(bad))), cfg_of(hub), hub=hub)
    assert any("does not apply" in e for e in rep.errors)
    rep = check_plan(put(hub, plan_text("### 1. p\n" + diff_fence(file="dags/zzz.py"))), cfg_of(hub), hub=hub)
    assert any("one file per diff step" in e for e in rep.errors)
    rep = check_plan(put(hub, plan_text("### 1. p\n" + diff_fence(repo="nope"))), cfg_of(hub), hub=hub)
    assert any("unknown repo" in e for e in rep.errors)
    rep = check_plan(put(hub, plan_text("### 1. p\n" + diff_fence() + "### 2. q\n" + diff_fence())), cfg_of(hub), hub=hub)
    assert any("already patches" in e for e in rep.errors)
    for f in ("../x.py", "/etc/passwd"):
        rep = check_plan(put(hub, plan_text("### 1. p\n" + diff_fence(file=f))), cfg_of(hub), hub=hub)
        assert any("relative path" in e for e in rep.errors)


@pytest.mark.parametrize("cmd", [
    "echo a && echo b", "echo a | tee x", "echo a; echo b", "echo a > out.txt", "cat < in.txt", "echo $(id)", "echo `id`",
    "sleep 1 &", "echo 'unterminated"])
def test_check_refuses_chained_or_redirected_shell(hub, cmd):
    rep = check_plan(put(hub, plan_text(f"### 1. s\n```shell\n{cmd}\n```\n")), cfg_of(hub), hub=hub)
    assert not rep.ok and any("refused" in e or "unbalanced" in e for e in rep.errors), rep.errors


def test_check_refuses_multi_line_shell(hub):
    rep = check_plan(put(hub, plan_text("### 1. s\n```shell\necho a\necho b\n```\n")), cfg_of(hub), hub=hub)
    assert any("single-line" in e for e in rep.errors)


def test_metacharacters_inside_quotes_are_plain_arguments(hub):
    cmd = """python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" dags/extract_shop.py"""
    assert stepexec.shell_problems(cmd) == []
    rep = check_plan(put(hub, plan_text(f"### 1. s\n```shell verify\n{cmd}\n```\n")), cfg_of(hub), hub=hub)
    assert rep.ok, rep.errors


@pytest.mark.parametrize("cmd", [
    "rm -rf build", "dbt run --full-refresh", "airflow dags delete extract_shop", "airflow db reset -y",
    "airflow tasks clear extract_shop", "aws s3api delete-bucket --bucket b", "aws ec2 terminate-instances --instance-ids i-1",
    "psql -c \"drop table t\"", "psql -c \"truncate raw.orders\"", "aws s3 rm s3://b/k"])
def test_destructive_shell_is_flagged_and_errors_under_risk_low(hub, cmd):
    assert stepexec.shell_destructive(cmd)
    text = f"### 1. s\n```shell\n{cmd}\n```\n"
    rep = check_plan(put(hub, plan_text(text, risk="medium")), cfg_of(hub), hub=hub)
    assert rep.ok and rep.rows[0].destructive
    rep = check_plan(put(hub, plan_text(text, risk="low")), cfg_of(hub), hub=hub)
    assert any("destructive command" in e and "risk: low" in e for e in rep.errors)


def test_safe_commands_are_not_destructive():
    for cmd in ("airflow dags backfill extract_shop -s 2026-10-01 -e 2026-10-03", "dbt build --select stg_orders",
                "docker compose exec -T airflow airflow dags trigger extract_shop", "aws s3 ls s3://b/"):
        assert stepexec.shell_destructive(cmd) is None


def test_plan_with_only_code_steps_needs_no_sources(hub):
    plan = put(hub, plan_text("### 1. p\n" + diff_fence(), sources="[]"))
    assert check_plan(plan, cfg_of(hub), hub=hub).ok
    plan = put(hub, plan_text("### 1. q\n```sql source=wh_rw\nselect 1;\n```\n", sources="[]"))
    assert any("sources" in e for e in check_plan(plan, cfg_of(hub), hub=hub).errors)


def test_already_applied_patch_is_a_warning_not_an_error(hub):
    (hub / "repo" / "dags" / "extract_shop.py").write_text(DAG_NEW)
    rep = check_plan(put(hub, plan_text("### 1. p\n" + diff_fence())), cfg_of(hub), hub=hub)
    assert rep.ok and any("already applied" in w for w in rep.warnings)


# ---------------------------------------------------------------- hashing

def test_diff_hash_is_over_the_exact_patch():
    crlf = PATCH.replace("\n", "\r\n")
    messy = PATCH.replace("\n", "  \n")
    assert stepexec.normalize_patch(messy) == stepexec.normalize_patch(PATCH) and "  " not in stepexec.normalize_patch(messy)
    assert stepexec.diff_hash("pipelines", crlf) == stepexec.diff_hash("pipelines", PATCH)  # CRLF -> LF only
    assert stepexec.diff_hash("pipelines", messy) != stepexec.diff_hash("pipelines", PATCH)  # trailing whitespace counts
    assert stepexec.diff_hash("pipelines", PATCH) != stepexec.diff_hash("other", PATCH)
    assert stepexec.diff_hash("pipelines", PATCH) != stepexec.diff_hash("pipelines", PATCH.replace("cust_id", "cust_ix"))
    assert stepexec.shell_hash("local", None, "echo hi") != stepexec.shell_hash("ssh:box", None, "echo hi")
    assert stepexec.shell_hash("local", None, "echo hi") != stepexec.shell_hash("local", "sub", "echo hi")


def test_sql_hashes_are_unchanged_by_the_new_kinds(hub):
    from de_agent.plans import statements
    from de_agent.sqlclass import classify

    plan = parse_plan(plan_text("### 1. s\n```sql source=wh_rw\nUPDATE t SET a = 1 WHERE b = 2;\n```\n" + diff_fence()))
    sql_hash = plan_hashes(plan)[0]
    assert sql_hash == classify("UPDATE t SET a = 1 WHERE b = 2;", "postgres")[0].hash == statements(plan.steps[0].blocks[0].sql)[0][1].hash


def test_old_sql_only_approvals_still_verify_and_new_hashes_are_covered_by_the_mac():
    # the canonical MAC body is unchanged: an approval signed before M2b (SQL hashes only) verifies as before
    old = {"plan_id": "p", "hashes": ["a" * 64], "approver": "t", "approved_at": "2026-10-01T00:00:00Z"}
    body = json.dumps(old, sort_keys=True, separators=(",", ":")).encode()
    old["mac"] = hmac.new(KEY, body, hashlib.sha256).hexdigest()
    assert approval.verify_data(old, KEY, "p") == (True, "")
    new = approval.make_approval("p", ["a" * 64, stepexec.diff_hash("r", PATCH), stepexec.shell_hash("local", None, "echo hi")],
                                 "t", "2026-10-27T00:00:00Z", KEY)
    assert approval.verify_data(new, KEY, "p")[0]
    for tamper in (lambda d: d["hashes"].pop(), lambda d: d["hashes"].append("b" * 64)):
        t = json.loads(json.dumps(new))
        tamper(t)
        assert not approval.verify_data(t, KEY, "p")[0]


# ---------------------------------------------------------------- apply

class Fake:
    def __init__(self):
        self.calls = []

    def write(self, conn, texts, exec_target, timeout, transaction=True):
        self.calls.append(("write", texts))
        return [1 for _ in texts]

    def read(self, conn, sql, exec_target, timeout):
        self.calls.append(("read", sql))
        return [{"v": 1}]


def test_apply_orders_sql_diff_shell_and_records_diff_stat_without_committing(hub):
    steps = ("### 1. Add\n```sql source=wh_rw\nALTER TABLE public.orders ADD COLUMN cust_id int;\n```\n"
             "```sql verify source=wh expect=\"= 1\"\nselect 1;\n```\n" + diff_fence().replace("### 1", "") +
             "```shell\necho between\n```\n### 2. Trigger\n```shell\necho after\n```\n")
    plan = put(hub, plan_text(steps, rollback_sql()))
    approve(hub, plan)
    before = commits(hub)
    f = Fake()
    res = apply_plan(hub, cfg_of(hub), plan, key=KEY, write_fn=f.write, read_fn=f.read)
    assert res.status == "ok", res.steps
    assert [c[0] for c in f.calls] == ["write", "read"]
    assert (hub / "repo" / "dags" / "extract_shop.py").read_text() == DAG_NEW
    assert commits(hub) == before  # never commits
    rec = json.loads((plans_dir(hub) / "fix-code.applied.json").read_text())
    acts = rec["steps"][0]["actions"]
    assert [a["kind"] for a in acts] == ["diff", "shell"]  # plan order within the step: sql, diff, shell
    assert "extract_shop.py" in acts[0]["diff_stat"] and acts[0]["mode"] == "applied"
    assert acts[1]["exit_code"] == 0 and acts[1]["stdout_tail"] == "between"
    assert rec["steps"][1]["actions"][0]["stdout_tail"] == "after"
    assert plan_state(hub, "fix-code", KEY) == "applied"
    st = subprocess.run(["git", "status", "--porcelain"], cwd=hub / "repo", capture_output=True, text=True).stdout
    assert st.strip() == "M dags/extract_shop.py"


def test_rollback_reverse_applies_diffs_newest_first_then_runs_the_rollback_section(hub):
    steps = "### 1. a\n" + diff_fence() + "### 2. b\n" + diff_fence(PATCH2, "dags/other.py")
    plan = put(hub, plan_text(steps, "```shell\necho undone\n```\n"))
    approve(hub, plan)
    apply_plan(hub, cfg_of(hub), plan, key=KEY)
    assert (hub / "repo" / "dags" / "other.py").read_text() == "x = 2\n"
    assert dry_run_lines(plan, True)[0].startswith("== revert step 2")
    res = apply_plan(hub, cfg_of(hub), plan, rollback=True, key=KEY)
    assert res.status == "ok" and [s["title"][:13] for s in res.steps] == ["revert step 2", "revert step 1", "rollback"]
    assert (hub / "repo" / "dags" / "extract_shop.py").read_text() == DAG
    assert (hub / "repo" / "dags" / "other.py").read_text() == "x = 1\n"
    assert res.steps[2]["actions"][0]["stdout_tail"] == "undone"
    st = subprocess.run(["git", "status", "--porcelain"], cwd=hub / "repo", capture_output=True, text=True).stdout
    assert st.strip() == ""  # git clean again
    assert plan_state(hub, "fix-code", KEY) == "rolled back"


def test_rollback_after_a_failed_run_only_reverts_what_was_applied(hub):
    steps = "### 1. a\n" + diff_fence() + "### 2. fails\n```shell\nfalse\n```\n### 3. b\n" + diff_fence(PATCH2, "dags/other.py")
    plan = put(hub, plan_text(steps))
    approve(hub, plan)
    res = apply_plan(hub, cfg_of(hub), plan, key=KEY)
    assert res.status == "failed" and [s["status"] for s in res.steps] == ["ok", "failed", "skipped"]
    assert "exited 1" in res.steps[1]["error"]
    rb = apply_plan(hub, cfg_of(hub), plan, rollback=True, key=KEY)
    assert rb.status == "ok" and len(rb.steps) == 1  # only step 1's diff
    assert (hub / "repo" / "dags" / "extract_shop.py").read_text() == DAG


def test_edited_diff_or_command_after_approval_is_refused_and_nothing_runs(hub):
    plan = put(hub, plan_text("### 1. a\n" + diff_fence() + "```shell\necho hi\n```\n"))
    approve(hub, plan)
    put(hub, plan_text("### 1. a\n" + diff_fence() + "```shell\necho hi there\n```\n"))
    with pytest.raises(ApplyRefused, match="not in the approved set"):
        apply_plan(hub, cfg_of(hub), load_plan(hub, "fix-code"), key=KEY)
    put(hub, plan_text("### 1. a\n" + diff_fence(PATCH.replace("cust_id", "cust_ix")) + "```shell\necho hi\n```\n"))
    with pytest.raises(ApplyRefused):
        apply_plan(hub, cfg_of(hub), load_plan(hub, "fix-code"), key=KEY)
    assert (hub / "repo" / "dags" / "extract_shop.py").read_text() == DAG


def test_sql_only_approval_does_not_cover_a_shell_step(hub):
    plan = put(hub, plan_text("### 1. s\n```sql source=wh_rw\nselect 1;\n```\n```shell\necho hi\n```\n"))
    sql_only = [h for h in plan_hashes(plan)][:1]
    approve(hub, plan, hashes=sql_only)
    assert plan_state(hub, "fix-code", KEY) != "approved"
    with pytest.raises(ApplyRefused, match="not in the approved set"):
        apply_plan(hub, cfg_of(hub), plan, key=KEY, write_fn=Fake().write)


def test_cwd_and_exec_changes_break_the_approval(hub):
    plan = put(hub, plan_text("### 1. s\n```shell cwd=.\necho hi\n```\n"))
    approve(hub, plan)
    put(hub, plan_text("### 1. s\n```shell cwd=repo\necho hi\n```\n"))
    with pytest.raises(ApplyRefused):
        apply_plan(hub, cfg_of(hub), load_plan(hub, "fix-code"), key=KEY)


# ---------------------------------------------------------------- shell verify, redaction, exec

def _shell_plan(hub, body, step_fence):
    plan = put(hub, plan_text(f"### 1. s\n{step_fence}{body}"))
    approve(hub, plan)
    return plan


@pytest.mark.parametrize("fence,cmd,ok", [
    ("```shell verify expect_exit=0", "true", True),
    ("```shell verify expect_exit=1", "false", True),
    ("```shell verify expect_exit=0", "false", False),
    ("```shell verify", "true", True),
    ("```shell verify", "false", False),
    ("```shell verify expect_stdout~=^rows=[0-9]+$", "echo rows=42", True),
    ("```shell verify expect_stdout~=^rows=[0-9]+$", "echo rows=none", False),
    ("```shell verify expect_stdout~=ok expect_exit=0", "echo ok", True),
])
def test_shell_verify_expectations(hub, fence, cmd, ok):
    plan = _shell_plan(hub, f"{cmd}\n```\n", fence + "\n")
    res = apply_plan(hub, cfg_of(hub), plan, key=KEY)
    assert (res.status == "ok") is ok
    v = res.steps[0]["verify"][0]
    assert v["ok"] is ok and v["source"] == "shell:local"
    if not ok:
        assert "verify failed" in res.steps[0]["error"]


def test_shell_output_is_tailed_and_redacted(hub):
    script = hub / "noisy.sh"
    lines = "\n".join(f"line {i}" for i in range(100))
    script.write_text(f"#!/bin/sh\ncat <<'EOT'\n{lines}\npassword=hunter2 postgres://acme:s3cr3t@db.example.internal/shop\n"
                      "token=abc123 Authorization: Bearer zzz AKIAABCDEFGHIJKLMNOP\nEOT\necho 'secret=oops' >&2\nexit 0\n")
    plan = _shell_plan(hub, "sh noisy.sh\n```\n", "```shell cwd=.\n")
    res = apply_plan(hub, cfg_of(hub), plan, key=KEY)
    a = res.steps[0]["actions"][0]
    assert res.status == "ok" and len(a["stdout_tail"].splitlines()) == stepexec.TAIL_LINES
    blob = json.dumps(a)
    for leak in ("hunter2", "s3cr3t", "abc123", "zzz", "AKIAABCDEFGHIJKLMNOP", "oops"):
        assert leak not in blob, leak
    assert "password=***" in a["stdout_tail"] and "line 99" in a["stdout_tail"] and "line 0\n" not in a["stdout_tail"]


def test_shell_runs_without_a_shell_so_metacharacters_in_quotes_stay_literal(hub):
    plan = _shell_plan(hub, 'echo "a;b|c>d"\n```\n', "```shell\n")
    res = apply_plan(hub, cfg_of(hub), plan, key=KEY)
    assert res.steps[0]["actions"][0]["stdout_tail"] == "a;b|c>d"


def test_shell_timeout_fails_the_step(hub):
    plan = _shell_plan(hub, "sleep 5\n```\n", "```shell timeout=1\n")
    res = apply_plan(hub, cfg_of(hub), plan, key=KEY)
    assert res.status == "failed" and "timed out" in res.steps[0]["error"]


def test_ssh_argv_quotes_each_argument_after_double_dash():
    argv = stepexec.shell_argv("airflow dags backfill 'my dag' -s 2026-10-01", "ssh:acme-box", None)
    assert argv[-7:] == ["--", "airflow", "dags", "backfill", "'my dag'", "-s", "2026-10-01"]
    assert argv[argv.index("acme-box") + 1] == "--" and argv[0] == "ssh"
    with_cwd = stepexec.shell_argv("ls -l", "ssh:acme-box", "/srv/app dir")
    assert with_cwd[with_cwd.index("--") + 1:] == ["cd", "'/srv/app dir'", "&&", "ls", "-l"]
    assert stepexec.shell_argv("echo 'a b'", "local", None) == ["echo", "a b"]


def test_dry_run_lists_diff_and_shell_units(hub):
    plan = put(hub, plan_text(STEPS))
    lines = "\n".join(dry_run_lines(plan, False))
    assert "git apply pipelines/dags/extract_shop.py" in lines and "apply shell on local" in lines
    assert "verify shell on local" in lines


def test_secrets_in_diffs_and_commands_are_refused(hub):
    leaky = PATCH.replace("cust_id from orders'", "cust_id from orders'\n+URI = 'postgres://acme:hunter2@db.example.internal/shop'")
    leaky = leaky.replace("@@ -1,3 +1,3 @@", "@@ -1,3 +1,4 @@")
    rep = check_plan(put(hub, plan_text("### 1. p\n" + diff_fence(leaky))), cfg_of(hub), hub=hub)
    assert any("contains a secret" in e for e in rep.errors)
    rep = check_plan(put(hub, plan_text("### 1. s\n```shell\npsql postgres://acme:hunter2@db.example.internal/shop\n```\n")),
                     cfg_of(hub), hub=hub)
    assert any("contains a secret" in e for e in rep.errors)


# ---------------------------------------------------------------- M2b.1: hash normalized, apply exact

def test_removed_line_with_trailing_whitespace_still_applies_and_rolls_back(hub):
    repo = hub / "repo"
    (repo / "dags" / "other.py").write_text("x = 1   \ny = 2\n")  # the removed line ends in spaces
    git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-qm", "trailing ws"], cwd=repo, check=True)
    patch = "--- a/dags/other.py\n+++ b/dags/other.py\n@@ -1,2 +1,2 @@\n-x = 1   \n+x = 3\n y = 2\n"
    text = plan_text("### 1. p\n" + diff_fence(patch, "dags/other.py"))
    # the plan file may carry CRLF endings (read_text turns them into LF); trailing spaces must survive
    (plans_dir(hub) / "fix-code.md").write_bytes(text.replace("\n", "\r\n").encode())
    plan = load_plan(hub, "fix-code")
    assert "-x = 1   \n" in plan.steps[0].blocks[0].sql  # exact text kept
    rep = check_plan(plan, cfg_of(hub), hub=hub)
    assert rep.ok and rep.warnings == ["no Rollback section"], (rep.errors, rep.warnings)
    # the hash is over the exact patch git apply receives (trailing spaces included)
    assert plan_hashes(plan)[0] == stepexec.diff_hash("pipelines", patch)
    approve(hub, plan)
    res = apply_plan(hub, cfg_of(hub), plan, key=KEY)
    assert res.status == "ok", res.steps
    assert (repo / "dags" / "other.py").read_text() == "x = 3\ny = 2\n"
    rb = apply_plan(hub, cfg_of(hub), plan, rollback=True, key=KEY)
    assert rb.status == "ok", rb.steps
    assert (repo / "dags" / "other.py").read_text() == "x = 1   \ny = 2\n"
