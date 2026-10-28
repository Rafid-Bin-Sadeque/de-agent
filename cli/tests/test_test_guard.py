"""scripts/test_guard.py: removing or weakening a dbt test is denied for every agent (M4.1). Runs under DEA_HOOK_PYTHON (3.9)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts" / "test_guard.py"
HOOK_PYTHON = os.environ.get("DEA_HOOK_PYTHON", sys.executable)
DEMO_SCHEMA = (ROOT / "demo" / "dbt" / "acme" / "models" / "staging" / "schema.yml").read_text()

SCHEMA = """version: 2
models:
  - name: orders
    columns:
      - name: id
        tests:
          - unique
          - not_null
      - name: status
        data_tests:
          - accepted_values:
              values: [a, b]
      - name: customer_id
        tests:
          - relationships:
              to: ref('customers')
              field: id
          - dbt_utils.expression_is_true:
              expression: ">= 0"
  - name: items
    tests:
      - dbt_utils.unique_combination_of_columns:
          combination_of_columns: [a, b]
    columns:
      - name: item_id
        tests: [unique, not_null]
"""


def run(payload, cwd):
    p = subprocess.run([HOOK_PYTHON, str(GUARD)], input=json.dumps(payload) if not isinstance(payload, str) else payload,
                       capture_output=True, text=True, cwd=str(cwd), timeout=30)
    assert p.returncode == 0, p.stderr
    out = p.stdout.strip()
    return json.loads(out)["hookSpecificOutput"] if out else None


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "hub" / "dbt" / "acme"
    (root / "models" / "staging").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "tests" / "assert_positive.sql").write_text("select * from {{ ref('orders') }} where total < 0\n")
    (root / "dbt_project.yml").write_text("name: acme\nversion: '1'\n")
    (root / "models" / "staging" / "schema.yml").write_text(SCHEMA)
    (root / "models" / "staging" / "demo.yml").write_text(DEMO_SCHEMA)
    (root / "models" / "staging" / "stg.sql").write_text("select 1\n")
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "schema.yml").write_text(SCHEMA)  # not in a dbt project
    return tmp_path


def edit(path, old, new, tool="Edit", **kw):
    return {"tool_name": tool, "tool_input": {"file_path": str(path), "old_string": old, "new_string": new, **kw}}


def assert_denied(out):
    assert out is not None and out["permissionDecision"] == "deny"
    assert "removing or weakening a dbt test is never an incident fix" in out["permissionDecisionReason"]
    assert "write a plan with a diff step for a human to approve" in out["permissionDecisionReason"]


def sp(proj):
    return proj / "hub" / "dbt" / "acme" / "models" / "staging" / "schema.yml"


# ---------------- yaml edits: deny
def test_m4_g1_exact_edit_denied(proj):
    """The two M4 G1 violations: `unique` dropped from stg_order_items.order_item_id."""
    p = proj / "hub" / "dbt" / "acme" / "models" / "staging" / "demo.yml"
    old = "      - name: order_item_id\n        tests: [unique, not_null]\n  - name: stg_events"
    new = "      - name: order_item_id\n        tests: [not_null]\n  - name: stg_events"
    assert_denied(run(edit(p, old, new, replace_all=False), proj / "hub"))
    assert_denied(run(edit(p, "        tests: [unique, not_null]\n  - name: stg_events", "        tests: [not_null]\n  - name: stg_events"), proj))


@pytest.mark.parametrize("old,new", [
    ("          - unique\n          - not_null\n", "          - not_null\n"),                    # block list item removed
    ("          - not_null\n      - name: status", "      - name: status"),                     # emptied block list entirely
    ("tests: [unique, not_null]\n", "tests: []\n"),                                          # flow list emptied
    ("      - name: status\n        data_tests:\n          - accepted_values:\n              values: [a, b]\n", ""),  # column dropped
    ("          - relationships:\n              to: ref('customers')\n              field: id\n", ""),          # namespaced arg test
    ("          - dbt_utils.expression_is_true:\n              expression: \">= 0\"\n", ""),
    ("    tests:\n      - dbt_utils.unique_combination_of_columns:\n          combination_of_columns: [a, b]\n", ""),
    ("          - unique\n", "          # - unique\n"),                                      # commented out
    ("  - name: items\n", "  - name: items\n    config: {enabled: false}\n"),                 # disables
])
def test_yaml_edit_removing_tests_denied(proj, old, new):
    assert_denied(run(edit(sp(proj), old, new), proj))


@pytest.mark.parametrize("add", [
    "          - unique:\n              severity: warn\n",
    "          - unique:\n              config:\n                severity: warn\n",
    "          - not_null:\n              where: \"id > 100\"\n",
    "          - not_null:\n              enabled: false\n",
    "          - unique:\n              error_if: \">1000\"\n",
    "          - unique:\n              warn_if: \">50\"\n",
])
def test_yaml_edit_weakening_denied(proj, add):
    assert_denied(run(edit(sp(proj), "          - not_null\n      - name: status", "          - not_null\n" + add + "      - name: status"), proj))


def test_multiedit_one_removal_among_several_denied(proj):
    mp = {"tool_name": "MultiEdit", "tool_input": {"file_path": str(sp(proj)), "edits": [
        {"old_string": "version: 2", "new_string": "version: 2  "},
        {"old_string": "          - unique\n          - not_null\n", "new_string": "          - not_null\n"}]}}
    assert_denied(run(mp, proj))


def test_write_with_fewer_tests_denied(proj):
    new = SCHEMA.replace("          - unique\n", "")
    assert_denied(run({"tool_name": "Write", "tool_input": {"file_path": str(sp(proj)), "content": new}}, proj))
    assert_denied(run({"tool_name": "Write", "tool_input": {"file_path": str(sp(proj)), "content": "version: 2\n"}}, proj))


def test_write_adding_severity_warn_denied(proj):
    new = SCHEMA.replace("          - unique\n", "          - unique:\n              severity: warn\n", 1)
    assert_denied(run({"tool_name": "Write", "tool_input": {"file_path": str(sp(proj)), "content": new}}, proj))


# ---------------- yaml edits: allow
def test_adding_and_tightening_tests_allowed(proj):
    add = edit(sp(proj), "          - unique\n          - not_null\n", "          - unique\n          - not_null\n          - dbt_utils.not_empty_string\n")
    assert run(add, proj) is None
    flow = edit(sp(proj), "tests: [unique, not_null]\n  - name", "tests: [unique, not_null, positive_values]\n  - name")
    assert run(flow, proj) is None
    tighten = edit(sp(proj), "          - not_null\n      - name: status", "          - not_null:\n              severity: error\n      - name: status")
    assert run(tighten, proj) is None
    strict_if = edit(sp(proj), "          - not_null\n      - name: status", "          - not_null:\n              error_if: \">0\"\n      - name: status")
    assert run(strict_if, proj) is None


def test_unrelated_yaml_edit_and_description_allowed(proj):
    assert run(edit(sp(proj), "  - name: orders\n", "  - name: orders\n    description: orders fact\n"), proj) is None
    assert run(edit(sp(proj), "version: 2", "version: 2  # schema"), proj) is None
    # a Write that keeps every test (reordered / re-indented columns) is fine
    assert run({"tool_name": "Write", "tool_input": {"file_path": str(sp(proj)), "content": SCHEMA + "  - name: extra\n"}}, proj) is None


def test_new_yaml_file_and_non_dbt_project_allowed(proj):
    new = proj / "hub" / "dbt" / "acme" / "models" / "staging" / "new.yml"
    assert run({"tool_name": "Write", "tool_input": {"file_path": str(new), "content": SCHEMA}}, proj) is None
    plain = proj / "plain" / "schema.yml"  # no dbt_project.yml above it
    assert run(edit(plain, "          - unique\n          - not_null\n", "          - not_null\n"), proj) is None


def test_edit_where_old_string_missing_allowed(proj):
    assert run(edit(sp(proj), "no such text", ""), proj) is None  # the Edit tool itself fails; nothing is removed


def test_sql_model_edit_allowed(proj):
    sql = proj / "hub" / "dbt" / "acme" / "models" / "staging" / "stg.sql"
    assert run(edit(sql, "select 1", "select 2"), proj) is None


# ---------------- singular test files
def test_singular_test_emptied_or_deleted_denied(proj):
    t = proj / "hub" / "dbt" / "acme" / "tests" / "assert_positive.sql"
    assert_denied(run({"tool_name": "Write", "tool_input": {"file_path": str(t), "content": ""}}, proj))
    assert_denied(run({"tool_name": "Write", "tool_input": {"file_path": str(t), "content": "-- disabled\n"}}, proj))
    assert_denied(run(edit(t, "select * from {{ ref('orders') }} where total < 0\n", ""), proj))
    assert_denied(run(edit(t, "select * from {{ ref('orders') }} where total < 0", "/* off */"), proj))


def test_singular_test_edit_that_keeps_logic_allowed(proj):
    t = proj / "hub" / "dbt" / "acme" / "tests" / "assert_positive.sql"
    assert run(edit(t, "total < 0", "total <= 0"), proj) is None
    assert run({"tool_name": "Write", "tool_input": {"file_path": str(proj / "hub/dbt/acme/tests/new_test.sql"), "content": "select 1 where false"}}, proj) is None


def test_custom_test_paths_honoured(proj):
    root = proj / "hub" / "dbt" / "acme"
    (root / "dbt_project.yml").write_text("name: acme\ntest-paths: [\"checks\"]\n")
    (root / "checks").mkdir()
    (root / "checks" / "c.sql").write_text("select 1\n")
    assert_denied(run({"tool_name": "Write", "tool_input": {"file_path": str(root / "checks" / "c.sql"), "content": ""}}, proj))
    # `tests/` is no longer a test path
    assert run({"tool_name": "Write", "tool_input": {"file_path": str(root / "tests" / "assert_positive.sql"), "content": ""}}, proj) is None


# ---------------- Bash
@pytest.mark.parametrize("cmd", [
    "rm dbt/acme/models/staging/schema.yml",
    "rm -f dbt/acme/tests/assert_positive.sql",
    "rm -rf dbt/acme/tests",
    "rm dbt/acme/tests/*",
    "rm dbt/acme/models/staging/*.yml",
    "git rm dbt/acme/models/staging/schema.yml",
    "git rm -f dbt/acme/tests/assert_positive.sql",
    "mv dbt/acme/tests/assert_positive.sql /tmp/x.sql",
    "mv dbt/acme/models/staging/schema.yml /tmp/",
    "mv /tmp/new.yml dbt/acme/models/staging/schema.yml",
    "sed -i '' '/unique/d' dbt/acme/models/staging/schema.yml",
    "sed -i.bak 's/unique//' dbt/acme/models/staging/schema.yml",
    "sed -i -e 's/unique//' dbt/acme/models/staging/schema.yml",
    "sed --in-place 's/unique//' dbt/acme/models/staging/schema.yml",
    "perl -pi -e 's/unique//' dbt/acme/models/staging/schema.yml",
    "truncate -s 0 dbt/acme/tests/assert_positive.sql",
    "> dbt/acme/tests/assert_positive.sql",
    ": > dbt/acme/models/staging/schema.yml",
    "echo 'version: 2' > dbt/acme/models/staging/schema.yml",
    "cat /dev/null >| dbt/acme/models/staging/schema.yml",
    "cp /dev/null dbt/acme/tests/assert_positive.sql",
    "cd dbt/acme && rm models/staging/schema.yml",
    "cd dbt/acme && sed -i '' 's/x/y/' models/staging/demo.yml && ls",
    "echo hi && rm dbt/acme/tests/assert_positive.sql",
    "ls | xargs rm dbt/acme/tests/assert_positive.sql",
    "sudo rm dbt/acme/models/staging/schema.yml",
    "bash -c 'rm dbt/acme/models/staging/schema.yml'",
    "find dbt/acme/tests -name '*.sql' -delete",
    "rm -rf dbt",
    "rm -rf dbt/acme/models",
    "tee dbt/acme/models/staging/schema.yml < /tmp/new.yml",
    "python3 -c \"open('dbt/acme/models/staging/schema.yml','w').write('version: 2')\"",
    "docker compose exec -T airflow rm /opt/airflow/dbt/acme/models/staging/schema.yml",
    "docker compose exec -T airflow sed -i 's/unique//' /opt/airflow/dbt/acme/models/staging/schema.yml",
    "ssh box 'rm /srv/dbt/acme/tests/x.sql'",
])
def test_bash_deny(proj, cmd):
    hub = proj / "hub"
    assert_denied(run({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(hub)}, proj))


@pytest.mark.parametrize("cmd", [
    "ls dbt/acme/tests", "cat dbt/acme/models/staging/schema.yml", "grep -rn unique dbt/acme/models",
    "rm dbt/acme/target/manifest.json", "rm -rf dbt/acme/target dbt/acme/logs",
    "rm dbt/acme/models/staging/stg.sql", "sed -i '' 's/1/2/' dbt/acme/models/staging/stg.sql",
    "mv dbt/acme/models/staging/stg.sql dbt/acme/models/staging/stg2.sql",
    "echo x >> dbt/acme/tests/assert_positive.sql", "git status", "git diff dbt/acme", "git rm README.md",
    "rm /tmp/scratch.yml", "echo hi > /tmp/schema.yml", "cp dbt/acme/models/staging/schema.yml /tmp/backup.yml",
    "dbt test --select orders 2>&1 | tail", "dea snapshot 2>&1 | head", "echo done > /dev/null", "cat x 2>&1 >/dev/null",
    "rm plain/schema.yml", "ls > out.txt", "docker compose exec -T airflow dbt test", "ssh box 'ls /srv/dbt/acme/tests'",
    "python3 -c \"print(open('dbt/acme/models/staging/schema.yml').read())\"",
    "sed -n '1,10p' dbt/acme/models/staging/schema.yml", "dea apply my-plan",
])
def test_bash_allow(proj, cmd):
    hub = proj / "hub"
    assert run({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(hub)}, proj) is None, cmd


def test_applies_to_every_agent_including_executor(proj):
    p = edit(sp(proj), "          - unique\n          - not_null\n", "          - not_null\n")
    for agent in (None, "de-agent:executor", "de-agent:runner"):
        q = dict(p, **({"agent_type": agent} if agent else {}))
        assert_denied(run(q, proj))


@pytest.mark.parametrize("raw", ["", "nope", "[1]", "{"])
def test_malformed_input_fails_closed(tmp_path, raw):
    out = run(raw, tmp_path)
    assert out["permissionDecision"] == "deny" and "de-agent guard error" in out["permissionDecisionReason"]


def test_other_tools_ignored(proj):
    assert run({"tool_name": "Read", "tool_input": {"file_path": str(sp(proj))}}, proj) is None
    assert run({"tool_name": "Grep", "tool_input": {"pattern": "unique"}}, proj) is None


def test_hooks_json_registers_guard():
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]["PreToolUse"]
    ent = [h for h in hooks if any("test_guard.py" in " ".join(x.get("args", [])) for x in h["hooks"])]
    assert ent and set(ent[0]["matcher"].split("|")) == {"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"}
