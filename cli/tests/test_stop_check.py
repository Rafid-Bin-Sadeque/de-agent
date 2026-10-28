import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STOP = ROOT / "scripts" / "stop_check.py"
SESSION = ROOT / "scripts" / "session_context.py"
HOOK_PYTHON = os.environ.get("DEA_HOOK_PYTHON", sys.executable)


def hook(script, hub, payload):
    p = subprocess.run([HOOK_PYTHON, str(script)], input=json.dumps({"cwd": str(hub), **payload}),
                       capture_output=True, text=True, timeout=30, env=dict(os.environ, CLAUDE_PROJECT_DIR=str(hub)))
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout) if p.stdout.strip() else None


def make_hub(tmp_path):
    hub = tmp_path / "hub"
    (hub / ".dea" / "plans").mkdir(parents=True)
    (hub / "de-agent.yaml").write_text("version: 1\nscans:\n  - {name: p, type: sql, path: ../repo}\n")
    (tmp_path / "repo").mkdir()
    return hub


def touch(path, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    os.utime(path, (mtime, mtime))
    if path.parent.parent.name == "snapshots":
        os.utime(path.parent, (mtime, mtime))


def test_blocks_once_when_applied_after_latest_snapshot(tmp_path):
    hub = make_hub(tmp_path)
    now = time.time()
    touch(hub / ".dea" / "snapshots" / "20261027T100000Z" / "wh.json", now - 100)
    touch(hub / ".dea" / "plans" / "fix.applied.json", now - 10)
    out = hook(STOP, hub, {"session_id": "s1"})
    assert out["decision"] == "block" and "dea snapshot" in out["reason"] and "dea diff" in out["reason"] and "fix" in out["reason"]
    assert hook(STOP, hub, {"session_id": "s1"}) is None  # marker: never loops
    touch(hub / ".dea" / "plans" / "fix.applied.json", now + 5)  # a newer apply is a new reason to block
    assert hook(STOP, hub, {"session_id": "s1"})["decision"] == "block"


def test_no_block_when_snapshot_is_newer_or_stop_hook_active(tmp_path):
    hub = make_hub(tmp_path)
    now = time.time()
    touch(hub / ".dea" / "plans" / "fix.applied.json", now - 100)
    touch(hub / ".dea" / "snapshots" / "20261027T100000Z" / "wh.json", now - 10)
    assert hook(STOP, hub, {"session_id": "s1"}) is None
    touch(hub / ".dea" / "plans" / "fix.applied.json", now + 50)
    assert hook(STOP, hub, {"session_id": "s1", "stop_hook_active": True}) is None


def test_rolled_back_plan_also_needs_a_snapshot(tmp_path):
    hub = make_hub(tmp_path)
    touch(hub / ".dea" / "plans" / "fix.rolledback.json", time.time())
    assert hook(STOP, hub, {"session_id": "s1"})["decision"] == "block"


def test_suggests_hub_sync_once_when_scanned_repo_changed(tmp_path):
    hub = make_hub(tmp_path)
    start = hook(SESSION, hub, {"session_id": "sess-1", "hook_event_name": "SessionStart"})  # writes the start marker
    assert start is None or "hookSpecificOutput" in start
    marker = hub / ".dea" / "session" / "sess-1.start"
    assert marker.is_file()
    os.utime(marker, (time.time() - 100, time.time() - 100))
    assert hook(STOP, hub, {"session_id": "sess-1"}) is None  # nothing changed yet
    (tmp_path / "repo" / "m.sql").write_text("select 1;")
    out = hook(STOP, hub, {"session_id": "sess-1"})
    assert out and "decision" not in out and "hub-sync" in out["systemMessage"]
    assert hook(STOP, hub, {"session_id": "sess-1"}) is None  # once per session


def test_stop_check_never_fails_outside_a_hub_or_on_bad_input(tmp_path):
    assert hook(STOP, tmp_path, {}) is None
    p = subprocess.run([HOOK_PYTHON, str(STOP)], input="not json", capture_output=True, text=True, timeout=30)
    assert p.returncode == 0 and p.stdout.strip() == ""


def test_hooks_json_wires_every_script():
    h = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    flat = json.dumps(h)
    for script in ("approval_guard.py", "stop_check.py", "secret_guard.py", "sql_guard.py", "query_log.py", "session_context.py"):
        assert script in flat and (ROOT / "scripts" / script).is_file()
    assert "Stop" in h
    assert any("Write" in e["matcher"] and "approval_guard.py" in json.dumps(e) for e in h["PreToolUse"])


def test_agents_and_skills_follow_the_spec():
    agents = {p.stem: p.read_text() for p in (ROOT / "agents").glob("*.md")}
    assert set(agents) == {"runner", "explorer", "executor"}
    for name, text in agents.items():
        assert len(text.splitlines()) <= 80, name
        fm = text.split("---")[1]
        assert re.search(r"^model:\s*sonnet\s*$", fm, re.M), name  # alias only, never a version
        assert not re.search(r"claude-|opus-|sonnet-\d|\d{8}", fm), name
        assert "hooks:" not in fm and "permissionMode" not in fm, name  # plugin agents ignore these
    assert re.search(r"^tools:\s*Bash, Read, Grep, Glob\s*$", agents["runner"], re.M)
    assert re.search(r"^tools:\s*Read, Grep, Glob, Bash\s*$", agents["explorer"], re.M)
    assert "tools:" not in agents["executor"].split("---")[1]  # all tools
    skills = {p.parent.name: p.read_text() for p in (ROOT / "skills").glob("*/SKILL.md")}
    assert set(skills) == {"hub-init", "onboard-project", "hub-sync", "pipeline-check", "incident-triage", "plan", "audit",
                          "backfill", "schema-change", "new-pipeline"}
    for name, text in skills.items():
        fm = text.split("---")[1]
        assert f"name: {name}" in fm and "description:" in fm
        heads = [m.group(1) for m in re.finditer(r"^## (.+)$", text, re.M)]
        assert heads[0].startswith("When to use") and heads[-1].startswith("Never"), (name, heads)
        assert "Output" in " ".join(heads) and "Steps" in " ".join(heads) and "Inputs" in " ".join(heads), (name, heads)
    assert (ROOT / "skills" / "audit" / "references" / "postgres.md").is_file()
