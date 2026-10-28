import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SQL_GUARD = ROOT / "scripts" / "sql_guard.py"
SECRET_GUARD = ROOT / "scripts" / "secret_guard.py"


HOOK_PYTHON = os.environ.get("DEA_HOOK_PYTHON", sys.executable)


def run(script, payload, project_dir, raw=None, env_extra=None):
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(project_dir), CLAUDE_PLUGIN_ROOT=str(ROOT),
               XDG_CONFIG_HOME=str(Path(project_dir) / ".xdg"))  # approval keys never touch the real ~/.config
    env.update(env_extra or {})
    p = subprocess.run(
        [HOOK_PYTHON, str(script)],
        input=raw if raw is not None else json.dumps(payload),
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert p.returncode == 0, p.stderr
    out = p.stdout.strip()
    return json.loads(out)["hookSpecificOutput"] if out else None


def bash(cmd, **extra):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": ".", **extra}


def test_bash_read_allowed(tmp_path):
    assert run(SQL_GUARD, bash('psql -c "select 1"'), tmp_path) is None


def test_non_db_command_allowed(tmp_path):
    assert run(SQL_GUARD, bash("ls -la"), tmp_path) is None


def test_bash_write_denied(tmp_path):
    out = run(SQL_GUARD, bash('psql -c "delete from t"'), tmp_path)
    assert out["permissionDecision"] == "deny"
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecisionReason"].startswith("de-agent: DELETE is a write; writes need an approved plan (dea approve). Statement: ")


def test_write_hidden_in_second_statement_denied(tmp_path):
    out = run(SQL_GUARD, bash('psql -c "select 1; drop table t"'), tmp_path)
    assert out["permissionDecision"] == "deny" and "DROP" in out["permissionDecisionReason"]


def test_unreadable_file_denied(tmp_path):
    assert run(SQL_GUARD, bash("psql -f /nope/missing.sql"), tmp_path)["permissionDecision"] == "deny"


def _approve(tmp_path, sql):
    from de_agent.approval import make_approval, write_approval
    from de_agent.sqlclass import classify

    key = b"test-key"
    kp = tmp_path / ".xdg" / "de-agent"
    kp.mkdir(parents=True, exist_ok=True)
    (kp / "approve.key").write_bytes(key + b"\n")
    plans = tmp_path / ".dea" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    data = make_approval("p1", [s.hash for s in classify(sql)], "tester", "2026-10-27T00:00:00Z", key)
    write_approval(str(plans / "p1.approved"), data)


def test_executor_with_approved_hash_allowed(tmp_path):
    _approve(tmp_path, "delete from t where id = 1")
    p = bash('psql -c "delete from t where id = 1"', agent_type="de-agent:executor")
    assert run(SQL_GUARD, p, tmp_path) is None


def test_executor_with_unapproved_statement_denied(tmp_path):
    _approve(tmp_path, "delete from t where id = 1")
    p = bash('psql -c "delete from t where id = 2"', agent_type="de-agent:executor")
    assert run(SQL_GUARD, p, tmp_path)["permissionDecision"] == "deny"


def test_executor_partial_approval_denied(tmp_path):
    _approve(tmp_path, "delete from t where id = 1")
    p = bash('psql -c "delete from t where id = 1; drop table t"', agent_type="de-agent:executor")
    assert run(SQL_GUARD, p, tmp_path)["permissionDecision"] == "deny"


def test_approved_hash_ignored_for_other_agents(tmp_path):
    _approve(tmp_path, "delete from t where id = 1")
    assert run(SQL_GUARD, bash('psql -c "delete from t where id = 1"'), tmp_path)["permissionDecision"] == "deny"
    p = bash('psql -c "delete from t where id = 1"', agent_type="de-agent:runner")
    assert run(SQL_GUARD, p, tmp_path)["permissionDecision"] == "deny"


def test_ask_mode(tmp_path):
    (tmp_path / "de-agent.yaml").write_text("guard: {mode: ask, write_roles_forbidden: true}\n")
    assert run(SQL_GUARD, bash('psql -c "delete from t"'), tmp_path)["permissionDecision"] == "ask"


def test_ask_mode_block_form(tmp_path):
    (tmp_path / "de-agent.yaml").write_text("hub: .\nguard:\n  mode: ask\n")
    assert run(SQL_GUARD, bash('psql -c "drop table t"'), tmp_path)["permissionDecision"] == "ask"


def test_mcp_query_write_denied(tmp_path):
    p = {"tool_name": "mcp__warehouse__run_query", "tool_input": {"sql": "update orders set total = 0"}}
    assert run(SQL_GUARD, p, tmp_path)["permissionDecision"] == "deny"


def test_mcp_query_read_allowed(tmp_path):
    p = {"tool_name": "mcp__warehouse__run_query", "tool_input": {"query": "select 1"}}
    assert run(SQL_GUARD, p, tmp_path) is None


def test_mcp_statement_field_checked(tmp_path):
    p = {"tool_name": "mcp__wh__execute_sql", "tool_input": {"statement": "drop table t"}}
    assert run(SQL_GUARD, p, tmp_path)["permissionDecision"] == "deny"


def test_mcp_unrelated_tool_without_sql_ignored(tmp_path):
    p = {"tool_name": "mcp__docs__search", "tool_input": {"text": "delete the old pages", "query": "how do I delete"}}
    assert run(SQL_GUARD, p, tmp_path) is None


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", "{"])
def test_sql_guard_malformed_stdin_denied(tmp_path, raw):
    out = run(SQL_GUARD, None, tmp_path, raw=raw)
    assert out["permissionDecision"] == "deny" and "de-agent guard error" in out["permissionDecisionReason"]


def test_sql_guard_fails_closed_when_sqlglot_missing(tmp_path):
    # a copy of the plugin without scripts/_vendor, with a shim that makes sqlglot unimportable
    plug = tmp_path / "plug"
    (plug / "scripts").mkdir(parents=True)
    shutil.copy(SQL_GUARD, plug / "scripts" / "sql_guard.py")
    shutil.copy(ROOT / "scripts" / "_guardlib.py", plug / "scripts" / "_guardlib.py")
    shutil.copytree(ROOT / "cli" / "src", plug / "cli" / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "sqlglot.py").write_text("raise ImportError('blocked for test')\n")
    out = run(plug / "scripts" / "sql_guard.py", bash('psql -c "select 1"'), tmp_path,
              env_extra={"PYTHONPATH": str(shim), "CLAUDE_PLUGIN_ROOT": ""})
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == "de-agent CLI not installed: pipx install de-agent"


def test_sql_guard_works_without_plugin_root_env(tmp_path):
    out = run(SQL_GUARD, bash('psql -c "delete from t"'), tmp_path, env_extra={"CLAUDE_PLUGIN_ROOT": ""})
    assert out["permissionDecision"] == "deny" and "DELETE is a write" in out["permissionDecisionReason"]


def test_session_statement_then_select_allowed(tmp_path):
    assert run(SQL_GUARD, bash("psql -c 'SET statement_timeout=1; select 1'"), tmp_path) is None


def test_set_role_denied(tmp_path):
    assert run(SQL_GUARD, bash("psql -c 'set role admin'"), tmp_path)["permissionDecision"] == "deny"


def test_begin_delete_commit_denied(tmp_path):
    out = run(SQL_GUARD, bash("psql -c 'begin; delete from t; commit'"), tmp_path)
    assert out["permissionDecision"] == "deny" and "DELETE" in out["permissionDecisionReason"]


@pytest.mark.parametrize("cmd", [
    'dea query warehouse "$Q"', 'psql -c "$1"', 'psql -c "${SQL}"', "psql -c '$Q'", 'dea query w "$1"',
])
def test_bare_shell_variable_denied_with_clear_message(tmp_path, cmd):
    out = run(SQL_GUARD, bash(cmd), tmp_path)
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == (
        "de-agent: SQL passed via a shell variable can't be checked; inline the literal SQL in the command")


def test_bare_variable_denied_even_for_executor(tmp_path):
    out = run(SQL_GUARD, bash('psql -c "$Q"', agent_type="de-agent:executor"), tmp_path)
    assert out["permissionDecision"] == "deny" and "shell variable" in out["permissionDecisionReason"]


# ---- secret_guard
@pytest.mark.parametrize("cmd", [
    "PGPASSWORD=hunter2 psql -c 'select 1'",
    "export PGPASSWORD=hunter2",
    "MYSQL_PWD=x mysql -e 'select 1'",
    "AWS_SECRET_ACCESS_KEY=abc aws s3 ls",
    "psql postgres://bob:hunter2@host/db -c 'select 1'",
    "psql postgresql://bob:hunter2@host:5432/db",
    "psql 'host=h dbname=d user=u password=hunter2'",
    "mysql -u bob --password=hunter2 -e 'select 1'", "pg_dump 'host=h password=hunter2' db", "psql password=hunter2",
    "mysqldump --password=hunter2 db", "snowsql -a acct -u bob --password=hunter2", "bq query --password=hunter2 'select 1'",
    "duckdb -c \"attach 'host=h password=hunter2' as pg\"",
    "python3 -c \"import psycopg; psycopg.connect(host='h', password=hunter2)\"",
    "python -c \"connect('x', password=hunter2)\"", "node -e \"connect({password=hunter2})\"",
    "export PASSWORD=hunter2", "DB_PASSWORD=hunter2 ./run.sh", "PGPASSWORD=hunter2 pg_dump db", "env PGPASSWORD=hunter2 psql",
    "export PGPASSWORD='hunter2'", "echo x; export MYSQL_PWD=hunter2",
    "sqlcmd -S host -U sa -P x password=hunter2", "echo 'host=h dbname=d password=hunter2' > dsn.txt",
    "curl 'https://h/api?user=u&password=hunter2'",
    "cat ~/.pgpass", "less ~/.pgpass", "grep host ~/.pgpass", "cat .env", "head -5 ../project/.env", "tail .env.production",
    "cat server.pem", "cat ~/.aws/credentials", "sed -n 1p ~/.aws/credentials", "awk '{print}' .env", "cat < ~/.pgpass",
    "sudo cat ~/.pgpass", "cat \"$HOME/.pgpass\"",
    # M3: AWS credential stores and secret-reading CLI calls
    "cat ~/.aws/config", "head /home/x/.aws/config", "grep -r key ~/.aws/credentials", "cat ~/.aws/sso/cache/abc.json",
    "aws configure get aws_secret_access_key", "aws configure get aws_secret_access_key --profile prod",
    "aws configure get aws_session_token", "aws configure export-credentials --profile prod", "aws sts get-session-token",
    "aws secretsmanager get-secret-value --secret-id x", "ls ~/.aws/cli/cache/ && cat ~/.aws/cli/cache/x.json",
])
def test_secret_guard_denies_bash(tmp_path, cmd):
    out = run(SECRET_GUARD, bash(cmd), tmp_path)
    assert out["permissionDecision"] == "deny" and "secrets must not enter the transcript" in out["permissionDecisionReason"]


@pytest.mark.parametrize("cmd", [
    "psql -c 'select 1'", "ls ~/.ssh", "cat README.md", "cat .env.example", "ls -la .env", "echo hello",
    "psql -h host -U readonly_agent -d db -c 'select 1'", "git status", "grep -r password docs/",
    "grep -rn password= src/", "rg 'password='", "rg -n 'password=' --glob '*.py'", "sed -n '/password=/p' notes.txt",
    "echo 'set password= in the form'", "grep -r 'PGPASSWORD' docs/", "cd /tmp && ls", "echo $PWD",
    "git log --grep='password='", "aws configure get region", "aws sts get-caller-identity", "aws s3 ls s3://acme-lake/bronze/",
    "aws redshift describe-clusters", "cat docs/aws-iam-readonly.json",
])
def test_secret_guard_allows_bash(tmp_path, cmd):
    assert run(SECRET_GUARD, bash(cmd), tmp_path) is None


@pytest.mark.parametrize("path", ["/home/u/.pgpass", "/proj/.env", "/proj/.env.local", "/keys/server.pem", "/home/u/.aws/credentials",
                                  "/home/u/.aws/config", "/home/u/.aws", "/home/u/.aws/sso/cache/x.json"])
def test_secret_guard_denies_read_tool(tmp_path, path):
    out = run(SECRET_GUARD, {"tool_name": "Read", "tool_input": {"file_path": path}}, tmp_path)
    assert out["permissionDecision"] == "deny"


@pytest.mark.parametrize("path", ["/home/u/.aws", "/home/u/.aws/config", "/home/u/.aws/credentials"])
def test_secret_guard_denies_grep_over_aws_dir_and_files(tmp_path, path):
    out = run(SECRET_GUARD, {"tool_name": "Grep", "tool_input": {"path": path, "pattern": "key"}}, tmp_path)
    assert out["permissionDecision"] == "deny"


def test_secret_guard_allows_normal_read(tmp_path):
    assert run(SECRET_GUARD, {"tool_name": "Read", "tool_input": {"file_path": "/proj/README.md"}}, tmp_path) is None
    assert run(SECRET_GUARD, {"tool_name": "Read", "tool_input": {"file_path": "/proj/.env.example"}}, tmp_path) is None


@pytest.mark.parametrize("raw", ["", "garbage", "[]"])
def test_secret_guard_malformed_stdin_denied(tmp_path, raw):
    out = run(SECRET_GUARD, None, tmp_path, raw=raw)
    assert out["permissionDecision"] == "deny" and "de-agent guard error" in out["permissionDecisionReason"]


# ---- M2: approval signatures, approval_guard, dea query visibility
APPROVAL_GUARD = ROOT / "scripts" / "approval_guard.py"
EXECUTOR = "de-agent:executor"


def test_unsigned_approved_file_grants_nothing(tmp_path):
    plans = tmp_path / ".dea" / "plans"
    plans.mkdir(parents=True)
    from de_agent.sqlclass import classify

    (plans / "p1.approved").write_text(json.dumps({"hashes": [s.hash for s in classify("delete from t where id = 1")]}))
    p = bash('psql -c "delete from t where id = 1"', agent_type=EXECUTOR)
    assert run(SQL_GUARD, p, tmp_path)["permissionDecision"] == "deny"


def test_tampered_approved_file_denied(tmp_path):
    from de_agent.sqlclass import classify

    _approve(tmp_path, "delete from t where id = 1")
    f = tmp_path / ".dea" / "plans" / "p1.approved"
    data = json.loads(f.read_text())
    data["hashes"].append(classify("drop table t")[0].hash)  # edited hash list, old MAC
    f.write_text(json.dumps(data))
    assert run(SQL_GUARD, bash('psql -c "drop table t"', agent_type=EXECUTOR), tmp_path)["permissionDecision"] == "deny"
    assert run(SQL_GUARD, bash('psql -c "delete from t where id = 1"', agent_type=EXECUTOR), tmp_path)[
        "permissionDecision"] == "deny"  # the whole file is void, not just the new hash


def test_approved_file_with_swapped_key_denied(tmp_path):
    _approve(tmp_path, "delete from t where id = 1")
    (tmp_path / ".xdg" / "de-agent" / "approve.key").write_bytes(b"another-key\n")
    p = bash('psql -c "delete from t where id = 1"', agent_type=EXECUTOR)
    assert run(SQL_GUARD, p, tmp_path)["permissionDecision"] == "deny"


def test_approved_file_for_other_plan_id_denied(tmp_path):
    _approve(tmp_path, "delete from t where id = 1")
    plans = tmp_path / ".dea" / "plans"
    (plans / "p1.approved").rename(plans / "other.approved")  # plan_id inside the MAC says p1
    p = bash('psql -c "delete from t where id = 1"', agent_type=EXECUTOR)
    assert run(SQL_GUARD, p, tmp_path)["permissionDecision"] == "deny"


def test_dea_query_write_denied_and_read_allowed(tmp_path):
    assert run(SQL_GUARD, bash('dea query warehouse "delete from t"'), tmp_path)["permissionDecision"] == "deny"
    assert run(SQL_GUARD, bash('dea query warehouse "select 1" --limit 5'), tmp_path) is None
    assert run(SQL_GUARD, bash("dea find orders"), tmp_path) is None


@pytest.mark.parametrize("cmd,agent,want", [
    # dea approve: denied for everyone
    ("dea approve my-plan", None, "deny"),
    ("dea approve my-plan", EXECUTOR, "deny"),
    ("dea approve my-plan", "de-agent:runner", "deny"),
    ("cd demo && cli/.venv/bin/dea approve my-plan", None, "deny"),
    ("bash -c 'dea approve my-plan'", EXECUTOR, "deny"),
    # shell writes / reads of approval material
    ("echo {} > .dea/plans/x.approved", None, "deny"),
    ("echo {} > .dea/plans/x.approved", EXECUTOR, "deny"),
    ("cat ~/.config/de-agent/approve.key", None, "deny"),
    ("cat ~/.config/de-agent/approve.key", EXECUTOR, "deny"),
    ("cp .dea/plans/a.approved .dea/plans/b.approved", EXECUTOR, "deny"),
    # dea apply: executor only
    ("dea apply my-plan", None, "deny"),
    ("dea apply my-plan", "de-agent:runner", "deny"),
    ("dea apply my-plan --dry-run", None, "deny"),
    ("dea apply my-plan", EXECUTOR, "allow"),
    ("dea apply my-plan --rollback", EXECUTOR, "allow"),
    ("cd demo && ../cli/.venv/bin/dea apply my-plan", EXECUTOR, "allow"),
    # everything around it stays usable
    ("dea plan check my-plan", None, "allow"),
    ("dea plans", None, "allow"),
    ("dea plan check my-plan", "de-agent:runner", "allow"),
    ("cat .dea/plans/my-plan.md", None, "allow"),
    ("ls .dea/plans", None, "allow"),
    ("git status", None, "allow"),
    ("git commit -m 'update docs'", None, "allow"),
])
def test_approval_guard_bash_corpus(tmp_path, cmd, agent, want):
    payload = bash(cmd, **({"agent_type": agent} if agent else {}))
    out = run(APPROVAL_GUARD, payload, tmp_path)
    assert (out["permissionDecision"] if out else "allow") == want, out
    if out:
        assert out["hookEventName"] == "PreToolUse"
        assert "dea approve" in out["permissionDecisionReason"] or "executor agent" in out["permissionDecisionReason"]


@pytest.mark.parametrize("tool,path,agent,want", [
    ("Write", "/proj/.dea/plans/x.approved", None, "deny"),
    ("Write", ".dea/plans/x.approved", EXECUTOR, "deny"),
    ("Edit", "/proj/.dea/plans/x.approved", None, "deny"),
    ("MultiEdit", "/proj/.dea/plans/x.approved", "de-agent:runner", "deny"),
    ("NotebookEdit", "/proj/.dea/plans/x.approved", None, "deny"),
    ("Write", "/proj/.dea/plans/./sub/../x.approved", None, "deny"),
    ("Write", "/home/u/.config/de-agent/approve.key", None, "deny"),
    ("Write", "/proj/.dea/plans/x.md", None, "allow"),
    ("Edit", "/proj/.dea/plans/x.md", EXECUTOR, "allow"),
    ("Write", "/proj/docs/notes.approved", None, "allow"),
])
def test_approval_guard_write_tools(tmp_path, tool, path, agent, want):
    payload = {"tool_name": tool, "tool_input": {"file_path": path}, "cwd": "."}
    if agent:
        payload["agent_type"] = agent
    out = run(APPROVAL_GUARD, payload, tmp_path)
    assert (out["permissionDecision"] if out else "allow") == want, out


@pytest.mark.parametrize("raw,want", [
    ("not json dea approve x", "deny"),
    ("{ broken .dea/plans/x.approved", "deny"),
    ("garbage dea apply x", "deny"),
    ("garbage", "allow"),  # nothing approval-shaped; sql_guard/secret_guard deny malformed input themselves
])
def test_approval_guard_fails_closed_on_malformed(tmp_path, raw, want):
    out = run(APPROVAL_GUARD, None, tmp_path, raw=raw)
    assert (out["permissionDecision"] if out else "allow") == want


@pytest.mark.parametrize("path", ["/home/u/.config/de-agent/approve.key", "/x/approve.key"])
def test_secret_guard_denies_reading_approval_key(tmp_path, path):
    assert run(SECRET_GUARD, {"tool_name": "Read", "tool_input": {"file_path": path}}, tmp_path)["permissionDecision"] == "deny"
    assert run(SECRET_GUARD, {"tool_name": "Grep", "tool_input": {"path": path, "pattern": "."}},
               tmp_path)["permissionDecision"] == "deny"
    assert run(SECRET_GUARD, bash(f"cat {path}"), tmp_path)["permissionDecision"] == "deny"


def test_secret_guard_denies_grep_over_config_dir(tmp_path):
    p = {"tool_name": "Grep", "tool_input": {"path": "/home/u/.config/de-agent", "pattern": "."}}
    assert run(SECRET_GUARD, p, tmp_path)["permissionDecision"] == "deny"


# ---- M4.1: secret_guard on the Grep tool (path, glob, include, type, pattern)
def _grep(tmp, **ti):
    return run(SECRET_GUARD, {"tool_name": "Grep", "tool_input": ti, "cwd": str(tmp)}, tmp)


@pytest.mark.parametrize("ti", [
    {"pattern": "DEMO", "path": "/proj", "glob": "**/.env*"},          # the exact M4 G4 leak
    {"pattern": "x", "glob": ".env*"}, {"pattern": "x", "glob": "*.pem"}, {"pattern": "x", "glob": "**/*.key"},
    {"pattern": "x", "glob": "{.env,.pgpass}"}, {"pattern": "x", "glob": "**/credentials"},
    {"pattern": "x", "glob": "**/.aws/*"}, {"pattern": "x", "glob": "*.{py,env}"}, {"pattern": "x", "glob": "*.md .env.*"},
    {"pattern": "x", "include": "*.pem"}, {"pattern": "x", "type": "env"}, {"pattern": "x", "glob": "approve.key"},
    {"pattern": "x", "glob": ".*"},
])
def test_grep_denies_secret_globs_and_types(tmp_path, ti):
    out = _grep(tmp_path, **ti)
    assert out and out["permissionDecision"] == "deny" and "secrets must not enter the transcript" in out["permissionDecisionReason"]


def test_grep_denies_secret_pattern_when_tree_has_env(tmp_path):
    (tmp_path / ".env").write_text("X=1\n")
    for ti in ({"pattern": "PASSWORD"}, {"pattern": "api_key", "path": str(tmp_path)}, {"pattern": "token", "glob": "*"},
               {"pattern": "secret", "glob": "**/*"}, {"pattern": "Passwd|x", "path": str(tmp_path), "output_mode": "content"}):
        out = _grep(tmp_path, **ti)
        assert out and out["permissionDecision"] == "deny", ti


def test_grep_secret_pattern_allowed_when_scoped_or_no_secret_files(tmp_path):
    (tmp_path / ".env").write_text("X=1\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("password = 1\n")
    assert _grep(tmp_path, pattern="password", glob="*.py") is None
    assert _grep(tmp_path, pattern="password", type="py") is None
    assert _grep(tmp_path, pattern="password", path=str(tmp_path / "src" / "a.py")) is None
    assert _grep(tmp_path, pattern="password", path=str(tmp_path / "src")) is None  # no credential file in that tree
    assert _grep(tmp_path, pattern="password", glob="src/**/*.{py,sql}") is None


@pytest.mark.parametrize("ti", [
    {"pattern": "def main"}, {"pattern": "select", "glob": "**/*.sql"}, {"pattern": "x", "glob": "*.{ts,tsx}"},
    {"pattern": "x", "type": "py"}, {"pattern": "x", "glob": "!.env*"}, {"pattern": "x", "path": "/proj/src"},
    {"pattern": "x", "glob": "*.json"}, {"pattern": "x", "glob": "*"},
])
def test_grep_allows_normal_searches(tmp_path, ti):
    (tmp_path / ".env").write_text("X=1\n")  # a pattern without secret words is fine even when a .env is in the tree
    assert _grep(tmp_path, **ti) is None


def test_grep_still_denies_secret_paths(tmp_path):
    assert _grep(tmp_path, pattern="x", path="/home/u/.pgpass")["permissionDecision"] == "deny"
    assert _grep(tmp_path, pattern="x", path="/home/u/.config/de-agent")["permissionDecision"] == "deny"


@pytest.mark.parametrize("cmd", ["grep -r x --include=.env* .", "rg x -g '.env*'", "rg x --glob=*.pem", "grep -r x --include '*.key' ."])
def test_secret_guard_denies_bash_grep_with_secret_globs(tmp_path, cmd):
    assert run(SECRET_GUARD, bash(cmd), tmp_path)["permissionDecision"] == "deny"


def test_dea_help_with_redirect_not_denied_by_sql_guard(tmp_path):
    for cmd in ("dea --help 2>&1 | head", "dea query --help 2>&1 | head -50", "dea snapshot 2>&1 | tail", "dea diff >&2",
                'dea query warehouse "select 1" 2>&1 | head'):
        assert run(SQL_GUARD, bash(cmd), tmp_path) is None, cmd
    assert run(SQL_GUARD, bash('dea query warehouse "delete from t" 2>&1 | head'), tmp_path)["permissionDecision"] == "deny"
