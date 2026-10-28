"""M3.3: single-pass redshift workload, heavy-section reuse (TTL / --deep), spill rework. Fictional fixtures only."""
import json
from datetime import datetime, timedelta, timezone

import pytest
import redshift_fixtures as fx
from de_agent.collectors import redshift as rs
from de_agent.collectors.base import CollectorError
from de_agent.config import ConfigError
from de_agent.diff import diff_results
from de_agent.render import render
from de_agent.runner import result_status
from test_redshift_collector import _src, collect, pin_meta

T0 = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
HEAVY_VIEWS = ("sys_query_history", "sys_query_detail", "svl_query_metrics_summary", "stl_wlm_query")


def _scans(log):
    """SQL texts of the heavy queries that were sent."""
    return [s for s, _ in log if any(h in s for h in HEAVY_VIEWS)]


# ---------------------------------------------------------------- derivation

def test_derive_workload_values_and_shapes():
    d = rs.derive_workload(fx.single_pass_rows(), 15)
    assert d["totals"] == {"queries": 959, "exec_s": 2201.0, "queue_p50_s": 0.01, "queue_p95_s": 0.01}
    assert set(d["totals"]) == set(rs.TYPES_TOTALS)
    assert [(u["user_id"], u["query_count"], u["exec_s"]) for u in d["users"]] == [
        (102, 900, 1200.0), (101, 24, 900.0), (103, 35, 101.0)]
    assert all(set(u) == set(rs.TYPES_USERS) for u in d["users"])
    assert [(t["query_type"], t["query_count"], t["exec_s"]) for t in d["types"]] == [
        ("SELECT", 905, 1201.0), ("INSERT", 24, 900.0), ("COPY", 30, 100.0)]
    assert all(set(t) == {"query_type", "query_count", "exec_s"} for t in d["types"])
    assert [(t["qhash"], t["n"], t["total_exec_s"], t["avg_exec_s"], t["max_exec_s"]) for t in d["top"]] == [
        (fx.H_ORDERS, 900, 1200.0, 1.33, 20.5), (fx.H_FCT, 24, 900.0, 37.5, 80.0),
        (fx.H_MISC, 30, 100.0, 3.33, 10.0), ("", 5, 1.0, 0.2, 0.5)]
    assert d["concurrency"] == [{"hour": "2026-10-19 10:00:00", "peak": 1}, {"hour": "2026-10-19 11:00:00", "peak": 3}]
    assert d["min_query_id"] == 5000


def test_derive_workload_top_n_and_empty_input():
    assert len(rs.derive_workload(fx.single_pass_rows(), 2)["top"]) == 2
    d = rs.derive_workload([], 15)
    assert d["totals"] == {"queries": 0, "exec_s": 0.0, "queue_p50_s": 0.0, "queue_p95_s": 0.0}
    assert d["users"] == d["types"] == d["top"] == d["concurrency"] == [] and d["min_query_id"] is None


def test_weighted_percentile():
    assert rs._weighted_pct([(1.0, 1), (9.0, 1)], 0.5) == 1.0
    assert rs._weighted_pct([(0.0, 90), (30.0, 10)], 0.95) == 30.0
    assert rs._weighted_pct([(0.0, 90), (30.0, 10)], 0.9) == 0.0


def test_text_normalization_strips_literals():
    assert rs._norm_text("select  a\nfrom t where id = 42 and s = 'secret value'") == "select a from t where id = ? and s = ?"


# ---------------------------------------------------------------- collector: one scan, same output shapes

def test_single_pass_sends_one_history_scan_plus_one_text_lookup_and_keeps_every_section():
    log = []
    res = collect(fx.single_pass(), log)
    assert res["errors"] == [], res["errors"]
    d = res["data"]
    sent = _scans(log)
    assert sum(s == rs.v_single_pass().sql for s in sent) == 1
    legacy = [v.sql for v in rs.v_users() + rs.v_types() + rs.v_totals() + rs.v_concurrency()]
    assert not any(s in legacy for s in sent)
    assert d["workload_24h"]["queries"] == 959
    assert [u["user"] for u in d["workload_users"]] == ["user#102", "user#101", "user#103"]  # no pg_user answer: ids
    assert d["workload_users"][0]["share_pct"] == 54.5
    assert d["top_queries"][0]["query"] == "select o.id from acme.orders o where o.id = ? and o.status = ?"
    assert d["top_queries"][2]["query"] == "(text unavailable)"
    assert [r["peak"] for r in d["concurrency_24h"]] == [1, 3]
    assert d["pressure_24h"]["spilled_queries"] == 0
    assert d["meta"]["min_query_id"] == 5000
    json.dumps(res)
    meta = d["meta"]["sections"]
    assert all(meta[n]["source"] == "sys_query_history" and meta[n]["single_pass"] for n in rs.WORKLOAD_SECTIONS)


def test_single_pass_output_shapes_equal_the_legacy_sections():
    new, old = collect(fx.single_pass())["data"], collect(fx.healthy())["data"]
    for name in rs.WORKLOAD_SECTIONS:
        assert type(new[name]) is type(old[name]), name
        if isinstance(old[name], list):
            assert {frozenset(r) for r in new[name]} == {frozenset(r) for r in old[name]}, name
        else:
            assert set(new[name]) == set(old[name]), name


def test_single_pass_and_legacy_agree_on_rankings_for_the_same_underlying_workload():
    rows = fx.single_pass_rows()
    a = fx.healthy()
    d = rs.derive_workload(rows, 15)
    a[rs.v_totals()[0].sql] = [dict(d["totals"])]
    a[rs.v_users()[0].sql] = d["users"]
    a[rs.v_types()[0].sql] = d["types"]
    legacy = collect(a)["data"]
    new = collect(fx.single_pass(rows=rows))["data"]
    assert [u["user"] for u in new["workload_users"]] == [u["user"] for u in legacy["workload_users"]]
    assert [t["query_type"] for t in new["workload_types"]] == [t["query_type"] for t in legacy["workload_types"]]


def test_single_pass_rows_are_capped_and_truncation_is_recorded_and_rendered():
    rows = fx.single_pass_rows() * 9000  # 54,000 groups
    res = collect(fx.single_pass(rows=rows))
    assert res["errors"] == [] and res["data"]["meta"]["workload_truncated"] == rs.SINGLE_PASS_CAP
    assert "lower bound" in render(pin_meta(res))
    assert "workload_truncated" not in collect(fx.single_pass())["data"]["meta"]


def test_text_lookup_failure_keeps_top_queries():
    a = fx.single_pass()
    a[rs.v_top_text([fx.H_ORDERS, fx.H_FCT, fx.H_MISC]).sql] = CollectorError("query timed out after 60s")
    res = collect(a)
    assert res["errors"] == [] and res["data"]["top_queries"][0]["query"] == "(text unavailable)"
    assert res["data"]["meta"]["sections"]["top_queries"]["text"] == "failed"
    assert res["data"]["top_queries"][0]["count"] == 900


def test_missing_generic_query_hash_falls_back_to_the_per_section_queries():
    a = fx.healthy()
    a[rs.v_single_pass().sql] = CollectorError('column "generic_query_hash" does not exist')
    log = []
    res = collect(a, log)
    assert res["errors"] == [] and res["data"]["workload_24h"]["queries"] == 99000
    assert rs.v_users()[0].sql in _scans(log)
    assert res["data"]["meta"]["sections"]["workload_users"].get("single_pass") is None


def test_single_pass_timeout_is_reported_per_section_and_not_retried_as_five_scans():
    a = fx.healthy()
    a[rs.v_single_pass().sql] = CollectorError("Query (1) cancelled on user's request")
    log = []
    res = collect(a, log)
    assert result_status(res) == "partial"
    assert [e.split(":")[0] for e in res["errors"] if "single pass" in e] == list(rs.WORKLOAD_SECTIONS)
    legacy = [v.sql for v in rs.v_users() + rs.v_types() + rs.v_totals()]
    assert not any(s in legacy for s in _scans(log))
    assert "workload_users" not in res["data"] and "tables" in res["data"]


# ---------------------------------------------------------------- spill rework

def test_spill_is_bounded_by_the_first_query_id_of_the_window_and_keeps_the_svl_fallback():
    v = rs.v_spill(5000)
    assert [x.source for x in v] == ["svl_query_metrics_summary", "sys_query_detail"]
    assert "m.query >= 5000" in v[0].sql and "stl_query" not in v[0].sql and v[0].timeout is None
    assert "d.query_id >= 5000" in v[1].sql and "start_time" not in v[1].sql and v[1].timeout == rs.SPILL_DETAIL_TIMEOUT_S
    a = fx.single_pass()
    a[v[0].sql] = CollectorError('relation "svl_query_metrics_summary" does not exist')  # serverless
    a[v[1].sql] = [{"spilled_queries": 3, "spilled_mb": 4096}]
    res = collect(a)
    assert res["errors"] == [] and res["data"]["pressure_24h"]["spilled_mb"] == 4096
    info = res["data"]["meta"]["sections"]["pressure_spill"]
    assert info["source"] == "sys_query_detail" and info["fallback_from"] == ["svl_query_metrics_summary"]


def test_spill_detail_fallback_gets_a_shorter_timeout_than_the_section_cap():
    a = fx.single_pass()
    a[rs.v_spill(5000)[0].sql] = CollectorError("permission denied for relation svl_query_metrics_summary")
    a[rs.v_spill(5000)[1].sql] = [{"spilled_queries": 0, "spilled_mb": 0}]
    log = []
    collect(a, log)
    assert [t for s, t in log if s == rs.v_spill(5000)[0].sql] == [rs.DEFAULT_HEAVY_TIMEOUT_S]
    assert [t for s, t in log if s == rs.v_spill(5000)[1].sql] == [rs.SPILL_DETAIL_TIMEOUT_S]
    log.clear()
    collect(a, log, source=_src(options="{heavy_section_timeout_s: 10}"))
    assert [t for s, t in log if s == rs.v_spill(5000)[1].sql] == [10]


def test_spill_without_a_known_bound_uses_the_unbounded_queries():
    assert "start_time" in rs.v_spill()[0].sql and rs.v_spill()[0].timeout is None


# ---------------------------------------------------------------- heavy-section TTL / --deep

def _first(**kw):
    return collect(fx.single_pass(), now=T0, **kw)


def _again(prior, hours, log=None, **kw):
    return collect(fx.single_pass(), log, now=T0 + timedelta(hours=hours), prior=prior, **kw)


def test_heavy_sections_younger_than_the_ttl_are_reused_without_any_heavy_query():
    first = _first()
    log = []
    second = _again(first, 2, log)
    assert _scans(log) == [], _scans(log)  # no workload, spill or concurrency scan at all
    assert second["errors"] == []
    for n in rs.WORKLOAD_SECTIONS + ("pressure_spill",):
        info = second["data"]["meta"]["sections"][n]
        assert info["cached_from"] == first["data"]["meta"]["sections"][n]["computed_at"] and info["cached_age_h"] == 2.0
        assert info["seconds"] == 0
    for n in rs.WORKLOAD_SECTIONS:
        assert second["data"][n] == first["data"][n]
    assert second["data"]["pressure_24h"]["spilled_queries"] == 0 and "commit_queue_p95_s" in second["data"]["pressure_24h"]
    assert second["data"]["meta"]["min_query_id"] == 5000
    assert any(s == rs.q_tables(50)[0].sql for s, _ in log)  # light sections still run
    md = render(pin_meta(second))
    assert "## Workload (24h) (from snapshot 2 h ago)" in md
    assert "### Top queries by total exec time (from snapshot 2 h ago)" in md
    assert "## Pressure (24h) (from snapshot 2 h ago)" in md and "(reused)" in md
    assert "(from snapshot" not in render(pin_meta(first))


def test_reuse_keeps_the_original_computation_time_so_the_cache_cannot_live_forever():
    first = _first()
    second = _again(first, 4)
    late = collect(fx.single_pass(), now=T0 + timedelta(hours=8), prior=second)  # 8 h after the real computation
    assert all("cached_from" not in late["data"]["meta"]["sections"][n] for n in rs.WORKLOAD_SECTIONS)
    ok = collect(fx.single_pass(), now=T0 + timedelta(hours=5), prior=second)  # still under 6 h
    assert ok["data"]["meta"]["sections"]["workload_24h"]["cached_age_h"] == 5.0


def test_expired_ttl_and_deep_recompute_everything():
    first = _first()
    log = []
    expired = _again(first, 6, log)  # exactly the ttl: no longer younger than it
    assert expired["data"]["meta"]["sections"]["workload_24h"].get("cached_from") is None
    assert rs.v_single_pass().sql in _scans(log)
    log.clear()
    deep = _again(first, 1, log, deep=True)
    assert rs.v_single_pass().sql in _scans(log) and rs.v_spill(5000)[0].sql in _scans(log)
    assert not any("cached_from" in v for v in deep["data"]["meta"]["sections"].values())


def test_ttl_option_is_respected_and_validated():
    first = _first()
    second = collect(fx.single_pass(), now=T0 + timedelta(hours=2), prior=first, source=_src(options="{heavy_ttl_h: 1}"))
    assert second["data"]["meta"]["sections"]["workload_24h"].get("cached_from") is None
    assert second["data"]["meta"]["heavy_ttl_h"] == 1.0
    for bad in ("{heavy_ttl_h: 0}", "{heavy_ttl_h: x}"):
        with pytest.raises(ConfigError):
            _src(options=bad)


def test_a_section_that_failed_last_time_is_recomputed():
    first = _first()
    first["data"]["meta"]["sections"]["pressure_spill"] = {"source": None, "seconds": 5}
    first["data"].pop("pressure_24h")
    log = []
    second = _again(first, 1, log)
    assert rs.v_spill(5000)[0].sql in _scans(log)  # spill recomputed ...
    assert rs.v_single_pass().sql not in _scans(log)  # ... the workload family is still reused
    assert second["data"]["meta"]["sections"]["pressure_spill"].get("cached_from") is None
    assert second["data"]["meta"]["sections"]["workload_users"]["cached_from"]


def test_nothing_reused_when_the_cluster_cannot_be_read_at_all():
    res = collect({}, now=T0 + timedelta(hours=1), prior=_first())
    assert result_status(res) == "failed" and "workload_users" not in res["data"]


def test_diff_does_not_report_a_reused_section_as_changed():
    first = _first()
    second = _again(first, 2)
    d = diff_results(first, second)
    assert d["lists"] == {} and d["scalars"] == [] and d["signals_new"] == []
    # even when the reused data differs from the baseline it came from, nothing new was observed there
    second["data"]["workload_24h"] = {**second["data"]["workload_24h"], "queries": 1}
    second["data"]["workload_users"][0]["user"] = "someone_else"
    second["data"]["concurrency_24h"] = []
    d = diff_results(first, second)
    assert d["lists"] == {} and d["scalars"] == []
    # a fresh recomputation that differs IS reported
    rows = fx.single_pass_rows() + [fx._grp(999, "SELECT", "2026-10-19 12:00:00", fx.H_MISC, 5, 1.0, 1.0, 1.0, 0.0, 0.0, 5200)]
    fresh = collect(fx.single_pass(rows=rows), now=T0 + timedelta(hours=9), prior=first)
    dd = diff_results(first, fresh)
    assert "2026-10-19 12:00:00" in [r["key"] for r in dd["lists"]["concurrency_24h"]["added"]]


def test_reused_spill_fields_are_excluded_from_the_diff():
    first = _first()
    second = _again(first, 1)
    assert rs.RedshiftCollector.reused_data(second["data"])[1] == {"pressure_24h": {"spilled_queries", "spilled_mb"}}
    assert rs.RedshiftCollector.reused_data(first["data"]) == (set(), {})


# ---------------------------------------------------------------- snapshot / cli plumbing

def test_take_snapshot_reuses_by_default_and_deep_forces_recompute(tmp_path):
    from de_agent.config import parse_config
    from de_agent.snapshot import list_snapshots, load_result, take_snapshot

    hub = tmp_path / "hub"
    hub.mkdir()
    cfg = parse_config("sources:\n  - {name: dwh, type: redshift, timeout_seconds: 600, "
                       "conn: {host: h, port: 5439, dbname: dev, user: ro, password: pgpass}}\n")
    log = []
    fn = fx.fake_query(fx.single_pass())

    def logged(conn, sql, ex, t):
        log.append(sql)
        return fn(conn, sql, ex, t)

    take_snapshot(hub, cfg, query_fn=logged)
    assert rs.v_single_pass().sql in log
    log.clear()
    take_snapshot(hub, cfg, query_fn=logged)
    assert rs.v_single_pass().sql not in log
    s2 = load_result(hub, list_snapshots(hub)[-1], "dwh")
    assert s2["data"]["meta"]["sections"]["workload_24h"]["cached_from"]
    assert "(from snapshot" in (hub / "platform" / "dwh.md").read_text()
    log.clear()
    take_snapshot(hub, cfg, query_fn=logged, deep=True)
    assert rs.v_single_pass().sql in log
    secs = load_result(hub, list_snapshots(hub)[-1], "dwh")["data"]["meta"]["sections"]
    assert not any("cached_from" in v for v in secs.values())


def test_dea_snapshot_deep_flag_reaches_take_snapshot(tmp_path, monkeypatch):
    from de_agent import snapshot as snap
    from de_agent.cli import app
    from typer.testing import CliRunner

    seen = {}

    def fake(hub, cfg, only=None, do_render=True, query_fn=None, now=None, deep=False):
        seen["deep"] = deep
        return snap.SnapshotOutcome("20261019T120000Z", [], [])

    monkeypatch.setattr(snap, "take_snapshot", fake)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "de-agent.yaml").write_text(
        "version: 1\nhub: {name: t}\nsources:\n  - {name: dwh, type: redshift, "
        "conn: {host: h, port: 5439, dbname: dev, user: ro, password: pgpass}}\n")
    for args, want in ((["snapshot"], False), (["snapshot", "--deep"], True)):
        seen.clear()
        r = CliRunner().invoke(app, args)
        assert r.exit_code == 0, r.output
        assert seen["deep"] is want


def test_spill_bound_comes_from_a_cheap_probe_when_the_single_pass_did_not_run():
    a = fx.healthy()
    a[rs.v_single_pass().sql] = CollectorError("Query (1) cancelled on user's request")
    a[rs.Q_MINQID.sql] = [{"min_query_id": 777}]
    a[rs.v_spill(777)[0].sql] = [{"spilled_queries": 2, "spilled_mb": 10}]
    log = []
    res = collect(a, log)
    assert res["data"]["pressure_24h"]["spilled_queries"] == 2 and res["data"]["meta"]["min_query_id"] == 777
    assert [t for s, t in log if s == rs.Q_MINQID.sql] == [rs.DEFAULT_HEAVY_TIMEOUT_S]
    # probe unavailable (e.g. serverless has no STL views): the unbounded queries are used, nothing fails
    a.pop(rs.Q_MINQID.sql)
    a[rs.v_spill()[0].sql] = [{"spilled_queries": 1, "spilled_mb": 5}]
    res = collect(a)
    assert res["data"]["pressure_24h"]["spilled_queries"] == 1 and "min_query_id" not in res["data"]["meta"]


# ---------------------------------------------------------------- M3.5: heavy-section robustness

def test_single_pass_uses_the_heavy_timeout_and_it_is_configurable():
    log = []
    collect(fx.single_pass(), log, now=T0)
    assert [t for s, t in log if s == rs.v_single_pass().sql] == [300]
    log.clear()
    collect(fx.single_pass(), log, now=T0, source=_src(options="{heavy_section_timeout_s: 500, section_timeout_seconds: 30}"))
    assert [t for s, t in log if s == rs.v_single_pass().sql] == [500]
    for bad in ("{heavy_section_timeout_s: 0}", "{heavy_section_timeout_s: x}", "{light: 1}"):
        with pytest.raises(ConfigError):
            _src(options=bad)


def test_spill_probe_uses_the_heavy_timeout_and_is_skipped_when_single_pass_gave_the_bound():
    a = fx.healthy()
    a[rs.v_single_pass().sql] = CollectorError("Query (1) cancelled on user's request")
    a[rs.Q_MINQID.sql] = [{"min_query_id": 777}]
    a[rs.v_spill(777)[0].sql] = [{"spilled_queries": 2, "spilled_mb": 10}]
    log = []
    collect(a, log, source=_src(options="{heavy_section_timeout_s: 200}"))
    assert [t for s, t in log if s == rs.Q_MINQID.sql] == [200]
    log.clear()
    collect(fx.single_pass(), log)
    assert not any(s == rs.Q_MINQID.sql for s, _ in log)


def test_light_mode_runs_no_heavy_query_and_renders_skipped():
    log = []
    res = collect(fx.single_pass(), log, source=_src(options="{light: true}"), now=T0)
    assert _scans(log) == [], _scans(log)
    assert res["errors"] == []
    for n in rs.WORKLOAD_SECTIONS + ("pressure_spill",):
        assert res["data"]["meta"]["sections"][n]["skipped"] == "light mode" and n not in res["data"]
    assert "spilled_queries" not in res["data"].get("pressure_24h", {}) and res["data"]["meta"]["light"] is True
    assert "skipped (light mode)" in render(pin_meta(res))
    assert "tables" in res["data"]  # light sections still run
    # a prior with heavy data is not reused (and not scanned) in light mode
    again = collect(fx.single_pass(), log, source=_src(options="{light: true}"), now=T0, prior=_first())
    assert "workload_24h" not in again["data"]


def test_best_prior_grafts_each_heavy_section_from_the_latest_snapshot_where_it_succeeded():
    good = _first()
    mid = _again(good, 1, deep=True)
    # newest snapshot: workload failed, spill fine; older: everything fine
    newest = _again(good, 2, deep=True)
    for n in rs.WORKLOAD_SECTIONS:
        newest["data"].pop(n)
        newest["data"]["meta"]["sections"][n] = {"source": None, "seconds": 5}
    newest["data"]["meta"].pop("min_query_id")
    merged = rs.RedshiftCollector.best_prior([newest, mid, good])
    for n in rs.WORKLOAD_SECTIONS:
        assert merged["data"][n] == mid["data"][n]
        assert merged["data"]["meta"]["sections"][n]["computed_at"] == mid["data"]["meta"]["sections"][n]["computed_at"]
    assert merged["data"]["meta"]["min_query_id"] == 5000
    assert newest["data"]["meta"]["sections"]["workload_24h"]["source"] is None  # inputs untouched
    # spill missing in the newest, present in the older one
    newest["data"]["meta"]["sections"]["pressure_spill"] = {"source": None, "seconds": 5}
    newest["data"].pop("pressure_24h")
    m2 = rs.RedshiftCollector.best_prior([newest, good])
    assert m2["data"]["pressure_24h"]["spilled_queries"] == 0
    assert rs.RedshiftCollector.best_prior([]) is None


def test_take_snapshot_reuses_an_older_good_section_when_the_latest_snapshot_is_partial(tmp_path):
    from de_agent.config import parse_config
    from de_agent.snapshot import take_snapshot

    hub = tmp_path / "hub"
    hub.mkdir()
    cfg = parse_config("sources:\n  - {name: dwh, type: redshift, timeout_seconds: 600, "
                       "conn: {host: h, port: 5439, dbname: dev, user: ro, password: pgpass}}\n")
    take_snapshot(hub, cfg, query_fn=fx.fake_query(fx.single_pass()), now=datetime.now(timezone.utc) - timedelta(minutes=10))
    broken = fx.single_pass()
    broken[rs.v_single_pass().sql] = CollectorError("Query (1) cancelled on user's request")
    take_snapshot(hub, cfg, query_fn=fx.fake_query(broken), deep=True, now=datetime.now(timezone.utc) - timedelta(minutes=5))
    log = []
    fn = fx.fake_query(fx.single_pass())

    def logged(conn, sql, ex, t):
        log.append(sql)
        return fn(conn, sql, ex, t)

    take_snapshot(hub, cfg, query_fn=logged)
    assert rs.v_single_pass().sql not in log  # the older good workload is reused, not re-scanned
