"""v0.3 section 4: routines in platform pages, one page per routine, diff hunks, find."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from de_agent.config import load_config
from de_agent.diff import HUNK_MAX_LINES, build_diff, render_markdown
from de_agent.find import FindIndex, fts5_available
from de_agent.snapshot import take_snapshot
from test_routines_edges import PROC, make_hub, rows

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)


def fake(routine_rows):
    def fn(conn, sql, exec_target, timeout_s):
        return routine_rows if "pg_get_functiondef" in sql else []
    return fn


def snap(hub, routine_rows, minutes=0, render=True):
    cfg = load_config(hub / "de-agent.yaml")
    return take_snapshot(hub, cfg, only=["warehouse"], query_fn=fake(routine_rows), do_render=render,
                         now=T0 + timedelta(minutes=minutes))


def test_summary_and_one_page_per_routine(tmp_path):
    hub = make_hub(tmp_path)
    snap(hub, rows())
    page = (hub / "platform" / "warehouse.md").read_text()
    assert "## Routines" in page and "| schema | procedure | function | view | trigger | total |" in page
    assert "| analytics | 2 | 0 | 1 | 0 | 3 |" in page
    assert "parse coverage: full" in page
    assert "### Changed since the previous snapshot" not in page            # first snapshot: nothing to compare
    d = hub / "platform" / "warehouse" / "routines"
    names = sorted(p.name for p in d.glob("*.md"))
    assert "analytics.refresh_orders.md" in names and "shop.orders_trg.md" in names
    p = (d / "analytics.refresh_orders.md").read_text()
    assert "# procedure analytics.refresh_orders()" in p and "| language | plpgsql |" in p
    assert "- `shop.orders`" in p and "- [analytics.audit](analytics.audit.md)" in p
    assert "```sql\nCREATE OR REPLACE PROCEDURE analytics.refresh_orders()" in p
    assert "none known" in p                                               # no scan yet: callers unknown


def test_second_snapshot_lists_changed_routines_and_removes_stale_pages(tmp_path):
    hub = make_hub(tmp_path)
    snap(hub, rows())
    changed = rows(PROC.replace("count(*)", "count(*) + 1"))[:-1]            # drop one routine, change another
    snap(hub, changed, minutes=5)
    page = (hub / "platform" / "warehouse.md").read_text()
    assert "### Changed since the previous snapshot" in page
    assert "- changed: analytics.refresh_orders() (procedure)" in page and "- removed: s.py() (function)" in page
    assert not (hub / "platform" / "warehouse" / "routines" / "s.py.md").exists()


def test_callers_are_rendered_after_a_scan(tmp_path):
    from de_agent.scanners.run import run_scan

    hub = make_hub(tmp_path)
    snap(hub, rows())
    run_scan(hub, load_config(hub / "de-agent.yaml"))
    snap(hub, rows(), minutes=5)
    p = (hub / "platform" / "warehouse" / "routines" / "analytics.refresh_orders.md").read_text()
    assert "- `dag:daily`" in p


def test_hostile_names_cannot_escape_or_inject(tmp_path):
    hub = make_hub(tmp_path)
    evil = [
        {"schema": "../../etc", "name": "passwd", "kind": "procedure", "args": "()", "language": "sql", "definition": "select 1"},
        {"schema": "s", "name": "x\n# INJECTED\n", "kind": "view", "args": "", "language": "sql", "definition": "select `a`"},
        {"schema": "s", "name": "a/b", "kind": "view", "args": "", "language": "sql", "definition": "select 2"},
        {"schema": "s", "name": "a_b", "kind": "view", "args": "", "language": "sql", "definition": "select 3"},
        {"schema": "s", "name": "|--|", "kind": "view", "args": "", "language": "sql", "definition": "```\n## FENCE BREAK\n```"},
    ]
    snap(hub, evil)
    root = hub / "platform" / "warehouse" / "routines"
    files = list(root.glob("*.md"))
    assert len(files) == 5 and all(f.resolve().parent == root.resolve() for f in files)
    assert not (hub / "etc").exists() and not (tmp_path / "etc").exists()
    assert all(p.resolve().is_relative_to(root.resolve()) for p in hub.rglob("*passwd*.md"))
    for f in files:
        text = f.read_text()
        assert "\n# INJECTED" not in text
    fence = next(f.read_text() for f in files if "FENCE BREAK" in f.read_text())
    assert "````sql\n```\n## FENCE BREAK\n```\n````" in fence                    # a longer fence wraps the hostile body
    main = (hub / "platform" / "warehouse.md").read_text()
    assert "\n# INJECTED" not in main and "\n## INJECTED" not in main


def test_overloads_and_collisions_get_distinct_pages(tmp_path):
    hub = make_hub(tmp_path)
    ov = [{"schema": "s", "name": "f", "kind": "function", "args": "(integer)", "language": "sql", "definition": "select 1"},
          {"schema": "s", "name": "f", "kind": "function", "args": "(text)", "language": "sql", "definition": "select 2"}]
    snap(hub, ov)
    names = sorted(p.name for p in (hub / "platform" / "warehouse" / "routines").glob("*.md"))
    assert len(names) == 2 and all(n.startswith("s.f~") for n in names)


# ---------------- diff ----------------

def test_diff_shows_the_changed_hunk(tmp_path):
    hub = make_hub(tmp_path)
    snap(hub, rows(), render=False)
    snap(hub, rows(PROC.replace("count(*)", "count(*) + 1")), minutes=5, render=False)
    report = build_diff(hub, "warehouse")
    ch = report["sources"]["warehouse"]["lists"]["routines"]["changed"]
    assert [c["key"] for c in ch] == ["analytics.refresh_orders.()"]
    assert [c["field"] for c in ch[0]["changes"]] == ["body_hash"] and ch[0]["changes"][0]["text"].startswith("body changed (")
    assert any(ln.startswith("-  insert into analytics.daily_orders select current_date, count(*) from") for ln in ch[0]["hunk"])
    assert any(ln.startswith("+  insert into analytics.daily_orders select current_date, count(*) + 1") for ln in ch[0]["hunk"])
    md = render_markdown(report)
    assert "- routines analytics.refresh_orders.(): body changed" in md and "```diff\n@@" in md
    json.dumps(report)


def test_diff_hunk_is_capped_and_added_removed_come_free(tmp_path):
    hub = make_hub(tmp_path)
    big_a = "begin\n" + "\n".join(f"  select {i} from a.t;" for i in range(300)) + "\nend;"
    big_b = "begin\n" + "\n".join(f"  select {i + 1000} from a.t;" for i in range(300)) + "\nend;"

    def r(name, body):
        return {"schema": "s", "name": name, "kind": "procedure", "args": "()", "language": "plpgsql", "definition": body}

    snap(hub, [r("p", big_a), r("gone", "select 1")], render=False)
    snap(hub, [r("p", big_b), r("new", "select 1")], minutes=5, render=False)
    d = build_diff(hub, "warehouse")["sources"]["warehouse"]["lists"]["routines"]
    (c,) = d["changed"]
    assert len(c["hunk"]) == HUNK_MAX_LINES + 1 and c["hunk"][-1].startswith("... ") and c["hunk"][-1].endswith("more lines")
    assert [x["key"] for x in d["added"]] == ["s.new.()"] and [x["key"] for x in d["removed"]] == ["s.gone.()"]
    assert "(procedure)" in d["added"][0]["text"]                                   # rename = removed + added


def test_a_body_missing_on_one_side_is_not_a_change(tmp_path):
    hub = make_hub(tmp_path)
    full = rows()
    no_view_text = [dict(r, definition="") if r["kind"] == "view" else r for r in full]   # lock-timeout fallback
    snap(hub, full)
    snap(hub, no_view_text, minutes=5)
    assert "routines" not in build_diff(hub, "warehouse")["sources"]["warehouse"]["lists"]
    assert "- changed:" not in (hub / "platform" / "warehouse.md").read_text()
    snap(hub, full, minutes=10)                                                        # bodies back: still no change
    assert "routines" not in build_diff(hub, "warehouse")["sources"]["warehouse"]["lists"]


def test_unchanged_warehouse_is_not_a_change_and_owner_churn_is_noise(tmp_path):
    hub = make_hub(tmp_path)
    snap(hub, rows(), render=False)
    other = [{**r, "owner": "someone_else"} for r in rows()]
    snap(hub, other, minutes=5, render=False)
    assert build_diff(hub, "warehouse")["sources"]["warehouse"]["status"] == "same"


@pytest.mark.skipif(not fts5_available(), reason="SQLite FTS5 unavailable")
def test_find_picks_up_routine_pages(tmp_path):
    hub = make_hub(tmp_path)
    snap(hub, rows())
    idx = FindIndex(hub)
    idx.refresh()
    hits = idx.search("refresh_orders daily_orders")
    assert any(h.path.endswith("warehouse/routines/analytics.refresh_orders.md") and h.kind == "platform" for h in hits)
    assert any(h.path.endswith("routines/analytics.refresh_orders.md") for h in idx.search("analytics refresh orders"))
    idx.search('"; drop table x; -- NEAR( * OR AND')                               # hostile query text: no crash
