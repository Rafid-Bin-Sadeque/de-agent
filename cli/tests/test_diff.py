import copy
import json

from de_agent import diff as D
from de_agent.snapshot import snapshots_dir

MB = 1024 * 1024


def result(**data):
    base = {
        "meta": {"long_query_seconds": 300, "top_tables": 25},
        "server": {"version": "PostgreSQL 16.1", "uptime_s": 5000, "max_connections": 100, "connections_total": 5,
                   "connections": {"idle": 5}, "cache_hit_pct": 99.0, "oldest_xid_age": 1000, "xid_wraparound_pct": 0.0},
        "tables": [], "databases": [], "schemas": [], "unused_indexes": [], "long_queries": [], "blocking": [],
        "replication_slots": [], "settings": [],
    }
    base.update(data)
    return {"source": "wh", "type": "postgres", "schema_version": 1, "collected_at": "2026-10-27T09:00:00Z",
            "duration_s": 1.0, "errors": [], "data": base}


def table(name, total=1000 * MB, live=100000, dead=0, **kw):
    pct = round(100.0 * dead / (live + dead), 1) if live + dead else None
    return {"schema": "shop", "name": name, "total_bytes": total, "n_live_tup": live, "n_dead_tup": dead,
            "dead_pct": pct, "last_autovacuum": None, "seq_scan": 0, "idx_scan": 0, **kw}


def test_added_removed_and_changed_rows():
    old = result(tables=[table("orders"), table("gone")])
    new = result(tables=[table("orders", total=1500 * MB, dead=60000), table("fresh", total=5 * MB)])
    new["collected_at"] = "2026-10-27T11:00:00Z"
    d = D.diff_results(old, new)
    t = d["lists"]["tables"]
    assert [r["key"] for r in t["added"]] == ["shop.fresh"] and "5.0 MB" in t["added"][0]["text"]
    assert [r["key"] for r in t["removed"]] == ["shop.gone"]
    ch = t["changed"][0]
    assert ch["key"] == "shop.orders"
    texts = " ".join(c["text"] for c in ch["changes"])
    assert "total_bytes 1000.0 MB -> 1.5 GB (+50%)" in texts
    assert "n_dead_tup 0 -> 60,000" in texts


def test_threshold_suppression():
    old = result(tables=[table("orders", total=1000 * MB, live=100000, dead=1000)])
    # +50% but only +50 MB: below the 100 MB absolute floor
    small_bytes = result(tables=[table("orders", total=1050 * MB, live=100000, dead=1000)])
    assert D.diff_results(old, small_bytes)["lists"] == {}
    # +5% of 3 GB = big absolute, below 20% relative
    big = result(tables=[table("orders", total=3000 * MB)])
    big2 = result(tables=[table("orders", total=3150 * MB)])
    assert D.diff_results(big, big2)["lists"] == {}
    # counts: +900 (below 1,000 floor) even though +90%
    a = result(tables=[table("orders", live=100000, dead=1000)])
    b = result(tables=[table("orders", live=100000, dead=1900)])
    assert D.diff_results(a, b)["lists"] == {}
    # counts: +1,000 and +100%: reported
    c = result(tables=[table("orders", live=100000, dead=2000)])
    assert D.diff_results(a, c)["lists"]["tables"]["changed"][0]["changes"][0]["field"] == "n_dead_tup"
    # seconds floor is 60
    assert D.numeric_change_significant("duration_s", 10, 50) is False
    assert D.numeric_change_significant("duration_s", 10, 100) is True
    assert D.numeric_change_significant("total_bytes", 100 * MB, 200 * MB) is True
    assert D.numeric_change_significant("dead_pct", 1.0, 90.0) is False


def test_volatile_fields_ignored_and_scalars_compared_directly():
    old = result(tables=[table("orders", seq_scan=1, last_autovacuum="2026-10-01T00:00:00Z")])
    new = result(tables=[table("orders", seq_scan=99999, last_autovacuum="2026-10-27T00:00:00Z")])
    assert D.diff_results(old, new)["lists"] == {}
    n2 = copy.deepcopy(new)
    n2["data"]["server"]["version"] = "PostgreSQL 16.4"
    n2["data"]["server"]["max_connections"] = 200
    sc = {c["field"]: c["text"] for c in D.diff_results(old, n2)["scalars"]}
    assert sc["version"] == "version PostgreSQL 16.1 -> PostgreSQL 16.4"
    assert "max_connections" not in sc  # +100 is below the count floor


def test_restart_detected():
    old, new = result(), result()
    new["data"]["server"]["uptime_s"] = 30
    assert any("restarted" in c["text"] for c in D.diff_results(old, new)["scalars"])


def test_new_and_cleared_signals():
    old = result(tables=[table("orders", live=100000, dead=0)],
                 replication_slots=[{"slot_name": "cdc", "plugin": "pgoutput", "active": False, "lag_bytes": 2 * 1024**3}])
    new = result(tables=[table("orders", live=100000, dead=150000)],
                 replication_slots=[{"slot_name": "cdc", "plugin": "pgoutput", "active": True, "lag_bytes": 10}])
    d = D.diff_results(old, new)
    assert d["signals_new"] == ["table shop.orders 60% dead tuples"]
    assert d["signals_cleared"] == ["slot cdc inactive, lag 2.0 GB"]


def test_persisting_signal_with_changed_number_is_not_new():
    a = result(tables=[table("orders", live=100000, dead=150000)])
    b = result(tables=[table("orders", live=100000, dead=250000)])
    d = D.diff_results(a, b)
    assert d["signals_new"] == [] and d["signals_cleared"] == []


def _write(hub, sid, res):
    d = snapshots_dir(hub) / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "wh.json").write_text(json.dumps(res))


def test_build_diff_markdown_and_no_changes(tmp_path):
    hub = tmp_path
    a = result(tables=[table("orders", live=100000, dead=1000)])
    a["collected_at"] = "2026-10-27T09:00:00Z"
    b = result(tables=[table("orders", live=100000, dead=150000)])
    b["collected_at"] = "2026-10-27T11:00:00Z"
    _write(hub, "20261027T090000Z", a)
    _write(hub, "20261027T110000Z", b)
    rep = D.build_diff(hub)
    md = D.render_markdown(rep)
    assert md.startswith("# What changed since 2026-10-27 09:00 UTC (2.0 h)")
    assert "## wh" in md and "NEW signal: table shop.orders 60% dead tuples" in md
    assert md.index("NEW signal") < md.index("tables shop.orders")
    assert "n_dead_tup 1,000 -> 150,000" in md
    # identical latest two snapshots -> one line
    c = copy.deepcopy(b)
    c["collected_at"] = "2026-10-27T12:00:00Z"
    _write(hub, "20261027T120000Z", c)
    assert D.render_markdown(D.build_diff(hub)) == "No changes since 2026-10-27 11:00 UTC (1.0 h).\n"
    # --since compares against the older snapshot
    since = D.render_markdown(D.build_diff(hub, since="20261027T090000Z"))
    assert "n_dead_tup" in since and "09:00 UTC (3.0 h)" in since


def test_failed_snapshot_is_skipped(tmp_path):
    a = result(tables=[table("orders")])
    _write(tmp_path, "20261027T090000Z", a)
    _write(tmp_path, "20261027T100000Z", result(tables=[table("orders", total=9000 * MB)]))
    failed = {"source": "wh", "type": "postgres", "schema_version": 1, "collected_at": "2026-10-27T11:00:00Z",
              "duration_s": 1, "data": {}, "errors": ["connection refused"]}
    _write(tmp_path, "20261027T110000Z", failed)
    rep = D.build_diff(tmp_path)["sources"]["wh"]
    assert rep["new_id"] == "20261027T100000Z" and rep["status"] == "changed"


def test_single_snapshot_has_no_baseline(tmp_path):
    _write(tmp_path, "20261027T090000Z", result())
    assert "No earlier snapshot" in D.render_markdown(D.build_diff(tmp_path))


def test_errors(tmp_path):
    import pytest

    with pytest.raises(D.DiffError, match="no snapshots"):
        D.build_diff(tmp_path)
    _write(tmp_path, "20261027T090000Z", result())
    with pytest.raises(D.DiffError, match="unknown snapshot"):
        D.build_diff(tmp_path, since="20200101T000000Z")
    with pytest.raises(D.DiffError, match="no snapshot data for source"):
        D.build_diff(tmp_path, source="nope")


# ---------------------------------------------------------------- M1b additions: windows, estimates, hints

def _af(runs, failures=()):
    return {"source": "af", "type": "airflow", "schema_version": 1, "collected_at": "2026-10-27T12:00:00Z", "duration_s": 1,
            "errors": [], "data": {"dags": [], "runs": runs, "failures_24h": list(failures), "import_errors": [],
                                   "datasets": [], "pools": [], "meta": {"collected_at": "2026-10-27T12:00:00Z"}}}


def test_windowed_sections_hide_removed_rows_but_show_added_with_state():
    from de_agent.diff import diff_results as dr

    old = _af([{"dag_id": "d", "run_id": "r1", "state": "success", "duration_s": 5}])
    new = _af([{"dag_id": "d", "run_id": "r2", "state": "failed", "duration_s": 75}])
    d = dr(old, new)["lists"]["runs"]
    assert d["removed"] == [] and d["added"][0]["text"] == "d.r2 (failed, 1 min)"


def test_estimated_sizes_do_not_produce_byte_noise():
    from de_agent.diff import diff_results as dr

    def pg(est, idx):
        data = {"tables": [{"schema": "s", "name": "t", "index_bytes": idx, "total_bytes": 1 << 30, "n_live_tup": 5}],
                "meta": {"sizes_estimated": ["tables"]} if est else {}}
        return {"source": "w", "type": "postgres", "schema_version": 1, "collected_at": "2026-10-27T12:00:00Z",
                "duration_s": 1, "errors": [], "data": data}

    d = dr(pg(False, 500 << 20), pg(True, None))
    assert d["lists"] == {} and d["signals_new"] == ["sizes of tables are estimates: a relation is locked (lock timeout)"]


def test_blocking_row_hint_names_the_blocker():
    from de_agent.diff import _row_hint

    assert _row_hint({"blocked_pid": 1, "blocker_pid": 2, "blocker_query": "LOCK TABLE x", "blocked_query": "insert"}) \
        == " ('LOCK TABLE x' blocks 'insert')"
