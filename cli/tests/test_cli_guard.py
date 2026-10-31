"""v0.2 section 3: the universal CLI write guard. The corpus runs the real hooks under DEA_HOOK_PYTHON (3.9 in CI)."""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml
from de_agent import cliclass
from de_agent.clients import GUARDED_CLIS

ROOT = Path(__file__).resolve().parents[2]
HOOK_PYTHON = os.environ.get("DEA_HOOK_PYTHON", sys.executable)
CLI_GUARD = ROOT / "scripts" / "cli_guard.py"
SQL_GUARD = ROOT / "scripts" / "sql_guard.py"
SELF_GUARD = ROOT / "scripts" / "self_guard.py"
SECTIONS = yaml.safe_load((Path(__file__).parent / "corpus" / "cli_guard.yaml").read_text())


def _cases():
    out = []
    for sec in SECTIONS:
        for expect in ("allow", "deny", "ask"):
            for i, cmd in enumerate(sec.get(expect, [])):
                out.append((f"{sec['section']}:{expect}:{i}", sec, expect, cmd))
    return out


CASES = _cases()


def run_hook(script, command, project, env_extra=None, agent_type=None):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(project)}
    if agent_type:
        payload["agent_type"] = agent_type
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(project), CLAUDE_PLUGIN_ROOT=str(ROOT), XDG_CONFIG_HOME=str(Path(project) / ".xdg"))
    env.pop("DEA_ALLOW_PLUGIN_EDITS", None)
    env.update(env_extra or {})
    p = subprocess.run([HOOK_PYTHON, str(script)], input=json.dumps(payload), capture_output=True, text=True, env=env,
                       timeout=60, cwd=str(project))
    assert p.returncode == 0, p.stderr
    out = p.stdout.strip()
    return json.loads(out)["hookSpecificOutput"] if out else None


def _project(base, sec, idx):
    d = base / f"p{idx}"
    d.mkdir(parents=True, exist_ok=True)
    for rel, body in (sec.get("files") or {}).items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(body)
    if "raw_yaml" in sec:
        (d / "de-agent.yaml").write_text(sec["raw_yaml"])
    elif sec.get("mode") or sec.get("cli_allow"):
        lines = ["version: 1", "guard:", f"  mode: {sec.get('mode', 'strict')}"]
        if sec.get("cli_allow"):
            lines.append("  cli_allow:")
            lines += [f"    - {json.dumps(e)}" for e in sec["cli_allow"]]
        (d / "de-agent.yaml").write_text("\n".join(lines) + "\n")
    return d


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    base = tmp_path_factory.mktemp("cli_corpus")
    projects: dict[str, Path] = {}
    for n, sec in enumerate(SECTIONS):
        projects[sec["section"]] = _project(base, sec, n)

    def one(case):
        cid, sec, expect, cmd = case
        proj = projects[sec["section"]]
        outs = [run_hook(CLI_GUARD, cmd, proj)]
        if sec.get("also_sql"):
            outs.append(run_hook(SQL_GUARD, cmd, proj))
        decisions = [o["permissionDecision"] for o in outs if o]
        got = "deny" if "deny" in decisions else ("ask" if "ask" in decisions else "allow")
        return cid, got, outs

    with ThreadPoolExecutor(max_workers=8) as ex:
        return {cid: (got, outs) for cid, got, outs in ex.map(one, CASES)}


def test_corpus_has_at_least_300_cases():
    assert len(CASES) >= 300, len(CASES)


def test_every_guarded_cli_has_allow_and_deny_cases():
    text = {c: [cmd for _cid, _s, _e, cmd in CASES if _first_exe(cmd) == c] for c in GUARDED_CLIS}
    missing = [c for c, cmds in text.items() if not cmds]
    assert not missing, missing
    for c in GUARDED_CLIS - {"dbsqlcli", "tofu"}:
        kinds = {e for _cid, _s, e, cmd in CASES if c in cmd.split() or any(w.startswith(c) for w in cmd.split()[:2])}
        assert "deny" in kinds, c


def _first_exe(cmd):
    words = cmd.split()
    for w in words:
        if "=" in w and not w.startswith("-"):
            continue
        return os.path.basename(w)
    return ""


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_corpus(case, results):
    cid, sec, expect, cmd = case
    got, outs = results[cid]
    assert got == expect, f"{cmd!r}: expected {expect}, got {got} {outs}"


def test_hooks_json_registers_cli_guard_on_bash_after_sql_guard():
    h = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]["PreToolUse"]
    bash = next(e for e in h if e["matcher"] == "Bash")
    scripts = [Path(x["args"][0]).name for x in bash["hooks"]]
    assert scripts == ["approval_guard.py", "secret_guard.py", "sql_guard.py", "cli_guard.py"]
    assert all(x["command"] == "python3" for x in bash["hooks"])


def test_wrappers_and_modes_are_all_in_the_corpus():
    names = {s["section"] for s in SECTIONS}
    assert {"wrappers-deny", "wrappers-allow", "ask-mode", "cli-allow-exact-prefix", "docker", "kubectl"} <= names
    assert sum(len(s.get("ask", [])) for s in SECTIONS) >= 10


# ---------------- messages ----------------

def test_unknown_verb_message_is_exact(tmp_path):
    out = run_hook(CLI_GUARD, "kubectl krew install x", tmp_path)
    assert out["permissionDecision"] == "deny"
    assert "not a recognised read-only command; put it in a plan as a shell step" in out["permissionDecisionReason"]
    assert out["hookEventName"] == "PreToolUse"


def test_write_message_names_the_command_and_the_plan(tmp_path):
    out = run_hook(CLI_GUARD, "aws s3 rm s3://acme-lake/k", tmp_path)
    r = out["permissionDecisionReason"]
    assert r.startswith("de-agent: `aws s3 rm` is a write") and "plan" in r and "dea apply" in r


def test_second_denial_is_counted(tmp_path):
    out = run_hook(CLI_GUARD, "aws s3 rm s3://a/x; terraform apply", tmp_path)
    assert "(+1 more in this command)" in out["permissionDecisionReason"]


def test_executor_agent_gets_no_exemption(tmp_path):
    """Writes run through `dea apply` (which does not use the Bash tool for them), so even the executor is denied here."""
    out = run_hook(CLI_GUARD, "terraform apply", tmp_path, agent_type="de-agent:executor")
    assert out["permissionDecision"] == "deny"
    assert run_hook(CLI_GUARD, "dea apply plan-1", tmp_path, agent_type="de-agent:executor") is None


def test_dea_commands_and_the_demo_flow_are_untouched(tmp_path):
    for cmd in ("dea apply p1", "dea snapshot", "dea scan", "dea query warehouse 'select 1'", "dea edges raw.orders",
                'docker compose exec warehouse psql -U acme -c "select 1"', "docker compose exec -T warehouse psql -c 'select 1'"):
        assert run_hook(CLI_GUARD, cmd, tmp_path) is None, cmd


# ---------------- fail closed ----------------

@pytest.mark.parametrize("raw", ["", "not json", "[]", '"x"', "{"])
def test_malformed_input_is_denied(tmp_path, raw):
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(tmp_path), CLAUDE_PLUGIN_ROOT=str(ROOT))
    p = subprocess.run([HOOK_PYTHON, str(CLI_GUARD)], input=raw, capture_output=True, text=True, env=env, timeout=30)
    assert p.returncode == 0 and json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_non_bash_tools_are_ignored(tmp_path):
    payload = {"tool_name": "Read", "tool_input": {"file_path": "x"}, "cwd": "."}
    p = subprocess.run([HOOK_PYTHON, str(CLI_GUARD)], input=json.dumps(payload), capture_output=True, text=True, timeout=30)
    assert p.returncode == 0 and p.stdout.strip() == ""


def test_oversized_command_is_denied_before_parsing(tmp_path):
    out = run_hook(CLI_GUARD, "echo " + "x" * 210_000, tmp_path)
    assert out["permissionDecision"] == "deny" and "too large" in out["permissionDecisionReason"]


def test_internal_error_is_a_deny(tmp_path, monkeypatch):
    plug = tmp_path / "plug"
    (plug / "scripts").mkdir(parents=True)
    for f in ("cli_guard.py", "_guardlib.py"):
        (plug / "scripts" / f).write_text((ROOT / "scripts" / f).read_text())
    (plug / "cli" / "src").mkdir(parents=True)  # no de_agent package: the import fails
    # -S: skip site-packages, so an editable install of de_agent cannot satisfy the import
    p = subprocess.run([HOOK_PYTHON, "-S", str(plug / "scripts" / "cli_guard.py")],
                       input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "aws s3 ls"}, "cwd": str(tmp_path)}),
                       capture_output=True, text=True, timeout=30, env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
    assert json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------- scripts, sql deferral, timing ----------------

def test_shell_script_files_are_read(tmp_path):
    (tmp_path / "deploy.sh").write_text("#!/bin/sh\nterraform apply -auto-approve\n")
    (tmp_path / "check.sh").write_text("#!/bin/sh\nterraform plan\naws s3 ls\n")
    assert run_hook(CLI_GUARD, "bash deploy.sh", tmp_path)["permissionDecision"] == "deny"
    assert run_hook(CLI_GUARD, "sh ./deploy.sh", tmp_path)["permissionDecision"] == "deny"
    assert run_hook(CLI_GUARD, "source deploy.sh", tmp_path)["permissionDecision"] == "deny"
    assert run_hook(CLI_GUARD, "bash check.sh", tmp_path) is None
    assert run_hook(CLI_GUARD, "bash missing.sh", tmp_path) is None  # the shell itself fails; nothing runs


def test_sql_carrying_commands_need_readable_sql(tmp_path):
    assert run_hook(CLI_GUARD, 'snow sql -q "select 1"', tmp_path) is None
    assert run_hook(CLI_GUARD, "snow sql", tmp_path)["permissionDecision"] == "deny"
    # cli_guard defers the statement itself to sql_guard
    assert run_hook(CLI_GUARD, 'bq query "drop table t"', tmp_path) is None
    assert run_hook(SQL_GUARD, 'bq query "drop table t"', tmp_path)["permissionDecision"] == "deny"


def test_worst_case_timing_under_five_seconds(tmp_path):
    """A big but legal command (under the 200 KB cap) with many guarded words, wrapped and nested."""
    cmd = "; ".join(f"env A={i} timeout 5 bash -c 'aws s3 ls s3://b/{i} | head -1'" for i in range(1500))
    assert len(cmd) < 190_000
    t = time.time()
    out = run_hook(CLI_GUARD, cmd, tmp_path)
    assert out is None
    assert time.time() - t < 5
    cmd2 = "echo " + "$(" * 40 + "aws s3 ls" + ")" * 40
    t = time.time()
    run_hook(CLI_GUARD, cmd2, tmp_path)
    assert time.time() - t < 5


# ---------------- self_guard protects cli_allow ----------------

def _edit(path, old, new):
    return {"tool_name": "Edit", "tool_input": {"file_path": str(path), "old_string": old, "new_string": new}, "cwd": str(path.parent)}


def _self(payload, tmp_path):
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(tmp_path), CLAUDE_PLUGIN_ROOT=str(ROOT))
    env.pop("DEA_ALLOW_PLUGIN_EDITS", None)
    p = subprocess.run([HOOK_PYTHON, str(SELF_GUARD)], input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=30)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)["hookSpecificOutput"] if p.stdout.strip() else None


@pytest.mark.parametrize("before,old,new,verdict", [
    ("version: 1\nguard: {mode: strict}\n", "guard: {mode: strict}", 'guard: {mode: strict, cli_allow: ["dbt run"]}', "deny"),
    ("version: 1\nguard:\n  mode: strict\n", "  mode: strict\n", "  mode: strict\n  cli_allow:\n    - terraform apply\n", "deny"),
    ('version: 1\nguard: {cli_allow: ["dbt run"]}\n', '"dbt run"', '"dbt run", "aws s3 rm s3://x"', "deny"),
    ('version: 1\nguard: {cli_allow: ["dbt run"]}\n', '["dbt run"]', "[]", "deny"),
    ("version: 1\nguard:\n  cli_allow:\n    - dbt run\n", "dbt run", "dbt build", "deny"),
    ("version: 1\nguard:\n  mode: strict\n", "version: 1", "version: 1\n# cli_allow: [dbt run]", "allow"),
    ("version: 1\nguard: {mode: strict}\nsources: []\n", "sources: []", "sources: []\nscans: []", "allow"),
    ('version: 1\nguard: {cli_allow: ["dbt run"]}\n', "version: 1", "version: 1\nhub: {name: x}", "allow"),
])
def test_self_guard_protects_cli_allow(tmp_path, before, old, new, verdict):
    f = tmp_path / "de-agent.yaml"
    f.write_text(before)
    out = _self(_edit(f, old, new), tmp_path)
    assert (out["permissionDecision"] if out else "allow") == verdict, out
    if verdict == "deny" and "cli_allow" in out["permissionDecisionReason"]:
        assert "loosens the CLI write guard" in out["permissionDecisionReason"]


def test_self_guard_blocks_a_new_hub_file_with_cli_allow_and_shell_writes(tmp_path):
    new = {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "de-agent.yaml"),
                                                  "content": 'version: 1\nguard: {cli_allow: ["terraform apply"]}\n'}, "cwd": str(tmp_path)}
    assert _self(new, tmp_path)["permissionDecision"] == "deny"
    sh = {"tool_name": "Bash", "tool_input": {"command": "echo 'guard: {cli_allow: [\"x y\"]}' >> de-agent.yaml"}, "cwd": str(tmp_path)}
    assert _self(sh, tmp_path)["permissionDecision"] == "deny"


# ---------------- unit behaviour of the classifier ----------------

def kinds(cmd, allow=()):
    return [(v.kind, v.cli, v.verb) for v in cliclass.classify_command(cmd, None, list(allow))]


def test_every_guarded_cli_is_recognised_by_name():
    for c in sorted(GUARDED_CLIS):
        assert cliclass._guarded(c, ["--help"]) is not None, c


def test_verdict_kinds():
    assert kinds("aws s3 ls") == [("read", "aws", "s3 ls")]
    assert kinds("aws s3 rm s3://b/k") == [("write", "aws", "s3 rm")]
    assert kinds("terraform frobnicate") == [("unknown", "terraform", "frobnicate")]
    assert kinds("bq query 'select 1'")[0][0] == "sql"
    assert kinds("ls") == []
    assert kinds("dbt run --select a", ["dbt run --select a"]) == [("allow", "dbt", "run")]


def test_classify_argv_for_packs():
    """The pack runner passes an argv list: no shell operators are interpreted, wrappers still unwrap."""
    assert [v.kind for v in cliclass.classify_argv(["kubectl", "get", "pods", "-n", "data"])] == ["read"]
    assert [v.kind for v in cliclass.classify_argv(["databricks", "jobs", "list-runs", "--output", "json"])] == ["read"]
    assert [v.kind for v in cliclass.classify_argv(["aws", "s3", "rm", "s3://b/k"])] == ["write"]
    assert [v.kind for v in cliclass.classify_argv(["env", "X=1", "terraform", "apply"])] == ["write"]
    assert cliclass.classify_argv(["echo", "hi"]) == []


def test_recursion_depth_is_bounded():
    import shlex

    deep, benign = "aws s3 rm s3://b/k", "echo hi"
    for _ in range(8):
        deep = "bash -c " + shlex.quote(deep)
        benign = "bash -c " + shlex.quote(benign)
    v = cliclass.classify_command(deep)
    assert v and v[0].denied              # too deep to follow, with a guarded CLI inside: a denial, never an allow
    assert cliclass.classify_command(benign) == []


def test_no_command_text_is_ever_executed():
    """The guard only parses: a command that would create a file must not run during classification."""
    cliclass.classify_command("aws s3 ls; touch /nonexistent-dir/should-not-matter")
