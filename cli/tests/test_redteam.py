"""Close-out red-team fixes: every reproducer runs the real hook under DEA_HOOK_PYTHON (3.9) via corpus/redteam.yaml."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from de_agent.clients import DB_CLIENTS, SQL_CLIENTS, dialect_for
from de_agent.extract import extract_sql

ROOT = Path(__file__).resolve().parents[2]
HOOK_PYTHON = os.environ.get("DEA_HOOK_PYTHON", sys.executable)
GUARDS = {"sql": "sql_guard.py", "secret": "secret_guard.py", "self": "self_guard.py", "test": "test_guard.py"}
CASES = yaml.safe_load((Path(__file__).parent / "corpus" / "redteam.yaml").read_text())


def run_hook(guard, payload, project_dir, env_extra=None, cwd=None):
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(project_dir), CLAUDE_PLUGIN_ROOT=str(ROOT),
               XDG_CONFIG_HOME=str(Path(project_dir) / ".xdg"))
    env.pop("DEA_ALLOW_PLUGIN_EDITS", None)
    env.update(env_extra or {})
    p = subprocess.run([HOOK_PYTHON, str(ROOT / "scripts" / GUARDS[guard])], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, timeout=60, cwd=str(cwd or project_dir))
    assert p.returncode == 0, p.stderr
    out = p.stdout.strip()
    return json.loads(out)["hookSpecificOutput"] if out else None


def _fill(obj):
    if isinstance(obj, str):
        return (obj.replace("{ROOT}", str(ROOT)).replace("@@BIG_COMMAND@@", "x" * 210_000)
                .replace("@@BIG_SQL@@", "1," * 60_000 + "1"))
    if isinstance(obj, dict):
        return {k: _fill(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill(v) for v in obj]
    return obj


def _payload(inp):
    inp = _fill(inp)
    if isinstance(inp, str):
        return {"tool_name": "Bash", "tool_input": {"command": inp}, "cwd": "."}
    inp.setdefault("cwd", ".")
    return inp


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_corpus(case, tmp_path):
    out = run_hook(case["guard"], _payload(case["input"]), tmp_path)
    if case["expect"] == "deny":
        assert out and out["permissionDecision"] == "deny", f"{case['id']} was allowed"
    else:
        assert out is None, out


def test_corpus_runs_under_python39_when_asked():
    p = subprocess.run([HOOK_PYTHON, "-c", "import sys;print(sys.version_info[:2])"], capture_output=True, text=True)
    assert p.returncode == 0


# ---- A3 shared client list
def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_one_shared_client_list():
    sg = _load_script("secret_guard")
    assert set(sg.DB_CLIENTS) == set(DB_CLIENTS)
    assert {"mysql", "mariadb", "snowsql", "sqlcmd", "clickhouse-client", "sqlite3", "duckdb", "trino", "presto",
            "spark-sql", "beeline"} <= set(SQL_CLIENTS)


@pytest.mark.parametrize("src,want", [("mysql-heredoc", "mysql"), ("sqlcmd", "tsql"), ("snowsql", "snowflake"),
                                     ("clickhouse-client", "clickhouse"), ("sqlite3-file", "sqlite"),
                                     ("duckdb", "duckdb"), ("trino", "trino"), ("spark-sql", "spark"),
                                     ("beeline", "hive"), ("python:redshift", "redshift"), ("redshift-data", "redshift"),
                                     ("psql", "postgres"), ("mcp:sql", "postgres")])
def test_dialect_for(src, want):
    assert dialect_for(src) == want


@pytest.mark.parametrize("cmd,source,sql", [
    ("mysql -e 'select 1'", "mysql", "select 1"),
    ("mysql --execute 'select 2'", "mysql", "select 2"),
    ("mysql db < q.sql", "mysql-stdin", None),
    ("mysql <<< 'select 3'", "mysql-herestring", "select 3"),
    ("snowsql -q 'select 4'", "snowsql", "select 4"),
    ("sqlcmd -Q 'select 5'", "sqlcmd", "select 5"),
    ("clickhouse-client --query='select 6'", "clickhouse-client", "select 6"),
    ("sqlite3 x.db 'select 7'", "sqlite3", "select 7"),
    ("duckdb x.db 'select 8'", "duckdb", "select 8"),
    ("trino --execute 'select 9'", "trino", "select 9"),
    ("spark-sql -e 'select 10'", "spark-sql", "select 10"),
    ("beeline -e 'select 11'", "beeline", "select 11"),
])
def test_extract_new_clients(cmd, source, sql, tmp_path):
    got = extract_sql(cmd, str(tmp_path))
    assert got and got[0][0] == source
    if sql:
        assert got[0][1] == sql


def test_client_file_flags_read_the_file(tmp_path):
    (tmp_path / "q.sql").write_text("delete from t;")
    for cmd in ("snowsql -f q.sql", "sqlcmd -i q.sql", "trino -f q.sql", "spark-sql -f q.sql", "beeline -f q.sql",
                "mysql < q.sql", "duckdb -init q.sql"):
        got = extract_sql(cmd, str(tmp_path))
        assert any("delete from t" in s for _, s in got), cmd


# ---- A4 python scripts
def test_python_script_file_scanned(tmp_path):
    (tmp_path / "w.py").write_text("import psycopg2\nc = psycopg2.connect('x').cursor()\nc.execute('DELETE FROM t')\n")
    (tmp_path / "ok.py").write_text("import csv\nprint('delete from the list')\n")
    (tmp_path / "r.py").write_text("import psycopg2\nc.execute('select 1')\n")
    (tmp_path / "dyn.py").write_text("import psycopg2\nc.execute(build_sql())\n")
    big = tmp_path / "big.py"
    big.write_text("x = 1\n" * 300_000)
    cases = {"w.py": "deny", "ok.py": "allow", "r.py": "allow", "dyn.py": "deny", "big.py": "deny"}
    for name, want in cases.items():
        pl = {"tool_name": "Bash", "tool_input": {"command": f"python3 {tmp_path / name} --flag"}, "cwd": str(tmp_path)}
        out = run_hook("sql", pl, tmp_path)
        assert (out["permissionDecision"] if out else "allow") == want, name


def test_python_name_variants_scanned(tmp_path):
    code = "import sqlite3\nsqlite3.connect('x').execute('drop table t')"
    for py in ("python", "python3", "python3.11", "/usr/bin/python3", "uv run python"):
        got = extract_sql(f'{py} -c "{code}"')
        assert any("drop table" in s for _, s in got), py


def test_python_resolves_simple_names_and_concatenation():
    code = "import sqlite3\nq = 'select ' + '1'\nsqlite3.connect('x').execute(q)"
    assert [s for _, s in extract_sql(f'python3 -c "{code}"')] == ["select 1"]
    code = "import sqlite3\nq = 'delete from t'\nsqlite3.connect('x').execute(q)"
    assert any("delete from t" in s for _, s in extract_sql(f'python3 -c "{code}"'))


# ---- A5 secret guard
def test_symlink_to_secret_denied(tmp_path):
    (tmp_path / "real.env").write_text("X=1\n")
    target = tmp_path / ".env"
    target.write_text("X=1\n")
    os.symlink(target, tmp_path / "innocent.txt")
    out = run_hook("secret", {"tool_name": "Bash", "tool_input": {"command": "cat innocent.txt"}, "cwd": str(tmp_path)},
                   tmp_path)
    assert out and out["permissionDecision"] == "deny"
    out = run_hook("secret", {"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "innocent.txt")},
                              "cwd": str(tmp_path)}, tmp_path)
    assert out and out["permissionDecision"] == "deny"
    out = run_hook("secret", {"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "real.env")},
                              "cwd": str(tmp_path)}, tmp_path)
    assert out is None


def test_dotdot_path_to_secret_denied(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / ".env").write_text("X=1\n")
    out = run_hook("secret", {"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "a" / ".." / ".env")}},
                   tmp_path)
    assert out and out["permissionDecision"] == "deny"


# ---- A6 size cap
def test_size_cap_messages(tmp_path):
    out = run_hook("sql", _payload({"tool_name": "Bash", "tool_input": {"command": "@@BIG_COMMAND@@"}}), tmp_path)
    assert "too large to check" in out["permissionDecisionReason"]
    out = run_hook("sql", _payload({"tool_name": "Bash", "tool_input": {"command": 'psql -c "select @@BIG_SQL@@"'}}), tmp_path)
    assert "too large to check" in out["permissionDecisionReason"]


def test_size_cap_worst_case_parses_fast(tmp_path):
    import time

    shapes = [
        'psql -c "' + "select 1;" * 11_000 + '"',               # ~99 KB, 11k statements
        'psql -c "select ' + "a+" * 49_000 + '1"',               # ~98 KB expression chain
        'psql -c "select ' + "(" * 5_000 + "1" + ")" * 5_000 + '"',  # deep nesting
        'psql -c "select ' + ",".join(f"c{i}" for i in range(14_000)) + ' from t"',
        "echo " + "a " * 99_000,                                # ~198 KB command, no client
        "psql -c 'select 1' " * 10_000,                         # ~190 KB, many commands
    ]
    worst = 0.0
    for cmd in shapes:
        t0 = time.monotonic()
        run_hook("sql", {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": "."}, tmp_path)
        worst = max(worst, time.monotonic() - t0)
    print(f"worst-case guard time under the cap: {worst:.2f}s")
    assert worst < 5.0, worst


# ---- A7 guard the guards
def _yaml(tmp_path, text):
    p = tmp_path / "de-agent.yaml"
    p.write_text(text)
    return p


BASE_YAML = "version: 1\nsources:\n  - {name: a, type: postgres, conn: {host: h, dbname: d, user: u}}\nguard: {mode: strict}\n"


def test_self_yaml_guard_and_writable(tmp_path):
    p = _yaml(tmp_path, BASE_YAML)

    def edit(old, new):
        return run_hook("self", {"tool_name": "Edit", "tool_input": {"file_path": str(p), "old_string": old, "new_string": new}},
                        tmp_path)

    assert edit("mode: strict", "mode: ask")["permissionDecision"] == "deny"
    assert edit("user: u}}", "user: u}, writable: true}")["permissionDecision"] == "deny"
    assert edit("version: 1", "version: 1\nscans:\n  - {name: s, type: sql, path: ../x}") is None  # adding scans is fine
    assert edit("name: a,", "name: renamed,") is None
    out = run_hook("self", {"tool_name": "Write", "tool_input": {"file_path": str(p), "content": BASE_YAML.replace("strict", "ask")}}, tmp_path)
    assert out["permissionDecision"] == "deny"
    out = run_hook("self", {"tool_name": "Write", "tool_input": {"file_path": str(p), "content": BASE_YAML + "  - {name: b, type: postgres}\n"}}, tmp_path)
    assert out is None
    out = run_hook("self", {"tool_name": "Bash", "tool_input": {"command": f"sed -i 's/strict/ask/' {p}"}}, tmp_path)
    assert out["permissionDecision"] == "deny"


def test_self_settings_hooks_edits(tmp_path):
    home = tmp_path / ".claude"
    home.mkdir()
    p = home / "settings.json"
    p.write_text(json.dumps({"hooks": {"Stop": []}, "permissions": {"allow": ["Bash(ls)"]}}, indent=2))
    ok = run_hook("self", {"tool_name": "Edit", "tool_input": {"file_path": str(p), "old_string": "Bash(ls)", "new_string": "Bash(pwd)"}}, tmp_path)
    assert ok is None
    bad = run_hook("self", {"tool_name": "Edit", "tool_input": {"file_path": str(p), "old_string": '"Stop": []', "new_string": '"Stop": [1]'}}, tmp_path)
    assert bad["permissionDecision"] == "deny"
    bad = run_hook("self", {"tool_name": "Edit", "tool_input": {"file_path": str(p), "old_string": '"permissions"', "new_string": '"disableAllHooks": true, "permissions"'}}, tmp_path)
    assert bad["permissionDecision"] == "deny"


def test_self_dev_escape_hatch(tmp_path):
    pl = {"tool_name": "Write", "tool_input": {"file_path": f"{ROOT}/scripts/x.py", "content": "x"}}
    assert run_hook("self", pl, tmp_path)["permissionDecision"] == "deny"
    assert run_hook("self", pl, tmp_path, env_extra={"DEA_ALLOW_PLUGIN_EDITS": "1"}) is None


def test_self_inactive_without_plugin_root(tmp_path):
    pl = {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "scripts" / "x.py"), "content": "x"}}
    assert run_hook("self", pl, tmp_path, env_extra={"CLAUDE_PLUGIN_ROOT": ""}) is None


# ---- A8 test_guard editors
@pytest.mark.parametrize("cmd", [
    "dd if=/dev/null of=dbt/acme/models/schema.yml",
    "ex -sc '%d|x' dbt/acme/models/schema.yml",
    "ed dbt/acme/models/schema.yml",
    "vi -c 'g/unique/d' -c wq dbt/acme/models/schema.yml",
    "vim -es -c '%d' -c wq dbt/acme/tests/assert_positive.sql",
    "perl -i -ne 'print unless /unique/' dbt/acme/models/schema.yml",
    "ruby -i -ne 'print unless /unique/' dbt/acme/models/schema.yml",
])
def test_test_guard_inplace_editors(cmd, tmp_path):
    root = tmp_path / "dbt" / "acme"
    (root / "models").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "dbt_project.yml").write_text("name: acme\n")
    (root / "models" / "schema.yml").write_text("models:\n  - name: o\n    tests: [unique]\n")
    (root / "tests" / "assert_positive.sql").write_text("select 1\n")
    out = run_hook("test", {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)}, tmp_path, cwd=tmp_path)
    assert out and out["permissionDecision"] == "deny"
