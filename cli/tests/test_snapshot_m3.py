"""dea snapshot end to end for the new source types (Redshift via an injected query function, AWS under moto)."""
import boto3
import redshift_fixtures as fx
from aws_fixtures import REGION
from de_agent.config import parse_config
from de_agent.diff import build_diff, render_markdown
from de_agent.snapshot import list_snapshots, take_snapshot
from moto import mock_aws


def _cfg():
    return parse_config(
        "sources:\n"
        "  - {name: dwh, type: redshift, timeout_seconds: 600, conn: {host: h, port: 5439, dbname: dev, user: ro, password: pgpass}}\n"
        "  - {name: aws_main, type: aws, aws: {region: us-east-1}, services: {s3: {prefixes: ['s3://acme-lake/bronze/']}, cloudwatch: {alarms: true}}}\n")


@mock_aws
def test_snapshot_writes_pages_index_and_diff_for_redshift_and_aws(tmp_path):
    hub = tmp_path / "hub"
    hub.mkdir()
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket="acme-lake")
    s3.put_object(Bucket="acme-lake", Key="bronze/a.parquet", Body=b"x")
    cfg = _cfg()
    out = take_snapshot(hub, cfg, query_fn=fx.fake_query(fx.healthy()))
    assert out.failed == [] and [r["source"] for r in out.results] == ["dwh", "aws_main"]
    assert (hub / "platform" / "dwh.md").read_text().startswith("---\nsource: dwh\ntype: redshift")
    assert "## S3" in (hub / "platform" / "aws_main.md").read_text()
    index = (hub / "platform" / "index.md").read_text()
    assert "| dwh | redshift | ok |" in index and "| aws_main | aws | ok |" in index
    out2 = take_snapshot(hub, cfg, query_fn=fx.fake_query(fx.degraded()), deep=True)
    assert len(list_snapshots(hub)) == 2 and out2.failed == []
    report = render_markdown(build_diff(hub, "dwh"))
    assert "NEW signal: workload is dominated by acme_etl" in report and "1 blocked session(s)" in report


def test_failed_redshift_snapshot_keeps_the_last_good_page(tmp_path):
    hub = tmp_path / "hub"
    hub.mkdir()
    cfg = parse_config("sources:\n  - {name: dwh, type: redshift, timeout_seconds: 600, "
                       "conn: {host: h, port: 5439, dbname: dev, user: ro, password: pgpass}}\n")
    take_snapshot(hub, cfg, query_fn=fx.fake_query(fx.healthy()))
    good = (hub / "platform" / "dwh.md").read_text()
    out = take_snapshot(hub, cfg, query_fn=fx.fake_query({}))  # every view "missing"
    assert out.failed == ["dwh"] and (hub / "platform" / "dwh.md").read_text() == good
