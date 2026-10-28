import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HOOK_PYTHON = os.environ.get("DEA_HOOK_PYTHON", sys.executable)
ROOT = Path(__file__).resolve().parents[2]
SESSION = ROOT / "scripts" / "session_context.py"
QUERY_LOG = ROOT / "scripts" / "query_log.py"


def run(script, payload=None, hub=None, cwd=None, env_extra=None, raw=None, home=None):
    env = {k: v for k, v in os.environ.items() if k not in ("DEA_HUB", "CLAUDE_PROJECT_DIR")}
    env["HOME"] = str(home or os.environ.get("TMPDIR") or "/tmp")
    env["CLAUDE_PLUGIN_ROOT"] = str(ROOT)
    if hub:
        env["DEA_HUB"] = str(hub)
    env.update(env_extra or {})
    p = subprocess.run([HOOK_PYTHON, str(script)], input=raw if raw is not None else json.dumps(payload or {}),
                       capture_output=True, text=True, env=env, timeout=30, cwd=str(cwd) if cwd else None)
    assert p.returncode == 0, p.stderr
    return p.stdout


def context(out):
    d = json.loads(out)["hookSpecificOutput"]
    assert d["hookEventName"] == "SessionStart"
    return d["additionalContext"]


def hub_with_status(example_hub, rows):
    s = example_hub / "status.md"
    s.write_text(s.read_text().replace("| Due | Check | State |\n|---|---|---|\n",
                                       "| Due | Check | State |\n|---|---|---|\n" + rows))
    return example_hub


def test_hub_found_returns_map_and_due_rows(example_hub, tmp_path):
    today = date.today()
    rows = (f"| {today - timedelta(days=3)} | verify nightly load | todo |\n"
            f"| {today + timedelta(days=2)} | check the report | doing |\n"
            f"| {today + timedelta(days=3)} | later check | todo |\n"
            f"| {today - timedelta(days=9)} | finished check | done |\n")
    hub_with_status(example_hub, rows)
    text = context(run(SESSION, {"cwd": str(tmp_path)}, hub=example_hub))
    assert "orders_etl" in text                      # map.md
    assert "## Due / overdue" in text
    assert "[OVERDUE]" in text and "verify nightly load" in text
    assert "check the report" in text
    assert "later check" not in text and "finished check" not in text
    assert text.rstrip().endswith("platform/")
    assert "Use `dea find <term>` before searching repos; platform state: " in text
    assert len(text) <= 6000


def test_hub_found_by_walking_up(example_hub):
    sub = example_hub / "projects"
    assert "orders_etl" in context(run(SESSION, {"cwd": str(sub)}, cwd=sub))


def test_stale_platform_lines(example_hub):
    old = (datetime.now(timezone.utc) - timedelta(hours=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (example_hub / "platform" / "wh.md").write_text(f"---\nsource: wh\ngenerated: {old}\n---\n# wh\n")
    (example_hub / "platform" / "index.md").write_text("---\ntype: platform-index\n---\n# i\n")
    text = context(run(SESSION, {}, hub=example_hub))
    assert "## Stale platform data" in text and "platform/wh.md is 60h old (max 24h)" in text
    assert "platform/index.md is" not in text


def test_output_capped_at_6000_chars(example_hub):
    (example_hub / "map.md").write_text("# Map\n" + ("x" * 100 + "\n") * 200)
    text = context(run(SESSION, {}, hub=example_hub))
    assert len(text) <= 6000 and "[map.md truncated]" in text and "dea find" in text


def test_no_hub_is_silent(tmp_path):
    assert run(SESSION, {"cwd": str(tmp_path)}, cwd=tmp_path) == ""


def test_corrupt_inputs_are_silent_exit_0(example_hub, tmp_path):
    assert run(SESSION, raw="{not json", hub=example_hub) != ""        # bad stdin: still injects the map
    assert run(SESSION, raw="", cwd=tmp_path) == ""
    (example_hub / "map.md").write_bytes(b"\xff\xfe\x00 binary \x80")
    (example_hub / "status.md").write_bytes(b"\x00\x01|||\n|---|\n| 2020-01-01 | x")
    run(SESSION, {}, hub=example_hub)                                   # must exit 0 (asserted in run)
    run(SESSION, {}, hub=tmp_path / "does-not-exist")


def test_unreadable_map_does_not_crash(example_hub):
    (example_hub / "map.md").unlink()
    out = run(SESSION, {}, hub=example_hub)
    assert "dea find" in context(out)


def test_user_config_hub_pointer(example_hub, tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "de-agent").mkdir(parents=True)
    (home / ".config" / "de-agent" / "config.yaml").write_text(f"hub: {example_hub}\n")
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert "orders_etl" in context(run(SESSION, {"cwd": str(other)}, cwd=other, home=home))


# ---------------------------------------------------------------- query_log


def log_lines(project):
    p = project / ".dea" / "logs" / "queries.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def bash(cmd, **extra):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": ".", "session_id": "s1", **extra}


def test_query_log_appends_db_sql(tmp_path):
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    out = run(QUERY_LOG, bash('psql -c "select 1; delete from t"', agent_type="de-agent:runner",
                              tool_response={"exit_code": 0}), env_extra=env, cwd=tmp_path)
    assert out == ""
    rows = log_lines(tmp_path)
    assert [r["kind"] for r in rows] == ["read", "write"] and [r["type"] for r in rows] == ["SELECT", "DELETE"]
    r = rows[0]
    assert r["session_id"] == "s1" and r["agent_type"] == "de-agent:runner" and r["source"] == "psql"
    assert r["exit_status"] == 0 and len(r["hash"]) == 64 and r["sql"].lower().startswith("select 1")
    assert r["ts"].endswith("Z")


def test_query_log_ignores_non_db_and_other_tools(tmp_path):
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    run(QUERY_LOG, bash("ls -la"), env_extra=env, cwd=tmp_path)
    run(QUERY_LOG, {"tool_name": "Read", "tool_input": {"file_path": "x"}}, env_extra=env, cwd=tmp_path)
    run(QUERY_LOG, raw="garbage", env_extra=env, cwd=tmp_path)
    assert log_lines(tmp_path) == []


def test_query_log_truncates_sql_and_appends(tmp_path):
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    long_sql = "select " + ", ".join(f"{i} as c{i}" for i in range(200))
    run(QUERY_LOG, bash(f'psql -c "{long_sql}"'), env_extra=env, cwd=tmp_path)
    run(QUERY_LOG, bash('psql -c "select 2"'), env_extra=env, cwd=tmp_path)
    rows = log_lines(tmp_path)
    assert len(rows) == 2 and len(rows[0]["sql"]) <= 300 and rows[0]["exit_status"] is None
