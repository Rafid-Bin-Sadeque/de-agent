import json

import pytest
from de_agent.cli import app
from de_agent.initcmd import init_hub
from typer.testing import CliRunner

runner = CliRunner()


def fake_query(rows_by_needle):
    def fn(conn, sql, exec_target, timeout_s):
        for needle, val in rows_by_needle:
            if needle in sql:
                if isinstance(val, Exception):
                    raise val
                return val(sql) if callable(val) else val
        return []
    return fn


SERVER = {"version": "PostgreSQL 16.1", "uptime_s": 100, "max_connections": 100, "connections_total": 2,
          "connections": {"idle": 2}, "cache_hit_pct": 99.0, "oldest_xid_age": 100}


def orders(dead):
    return {"schema": "shop", "name": "orders", "total_bytes": 400 * 1024 * 1024, "table_bytes": 300 * 1024 * 1024,
            "index_bytes": 100 * 1024 * 1024, "n_live_tup": 300000, "n_dead_tup": dead,
            "dead_pct": round(100.0 * dead / (300000 + dead), 1), "last_autovacuum": None, "last_autoanalyze": None,
            "seq_scan": 1, "idx_scan": 2}


def add_source(hub):
    cfg = hub / "de-agent.yaml"
    cfg.write_text(cfg.read_text().replace("sources: []", (
        "sources:\n  - name: warehouse\n    type: postgres\n"
        "    conn: {host: localhost, port: 5432, dbname: shop, user: readonly_agent, password: pgpass}\n"
        "    options: {schemas: [shop]}")))


def test_init_creates_hub_and_refuses_overwrite(tmp_path):
    hub = tmp_path / "h"
    r = runner.invoke(app, ["init", str(hub), "--name", "Demo"])
    assert r.exit_code == 0 and "created  de-agent.yaml" in r.output
    for rel in ["de-agent.yaml", "map.md", "status.md", "conventions.md", "domains/_template.md",
                "projects/_template.md", "integrations/_template.md", "platform/.gitkeep", ".gitignore", "README.md"]:
        assert (hub / rel).exists(), rel
    assert (hub / ".gitignore").read_text().strip() == ".dea/"
    assert "Demo" in (hub / "map.md").read_text()
    (hub / "map.md").write_text("mine")
    r2 = runner.invoke(app, ["init", str(hub), "--name", "Other"])
    assert "skipped (already exist)" in r2.output and "map.md" in r2.output
    assert (hub / "map.md").read_text() == "mine"


def test_init_templates_follow_architecture(tmp_path):
    init_hub(tmp_path, "Demo")
    prof = (tmp_path / "projects" / "_template.md").read_text()
    for section in ("## Purpose", "## Layout", "## Conventions", "## Connections", "## Gotchas"):
        assert section in prof
    for key in ("owner:", "domain:", "updated:", "repo:"):
        assert key in prof
    m = (tmp_path / "map.md").read_text()
    assert "Domains / Projects" in m and "platform/index.md" in m and "status.md" in m
    st = (tmp_path / "status.md").read_text()
    for h in ("## Dated checks", "## In progress", "## Open issues", "## Done"):
        assert h in st
    assert "sources: []" in (tmp_path / "de-agent.yaml").read_text()


def test_commands_need_a_hub(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for cmd in (["snapshot"], ["diff"], ["lint"], ["find", "x"]):
        r = runner.invoke(app, cmd)
        assert r.exit_code == 2, cmd
        assert "dea init" in r.output and "DEA_HUB" in r.output


def test_snapshot_diff_find_lint_flow(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    init_hub(hub, "Demo", with_example=True)
    add_source(hub)
    monkeypatch.chdir(hub)
    state = {"dead": 1000}
    fake = fake_query([
        ("version()", lambda s: [SERVER]),
        ("pg_stat_user_tables", lambda s: [orders(state["dead"])]),
    ])
    from de_agent import runner as rn
    from de_agent import sqlexec
    monkeypatch.setattr(sqlexec, "run_query", fake)
    monkeypatch.setattr(rn, "run_sources", lambda s, query_fn=None, max_workers=4: [rn.run_source(x, fake) for x in s])

    r = runner.invoke(app, ["snapshot"])
    assert r.exit_code == 0, r.output
    assert "warehouse: partial" in r.output or "warehouse: ok" in r.output
    assert (hub / "platform" / "warehouse.md").exists() and (hub / "platform" / "index.md").exists()
    assert "warehouse" in (hub / "platform" / "index.md").read_text()
    snaps = list((hub / ".dea" / "snapshots").iterdir())
    assert len(snaps) == 1 and (snaps[0] / "warehouse.json").exists()
    assert "shop.orders" in (hub / "platform" / "warehouse.md").read_text()

    state["dead"] = 150000
    assert runner.invoke(app, ["snapshot"]).exit_code == 0
    d = runner.invoke(app, ["diff"])
    assert d.exit_code == 0 and "n_dead_tup 1,000 -> 150,000" in d.output and "What changed since" in d.output
    dj = runner.invoke(app, ["diff", "--json"])
    assert json.loads(dj.output)["sources"]["warehouse"]["status"] == "changed"

    f = runner.invoke(app, ["find", "orders"])
    assert f.exit_code == 0 and "platform/warehouse.md" in f.output and "projects/orders_etl.md" in f.output
    assert runner.invoke(app, ["find", "no_such_term_zzz"]).exit_code == 1

    lt = runner.invoke(app, ["lint"])
    assert lt.exit_code == 0 and "0 error(s)" in lt.output


def test_snapshot_exit_1_when_a_source_fails_and_keeps_others(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    init_hub(hub, "Demo")
    cfg = hub / "de-agent.yaml"
    cfg.write_text(cfg.read_text().replace("sources: []", (
        "sources:\n"
        "  - {name: good, type: postgres, conn: {dbname: shop, user: u}}\n"
        "  - {name: bad, type: postgres, conn: {dbname: baddb, user: u}}")))
    monkeypatch.chdir(hub)
    from de_agent import sqlexec
    from de_agent.collectors.base import CollectorError

    def fn(conn, sql, exec_target, timeout_s):
        if conn.dbname == "baddb":
            raise CollectorError("connection refused")
        return [SERVER] if "version()" in sql else []

    monkeypatch.setattr(sqlexec, "run_query", fn)
    r = runner.invoke(app, ["snapshot"])
    assert r.exit_code == 1
    assert (hub / "platform" / "good.md").exists() and not (hub / "platform" / "bad.md").exists()
    idx = (hub / "platform" / "index.md").read_text()
    assert "| bad | postgres | failed |" in idx and "| good | postgres |" in idx
    snap = next((hub / ".dea" / "snapshots").iterdir())
    assert (snap / "bad.json").exists()


def test_retention_prunes_old_snapshots(tmp_path):
    from datetime import datetime, timedelta, timezone

    from de_agent.config import load_config
    from de_agent.snapshot import list_snapshots, take_snapshot

    hub = tmp_path / "hub"
    init_hub(hub, "Demo")
    add_source(hub)
    cfg = hub / "de-agent.yaml"
    cfg.write_text(cfg.read_text().replace("retention: 30", "retention: 3"))
    conf = load_config(cfg)
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    for i in range(5):
        take_snapshot(hub, conf, query_fn=fake_query([("version()", [SERVER])]), now=t0 + timedelta(hours=i))
    assert list_snapshots(hub) == ["20261001T020000Z", "20261001T030000Z", "20261001T040000Z"]


def test_same_second_snapshots_do_not_collide(tmp_path):
    from datetime import datetime, timezone

    from de_agent.config import load_config
    from de_agent.snapshot import list_snapshots, take_snapshot

    hub = tmp_path / "hub"
    init_hub(hub, "Demo")
    add_source(hub)
    conf = load_config(hub / "de-agent.yaml")
    t = datetime(2026, 10, 1, tzinfo=timezone.utc)
    a = take_snapshot(hub, conf, query_fn=fake_query([("version()", [SERVER])]), now=t)
    b = take_snapshot(hub, conf, query_fn=fake_query([("version()", [SERVER])]), now=t)
    assert a.snapshot_id != b.snapshot_id and len(list_snapshots(hub)) == 2


def test_no_render_writes_json_only(tmp_path):
    from de_agent.config import load_config
    from de_agent.snapshot import take_snapshot

    hub = tmp_path / "hub"
    init_hub(hub, "Demo")
    add_source(hub)
    take_snapshot(hub, load_config(hub / "de-agent.yaml"), do_render=False,
                  query_fn=fake_query([("version()", [SERVER])]))
    assert not (hub / "platform" / "warehouse.md").exists()


def test_snapshot_never_contains_the_secret(tmp_path, monkeypatch):
    from de_agent.config import load_config
    from de_agent.snapshot import take_snapshot

    hub = tmp_path / "hub"
    init_hub(hub, "Demo")
    add_source(hub)
    cfg = hub / "de-agent.yaml"
    cfg.write_text(cfg.read_text().replace("password: pgpass", "password: env:DEA_PW"))
    monkeypatch.setenv("DEA_PW", "very-secret-value")
    take_snapshot(hub, load_config(cfg), query_fn=fake_query([("version()", [SERVER])]))
    for p in hub.rglob("*"):
        if p.is_file() and ".dea/index.db" not in p.as_posix():
            assert "very-secret-value" not in p.read_text(errors="ignore")


def test_unknown_source_flag(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    init_hub(hub, "Demo")
    add_source(hub)
    monkeypatch.chdir(hub)
    r = runner.invoke(app, ["snapshot", "--source", "nope"])
    assert r.exit_code == 2 and "no such source" in r.output


def test_doctor_runs(example_hub, monkeypatch):
    monkeypatch.chdir(example_hub)
    r = runner.invoke(app, ["doctor"])
    assert "hub" in r.output and "python" in r.output and "fts5" in r.output and "hook python" in r.output
    assert r.exit_code in (0, 1)


def test_doctor_reports_unreachable_source(example_hub, monkeypatch):
    add_source(example_hub)
    monkeypatch.chdir(example_hub)
    from de_agent import sqlexec
    from de_agent.collectors.base import CollectorError

    def boom(*a, **k):
        raise CollectorError("connection refused")

    monkeypatch.setattr(sqlexec, "run_query", boom)
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 1 and "✗ source warehouse" in r.output


def test_guard_test_still_works():
    r = runner.invoke(app, ["guard-test"])
    assert r.exit_code == 0 and "passed" in r.output


@pytest.mark.parametrize("cmd", ["snapshot", "diff", "lint", "find", "doctor", "init", "guard-test"])
def test_help_lists_commands(cmd):
    assert cmd in runner.invoke(app, ["--help"]).output


def test_doctor_hook_python_is_not_the_venv_python(example_hub, monkeypatch, tmp_path):
    import os
    import stat

    from de_agent import cli as climod

    venv_bin = tmp_path / "venvbin"
    sys_bin = tmp_path / "sysbin"
    for d in (venv_bin, sys_bin):
        d.mkdir()
        p = d / "python3"
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    monkeypatch.delenv("DEA_HOOK_PYTHON", raising=False)
    monkeypatch.setenv("VIRTUAL_ENV", str(venv_bin.parent / "venvroot"))
    (venv_bin.parent / "venvroot").mkdir()
    os.symlink(venv_bin, venv_bin.parent / "venvroot" / "bin")
    monkeypatch.setenv("PATH", os.pathsep.join([str(venv_bin), str(sys_bin)]))
    assert climod._hook_python() == str(sys_bin / "python3")


def test_doctor_shows_vendored_sqlglot_hint_instead_of_skipping(example_hub, monkeypatch):
    from de_agent import cli as climod

    monkeypatch.chdir(example_hub)
    monkeypatch.setattr(climod, "_plugin_root", lambda: None)
    r = runner.invoke(app, ["doctor"])
    assert "vendored sqlglot" in r.output and "set CLAUDE_PLUGIN_ROOT=<your clone of the plugin> to check" in r.output
