import csv
import io
import json
import os
import shutil
import sys
from pathlib import Path

from de_agent.cli import app
from de_agent.initcmd import init_hub
from typer.testing import CliRunner

runner = CliRunner()
GOLDEN = Path(__file__).parent / "golden"

HEADER = "task,schema,table,target_schema,target_table,rule,source_endpoint,target_endpoint"
M1 = {"task": "t1", "schema": "shop", "table": "orders", "target_schema": "bronze", "target_table": "orders",
      "rule": "explicit", "source_endpoint": "src", "target_endpoint": "lake"}
M2 = {"task": "t1", "schema": "shop", "table": "items", "target_schema": "bronze", "target_table": "items_r",
      "rule": "wildcard shop.%", "source_endpoint": "src", "target_endpoint": "lake"}


def make_hub(tmp_path, mappings, truncated=None, two=True):
    hub = tmp_path / "hub"
    init_hub(hub, "Demo")
    cfg = hub / "de-agent.yaml"
    cfg.write_text(cfg.read_text().replace(
        "sources: []", "sources:\n  - {name: aws_main, type: aws, aws: {region: us-east-1}, services: {dms: {}}}"))
    for sid in (["20260101T000000Z", "20260102T000000Z"] if two else ["20260102T000000Z"]):
        d = hub / ".dea" / "snapshots" / sid
        d.mkdir(parents=True)
        data = {} if mappings is None else {"dms_table_mappings": mappings if sid.startswith("20260102") else mappings[:1]}
        if truncated:
            data["meta"] = {"truncated": truncated}
        (d / "aws_main.json").write_text(json.dumps({"source": "aws_main", "type": "aws", "data": data}))
    return hub


def test_export_csv_file_uses_latest_snapshot(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, [M1, M2])
    monkeypatch.setenv("DEA_HUB", str(hub))
    out = tmp_path / "m.csv"
    r = runner.invoke(app, ["export", "dms-mappings", "--csv", str(out)])
    assert r.exit_code == 0
    text = out.read_text()
    assert text.splitlines()[0] == HEADER and "\r" not in text
    rows = list(csv.DictReader(io.StringIO(text)))
    assert [x["table"] for x in rows] == ["orders", "items"] and rows[1]["target_table"] == "items_r"


def test_export_stdout_and_source_filter(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, [M1])
    monkeypatch.setenv("DEA_HUB", str(hub))
    r = runner.invoke(app, ["export", "dms-mappings"])
    assert r.exit_code == 0 and r.stdout.splitlines() == [HEADER, "t1,shop,orders,bronze,orders,explicit,src,lake"]
    assert runner.invoke(app, ["export", "dms-mappings", "--source", "other"]).exit_code == 2


def test_export_missing_section_or_snapshot_exits_2(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, None)
    monkeypatch.setenv("DEA_HUB", str(hub))
    r = runner.invoke(app, ["export", "dms-mappings"])
    assert r.exit_code == 2 and "no dms_table_mappings" in r.output
    shutil.rmtree(hub / ".dea" / "snapshots")
    r = runner.invoke(app, ["export", "dms-mappings"])
    assert r.exit_code == 2 and "no snapshot" in r.output


def test_export_truncated_warns_but_writes(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, [M1], truncated=["dms_table_mappings"], two=False)
    monkeypatch.setenv("DEA_HUB", str(hub))
    r = runner.invoke(app, ["export", "dms-mappings"])
    assert r.exit_code == 0 and "dms_mappings_max" in r.output and HEADER in r.stdout


def _norm(text, hub):
    return text.replace(str(hub), "<HUB>")


def _sched(tmp_path, monkeypatch, *args):
    hub = tmp_path / "hub"
    init_hub(hub, "Demo")
    monkeypatch.setenv("DEA_HUB", str(hub))
    monkeypatch.setenv("PATH", "/fake/bin:/usr/bin")
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/bin/dea")
    before = sorted(p for p in Path(os.environ["HOME"]).rglob("*"))
    files = sorted(p for p in hub.rglob("*"))
    r = runner.invoke(app, ["schedule", "print", *args])
    assert sorted(p for p in Path(os.environ["HOME"]).rglob("*")) == before
    assert sorted(p for p in hub.rglob("*")) == files
    return hub, r


def test_schedule_launchd_golden(tmp_path, monkeypatch):
    hub, r = _sched(tmp_path, monkeypatch, "--os", "macos", "--at", "07:30", "--sources", "dwh,aws_main")
    assert r.exit_code == 0
    assert _norm(r.stdout, hub) == (GOLDEN / "schedule_launchd.plist").read_text()


def test_schedule_cron_golden(tmp_path, monkeypatch):
    hub, r = _sched(tmp_path, monkeypatch, "--os", "linux")
    assert r.exit_code == 0
    assert _norm(r.stdout, hub) == (GOLDEN / "schedule_cron.txt").read_text()


def test_schedule_default_os_follows_platform(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    _, r = _sched(tmp_path, monkeypatch)
    assert r.exit_code == 0 and "crontab -e" in r.stdout and "<plist" not in r.stdout


def test_schedule_bad_at_exits_2(tmp_path, monkeypatch):
    for bad in ("25:00", "6", "06:60", "abc"):
        _, r = _sched(tmp_path, monkeypatch, "--at", bad)
        assert r.exit_code == 2, bad
