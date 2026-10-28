import os
import statistics
import time

import pytest
from de_agent.find import FindIndex, find, fts5_available, linear_search, query_terms

pytestmark = pytest.mark.skipif(not fts5_available(), reason="SQLite FTS5 unavailable")


def page(hub, rel, text):
    p = hub / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


@pytest.fixture
def hub(tmp_path):
    page(tmp_path, "projects/orders_etl.md", "# orders_etl\n\nLoads analytics.fct_orders nightly.\nSecond line.\n")
    page(tmp_path, "projects/billing.md", "# billing\n\nWrites analytics.fct_invoices. Mentions orders once.\n")
    page(tmp_path, "platform/wh.md", "---\nsource: wh\n---\n# wh\n\n| table |\n|---|\n| shop.orders |\n")
    page(tmp_path, "map.md", "# map\n\nSee projects.\n")
    return tmp_path


def paths(hits):
    return [h.path for h in hits]


def test_dotted_identifier(hub):
    hits = find(hub, "analytics.fct_orders")
    assert paths(hits)[0] == "projects/orders_etl.md"
    assert hits[0].line == 3 and "analytics.fct_orders" in hits[0].snippet


def test_underscored_identifier_matches_whole_token(hub):
    assert paths(find(hub, "fct_orders")) == ["projects/orders_etl.md"]
    assert paths(find(hub, "fct_invoices")) == ["projects/billing.md"]


def test_schema_prefix_matches(hub):
    assert set(paths(find(hub, "analytics"))) == {"projects/orders_etl.md", "projects/billing.md"}


def test_split_identifier_in_platform_page(hub):
    hits = find(hub, "orders")
    assert "platform/wh.md" in paths(hits)
    plat = next(h for h in hits if h.path == "platform/wh.md")
    assert plat.kind == "platform" and plat.snippet == "| shop.orders |"


def test_title_weighs_more(hub):
    page(hub, "projects/mart.md", "# Orders mart\n\nBuilt weekly.\n")
    page(hub, "projects/noise.md", "# noise\n\nThe orders table is loaded after the customers table; orders are never deleted here.\n")
    for i in range(30):  # bm25 needs a corpus where the term is rare, or every idf is ~0
        page(hub, f"projects/filler_{i}.md", f"# filler {i}\n\nunrelated text number {i}\n")
    assert paths(find(hub, "orders"))[0] == "projects/mart.md"


def test_or_fallback_for_natural_language(hub):
    assert paths(find(hub, "why is billing slow"))[0] == "projects/billing.md"


def test_no_match_and_empty_query(hub):
    assert find(hub, "zzzzqqq") == [] and find(hub, "...") == []


def test_kinds(hub):
    kinds = {h.path: h.kind for h in find(hub, "projects")}
    assert kinds.get("map.md") == "map"


def test_incremental_reindex(hub):
    idx = FindIndex(hub)
    assert idx.refresh() == 4
    assert idx.refresh() == 0
    p = page(hub, "projects/billing.md", "# billing\n\nNow mentions zebra_table.\n")
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert idx.refresh() == 1
    assert paths(idx.search("zebra_table")) == ["projects/billing.md"]
    assert idx.search("fct_invoices") == []
    (hub / "map.md").unlink()
    idx.refresh()
    assert "map.md" not in paths(idx.search("projects"))
    page(hub, "projects/new.md", "# new\nbrand_new_thing\n")
    assert paths(find(hub, "brand_new_thing")) == ["projects/new.md"]


def test_rebuild_flag(hub):
    find(hub, "orders")
    assert FindIndex(hub).refresh(rebuild=True) == 4


def test_hidden_and_state_dirs_not_indexed(hub):
    page(hub, ".dea/snapshots/x.md", "# hidden\nsecretword\n")
    assert find(hub, "secretword") == []


def test_linear_fallback(hub):
    with pytest.warns(UserWarning, match="FTS5"):
        hits = linear_search(hub, "analytics.fct_orders")
    assert hits[0].path == "projects/orders_etl.md"


def test_query_terms():
    assert query_terms("analytics.fct_orders") == ["analytics", "fct_orders"]
    assert query_terms("a-b/c:d") == ["a", "b", "c", "d"]


def test_performance_5k_pages(tmp_path):
    for i in range(5000):
        d = tmp_path / ("projects" if i % 2 else "platform")
        d.mkdir(exist_ok=True)
        (d / f"page_{i}.md").write_text(
            f"# page_{i}\n\nTable analytics.fct_orders_{i % 97} feeds shop_{i % 13}.mart_{i}.\n"
            + "Lorem ipsum dolor sit amet. " * 20 + "\n")
    idx = FindIndex(tmp_path)
    assert idx.refresh() == 5000
    times = []
    for q in ["analytics.fct_orders_5", "mart_4321", "shop_7", "page_100", "fct_orders_42", "lorem"] * 20:
        t = time.perf_counter()
        idx.search(q)
        times.append((time.perf_counter() - t) * 1000)
    times.sort()
    p95 = times[int(len(times) * 0.95) - 1]
    assert p95 < 50, f"p95 {p95:.1f} ms (median {statistics.median(times):.1f})"
    target = tmp_path / "projects" / "page_1.md"
    target.write_text("# page_1\n\nchanged_content_marker\n")
    os.utime(target, (time.time() + 10, time.time() + 10))
    t = time.perf_counter()
    assert idx.refresh() == 1
    ms = (time.perf_counter() - t) * 1000
    assert ms < 200, f"incremental reindex took {ms:.0f} ms"
    assert idx.search("changed_content_marker")[0].path == "projects/page_1.md"
