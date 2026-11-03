"""`dea pack ...`, doctor, config shape, hub overrides, a real cli pack end to end (fake kubectl on PATH), and the wheel's package data."""
import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from de_agent.cli import app
from de_agent.config import ConfigError, load_config, parse_config
from de_agent.packs.loader import builtin_names, set_hub_hint
from typer.testing import CliRunner

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _reset():
    set_hub_hint(None)
    yield
    set_hub_hint(None)


@pytest.fixture
def hub(tmp_path, monkeypatch):
    h = tmp_path / "hub"
    h.mkdir()
    (h / "de-agent.yaml").write_text("version: 1\n")
    monkeypatch.setenv("DEA_HUB", str(h))
    return h


def invoke(*args):
    return runner.invoke(app, list(args))


def test_pack_list_shows_the_eleven_builtins(hub):
    r = invoke("pack", "list")
    assert r.exit_code == 0
    lines = r.output.strip().splitlines()
    assert lines[0].split() == ["name", "kind", "validated", "source"]
    rows = {ln.split()[0]: ln.split() for ln in lines[1:]}
    assert set(rows) == set(builtin_names()) and len(rows) == 11
    assert rows["mysql"] == ["mysql", "sql", "live", "built-in"]
    assert rows["snowflake"][2] == "fixtures"


def test_hub_pack_overrides_a_builtin_and_adds_new_ones(hub):
    (hub / "packs").mkdir()
    (hub / "packs" / "mysql.yaml").write_text(
        "pack: mysql\nkind: sql\ndialect: mysql\nrequires: {extra: mysql}\nvalidated: fixtures\nsections:\n  - name: one\n    sql: select 1 as a\n")
    (hub / "packs" / "mine.yaml").write_text("pack: mine\nkind: http\nsections:\n  - name: x\n    get: /x\n")
    out = invoke("pack", "list").output
    assert any(ln.split()[:4] == ["mysql", "sql", "fixtures", "hub"] for ln in out.splitlines())
    assert any(ln.split()[:4] == ["mine", "http", "fixtures", "hub"] for ln in out.splitlines())
    shown = invoke("pack", "show", "mysql").output
    assert shown.startswith("# hub:") and "select 1 as a" in shown


def test_pack_show_unknown_pack(hub):
    r = invoke("pack", "show", "nope")
    assert r.exit_code == 1 and "no pack named" in r.output


@pytest.mark.parametrize("kind", ["sql", "cli", "http"])
def test_pack_new_scaffolds_a_pack_that_passes_check(hub, kind):
    r = invoke("pack", "new", "mything", "--kind", kind)
    assert r.exit_code == 0, r.output
    f = hub / "packs" / "mything.yaml"
    assert f.is_file() and "# " in f.read_text()  # commented
    c = invoke("pack", "check", "mything")
    assert c.exit_code == 0 and f"ok   mything ({kind}" in c.output
    again = invoke("pack", "new", "mything", "--kind", kind)
    assert again.exit_code == 1 and "already exists" in again.output
    assert f.read_text().startswith("# Pack scaffold")


def test_pack_new_rejects_bad_names_and_kinds(hub):
    assert invoke("pack", "new", "Bad Name", "--kind", "sql").exit_code == 2
    assert invoke("pack", "new", "../evil", "--kind", "sql").exit_code == 2
    assert invoke("pack", "new", "ok", "--kind", "ftp").exit_code == 2
    assert not (hub / "packs").exists()


def test_pack_new_without_a_hub_writes_to_the_current_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("DEA_HUB", raising=False)
    monkeypatch.chdir(tmp_path)
    r = invoke("pack", "new", "x", "--kind", "sql")
    assert r.exit_code == 0 and (tmp_path / "packs" / "x.yaml").is_file()
    assert invoke("pack", "check", "x").exit_code == 0


def test_pack_check_reports_every_problem_and_exits_1(hub, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("pack: bad\nkind: sql\ndialect: postgres\nsections:\n  - name: a\n    sql: delete from t\n"
                   "  - name: b\n    sql: select 1\n    signals:\n      - rows: \"__import__('os')\"\n        message: x\n")
    r = invoke("pack", "check", str(bad))
    assert r.exit_code == 1
    assert "FAIL" in r.output and "is a write" in r.output and "only these functions may be called" in r.output


def test_pack_check_all_covers_every_builtin(hub):
    r = invoke("pack", "check", "--all")
    assert r.exit_code == 0, r.output
    assert r.output.count("\nok ") + (1 if r.output.startswith("ok ") else 0) == 11


def test_pack_check_unknown(hub):
    assert invoke("pack", "check", "nope").exit_code == 2
    assert invoke("pack", "check").exit_code == 2


# ---------------------------------------------------------------- a real cli pack: fake kubectl on PATH

FAKE_KUBECTL = """#!/bin/sh
# fake kubectl: prints canned JSON; refuses anything that is not `get pods`
if [ "$1" = "get" ] && [ "$2" = "pods" ]; then
  cat "$FAKE_KUBECTL_OUT"
else
  echo "unexpected: $*" >&2; exit 9
fi
"""


@pytest.fixture
def fake_kubectl(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "kubectl"
    exe.write_text(FAKE_KUBECTL)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    out = tmp_path / "out.json"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_KUBECTL_OUT", str(out))

    def set_pods(*phases):
        out.write_text(json.dumps({"items": [{"metadata": {"name": f"p{i}"}, "status": {"phase": ph}} for i, ph in enumerate(phases)]}))

    return set_pods


def cli_hub(hub):
    (hub / "packs").mkdir(exist_ok=True)
    (hub / "packs" / "pods.yaml").write_text(
        "pack: pods\nkind: cli\nrequires: {cli: kubectl}\nparams:\n  namespaces: {type: list, required: true}\n"
        "sections:\n  - name: bad_pods\n    run: [kubectl, get, pods, -n, '{{item}}', -o, json]\n    for_each: namespaces\n    item_as: namespace\n"
        "    select: \"items[?status.phase != 'Running']\"\n    fields: {name: metadata.name, phase: status.phase}\n"
        "    key: [namespace, name]\n    signals:\n      - rows: 'count > 0'\n        message: '{count} pod(s) not running'\n")
    (hub / "de-agent.yaml").write_text("version: 1\nsources:\n  - {name: cluster, type: cli, pack: pods, options: {namespaces: [shop]}}\n")


def test_pack_test_runs_the_source_once_and_prints_page_and_signals(hub, fake_kubectl):
    cli_hub(hub)
    fake_kubectl("Running", "Pending", "Failed")
    r = invoke("pack", "test", "cluster")
    assert r.exit_code == 0, r.output
    assert "| shop | p1 | Pending |" in r.output.replace("  ", " ") or "shop" in r.output and "Pending" in r.output
    assert "signals (1):" in r.output and "2 pod(s) not running" in r.output
    assert not (hub / ".dea").exists()  # a test run writes no snapshot


def test_dea_query_points_pack_sources_to_pack_test(hub):
    (hub / "de-agent.yaml").write_text("version: 1\nsources:\n  - {name: wh, type: sql, pack: postgres-lite, conn: {url: 'postgresql+psycopg://u@h/d'}}\n")
    r = invoke("query", "wh", "select 1")
    assert r.exit_code == 2 and "dea pack test wh" in r.output


def test_pack_test_unknown_or_non_pack_source(hub):
    (hub / "de-agent.yaml").write_text("version: 1\nsources:\n  - {name: wh, type: postgres, conn: {dbname: d, user: u}}\n")
    assert invoke("pack", "test", "wh").exit_code == 2
    assert invoke("pack", "test", "zzz").exit_code == 2


def test_snapshot_and_diff_work_unchanged_for_a_pack_source(hub, fake_kubectl):
    cli_hub(hub)
    fake_kubectl("Running", "Pending")
    assert invoke("snapshot").exit_code == 0
    page = (hub / "platform" / "cluster.md").read_text()
    assert "1 pod(s) not running" in page and "type: cli" in page
    assert "cluster" in (hub / "platform" / "index.md").read_text()
    fake_kubectl("Running", "Pending", "Failed")
    assert invoke("snapshot").exit_code == 0
    d = invoke("diff").output
    assert "NEW signal: 2 pod(s) not running" not in d  # numbers are normalised: same signal, different count
    assert "bad_pods: added shop.p2" in d
    # dea find indexes the generated page
    assert invoke("find", "p2").exit_code == 0


def test_doctor_covers_pack_sources(hub, fake_kubectl, monkeypatch, tmp_path):
    cli_hub(hub)
    r = invoke("doctor")
    assert "source cluster" in r.output and "pack pods ok, `kubectl` found" in r.output
    assert "pack pods" in r.output and "passes pack check" in r.output
    (tmp_path / "empty-path").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))  # no kubectl any more (CI runners ship one in /usr/bin)
    r = invoke("doctor")
    row = next(ln for ln in r.output.splitlines() if "source cluster" in ln)
    assert row.startswith("✗") and "kubectl" in row and "not on PATH" in row


def test_doctor_flags_a_hub_pack_that_fails_check(hub):
    (hub / "packs").mkdir()
    (hub / "packs" / "evil.yaml").write_text("pack: evil\nkind: sql\ndialect: postgres\nsections:\n  - name: a\n    sql: drop table t\n")
    row = next(ln for ln in invoke("doctor").output.splitlines() if "pack evil" in ln)
    assert row.startswith("✗") and "write" in row


def test_doctor_reports_a_missing_sql_driver_with_the_install_hint(hub, monkeypatch):
    (hub / "de-agent.yaml").write_text(
        "version: 1\nsources:\n  - {name: wh, type: sql, pack: snowflake, conn: {url: 'snowflake://u@acct/db'}, options: {warehouses: [W], databases: [D]}}\n")
    import builtins

    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "sqlalchemy":
            raise ImportError("x", name="sqlalchemy")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    row = next(ln for ln in invoke("doctor").output.splitlines() if "source wh" in ln)
    assert row.startswith("✗") and "de-agent[snowflake]" in row


# ---------------------------------------------------------------- config shape

def test_sql_conn_rejects_passwords_in_the_url_and_literal_secrets():
    base = "sources:\n  - {name: s, type: sql, pack: postgres-lite, conn: %s}\n"
    for conn, msg in [
        ("{url: 'postgresql+psycopg://u:hunter2@h/d'}", "password or token inside the url"),
        ("{url: 'postgresql+psycopg://u@h/d?password=x'}", "password or token inside the url"),
        ("{url: 'postgresql+psycopg://u@h/d', password: hunter2}", "secret literal"),
        ("{url: 'nourl'}", "driver://"),
        ("{host: h, dbname: d, user: u}", "url"),
    ]:
        with pytest.raises(ConfigError, match=msg):
            parse_config(base % conn)
    ok = parse_config(base % "{url: 'postgresql+psycopg://u@h/d', password: 'env:PGPW'}")
    assert ok.sources[0].conn.url.startswith("postgresql")


def test_pack_fields_only_on_pack_sources():
    with pytest.raises(ConfigError, match="type sql\\|cli\\|http"):
        parse_config("sources:\n  - {name: p, type: postgres, pack: mysql, conn: {dbname: d, user: u}}\n")
    with pytest.raises(ConfigError, match="not a url"):
        parse_config("sources:\n  - {name: p, type: postgres, conn: {url: 'postgresql://u@h/d'}}\n")


def test_unknown_type_hint_points_to_packs():
    with pytest.raises(ConfigError, match="type: sql\\|cli\\|http plus pack"):
        parse_config("sources:\n  - {name: p, type: snowflake, conn: {url: 'x://u@h/d'}}\n")


def test_hub_local_pack_resolves_through_load_config(hub, fake_kubectl):
    cli_hub(hub)
    cfg = load_config(hub / "de-agent.yaml")
    assert cfg.sources[0].pack == "pods"


def test_a_broken_hub_pack_does_not_break_config_loading(hub):
    (hub / "packs").mkdir()
    (hub / "packs" / "pods.yaml").write_text("pack: pods\nkind: cli\nsections: not-a-list\n")
    (hub / "de-agent.yaml").write_text("version: 1\nsources:\n  - {name: c, type: cli, pack: pods}\n")
    assert load_config(hub / "de-agent.yaml").sources[0].name == "c"
    from de_agent.runner import result_status, run_source

    res = run_source(load_config(hub / "de-agent.yaml").sources[0])
    assert result_status(res) == "failed" and res["errors"]


# ---------------------------------------------------------------- the wheel carries the packs

def test_built_wheel_contains_every_pack_and_lists_them_when_installed(tmp_path):
    try:
        import build  # noqa: F401
    except ImportError:
        pytest.skip("python -m build is not installed")
    out = tmp_path / "dist"
    p = subprocess.run([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(out), str(ROOT / "cli")],
                       capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        pytest.skip("cannot build the wheel offline: " + p.stderr[-200:])
    whl = next(out.glob("de_agent-*.whl"))
    with zipfile.ZipFile(whl) as z:
        names = z.namelist()
        packs = sorted(n for n in names if n.startswith("de_agent/packs/") and n.endswith(".yaml"))
        z.extractall(tmp_path / "site")
    assert {Path(n).stem for n in packs} == set(builtin_names()) and len(packs) == 11
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import de_agent; assert de_agent.__file__.startswith(sys.argv[1]), de_agent.__file__;"
            "from de_agent.cli import app; app(['pack', 'list'])")
    r = subprocess.run([sys.executable, "-I", "-c", code, str(tmp_path / "site")], capture_output=True, text=True,
                       cwd=tmp_path, env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "PYTHONPATH": ""})
    # -I ignores PYTHONPATH; the venv's site-packages still provide the third-party deps
    assert r.returncode == 0, r.stderr[-400:]
    assert "postgres-lite" in r.stdout and "fivetran" in r.stdout and "built-in" in r.stdout
