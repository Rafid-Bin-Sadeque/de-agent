"""v0.3.2 1c: interpreter writes to the plugin's protected paths are denied like Edit; mentions are not writes."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SELF_GUARD = ROOT / "scripts" / "self_guard.py"
TEST_GUARD = ROOT / "scripts" / "test_guard.py"
HOOK_PYTHON = os.environ.get("DEA_HOOK_PYTHON", sys.executable)


def _run(script, command, cwd, plugin, maintainer=False):
    env = dict(os.environ, CLAUDE_PLUGIN_ROOT=str(plugin), CLAUDE_PROJECT_DIR=str(cwd), HOME=str(Path(cwd) / ".home"),
               XDG_CONFIG_HOME=str(Path(cwd) / ".xdg"))
    env.pop("DEA_ALLOW_PLUGIN_EDITS", None)
    env.pop("DEA_HUB", None)
    if maintainer:
        env["DEA_ALLOW_PLUGIN_EDITS"] = "1"
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    p = subprocess.run([HOOK_PYTHON, str(script)], input=json.dumps(payload), capture_output=True, text=True, env=env,
                       timeout=30)
    assert p.returncode == 0, p.stderr
    out = p.stdout.strip()
    return json.loads(out)["hookSpecificOutput"] if out else None


@pytest.fixture
def plugin(tmp_path):
    root = tmp_path / "plugin"
    for d in ("scripts", "hooks", "cli/src/de_agent"):
        (root / d).mkdir(parents=True)
    return root


HEREDOC = "python3 - <<'EOF'\nimport pathlib\np = pathlib.Path('{path}')\np.write_text(p.read_text())\nEOF"


@pytest.mark.parametrize("cmd", [
    HEREDOC.format(path="scripts/secret_guard.py"),
    HEREDOC.format(path="cli/src/de_agent/secretscan.py"),
    "python3 -c \"open('hooks/hooks.json', 'w').write('{}')\"",
    "python3 -c \"import shutil; shutil.copy('/tmp/x.py', 'scripts/sql_guard.py')\"",
    "node -e \"require('fs').writeFileSync('scripts/a.py', '')\"",
    "cd cli && python3 - <<'EOF'\nimport pathlib\npathlib.Path('src/de_agent/clients.py').write_text('')\nEOF",
])
def test_interpreter_writes_to_protected_paths_denied(plugin, cmd):
    out = _run(SELF_GUARD, cmd, plugin, plugin)
    assert out and out["permissionDecision"] == "deny" and "guards protect themselves" in out["permissionDecisionReason"]


def test_maintainer_session_may_rewrite_the_plugin(plugin):
    assert _run(SELF_GUARD, HEREDOC.format(path="scripts/secret_guard.py"), plugin, plugin, maintainer=True) is None


def test_interpreter_writes_elsewhere_allowed(plugin, tmp_path):
    hub = tmp_path / "hub"
    hub.mkdir()
    for cmd in (HEREDOC.format(path="scripts/secret_guard.py"),  # hub-relative scripts/, not the plugin's
                "python3 -c \"open('out/summary.csv', 'w').write('1')\""):
        assert _run(SELF_GUARD, cmd, hub, plugin) is None


@pytest.mark.parametrize("cmd", [
    "python3 - <<'EOF'\nimport pathlib\np = pathlib.Path('notes.py')\n"
    "p.write_text(p.read_text().replace('x', 'no sources configured in de-agent.yaml'))\nEOF",
    "python3 -c \"open('report.md', 'w').write('see de-agent.yaml for the guard mode')\"",
])
def test_prose_mention_of_the_hub_yaml_is_not_a_write(plugin, tmp_path, cmd):
    hub = tmp_path / "hub"
    hub.mkdir()
    assert _run(SELF_GUARD, cmd, hub, plugin) is None


@pytest.mark.parametrize("cmd", [
    "python3 -c \"open('de-agent.yaml', 'w').write('guard: off')\"",
    "python3 -c \"import yaml; yaml.dump({}, open('../hub/de-agent.yaml', 'w'))\"",
    "python3 rewrite.py de-agent.yaml && python3 -c \"open('x', 'w')\"",
])
def test_real_hub_yaml_writes_still_denied(plugin, tmp_path, cmd):
    hub = tmp_path / "hub"
    hub.mkdir()
    out = _run(SELF_GUARD, cmd, hub, plugin)
    assert out and out["permissionDecision"] == "deny" and "de-agent.yaml" in out["permissionDecisionReason"]


def test_dbt_test_rule_ignores_globs_in_unrelated_script_text(tmp_path, plugin):
    """`*.yml` in a python edit of a non-dbt file is not a dbt test, even with a dbt project somewhere below."""
    repo = tmp_path / "repo"
    (repo / "demo" / "tests").mkdir(parents=True)
    (repo / "demo" / "dbt_project.yml").write_text("name: demo\n")
    (repo / "scripts").mkdir()
    cmd = ("python3 -c \"import pathlib; p = pathlib.Path('scripts/guard.py'); "
           "p.write_text(p.read_text().replace('*.yml', '*.yml'))\"")
    assert _run(TEST_GUARD, cmd, repo, plugin) is None
    real = "python3 -c \"import pathlib; pathlib.Path('demo/models/schema.yml').write_text('')\""
    (repo / "demo" / "models").mkdir()
    out = _run(TEST_GUARD, real, repo, plugin)
    assert out and out["permissionDecision"] == "deny"
