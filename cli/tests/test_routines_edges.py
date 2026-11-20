"""v0.3 section 3: live routines in the edge graph (parse cache, nodes, calls, resolution order, drift, impact)."""
import json

from de_agent.cli import app
from de_agent.config import load_config
from de_agent.scanners import routines as rt
from de_agent.scanners.edges import build_edges, callers, downstream, find_routines
from de_agent.scanners.run import run_scan
from de_agent.snapshot import take_snapshot
from typer.testing import CliRunner

PROC = """CREATE OR REPLACE PROCEDURE analytics.refresh_orders()
 LANGUAGE plpgsql
AS $procedure$
begin
  delete from analytics.daily_orders where d = current_date;
  insert into analytics.daily_orders select current_date, count(*) from shop.orders;
  call analytics.audit();
end;
$procedure$"""
AUDIT = "CREATE OR REPLACE PROCEDURE analytics.audit()\n LANGUAGE plpgsql\nAS $procedure$\nbegin\n  insert into analytics.audit_log select now();\nend;\n$procedure$"
FUNC = "CREATE OR REPLACE FUNCTION shop.order_total(integer)\n RETURNS numeric\n LANGUAGE sql\nAS $function$ select sum(amount) from shop.orders where customer_id = $1 $function$"
VIEW = " SELECT d, n FROM analytics.daily_orders WHERE n > 0;"
TRIG = "CREATE TRIGGER orders_trg AFTER INSERT ON shop.orders FOR EACH ROW EXECUTE FUNCTION shop.trg_fn()"
TRGFN = "CREATE OR REPLACE FUNCTION shop.trg_fn()\n RETURNS trigger\n LANGUAGE plpgsql\nAS $function$ begin insert into shop.orders_audit select new.id; return new; end $function$"
PYFN = "CREATE FUNCTION s.py() RETURNS int AS $$ return 1 $$ LANGUAGE plpythonu"


def _row(schema, name, kind, definition, args="()", language="plpgsql", **kw):
    return {"schema": schema, "name": name, "kind": kind, "args": args, "language": language, "returns": "", "owner": "o",
            "definition": definition, **kw}


def rows(proc=PROC):
    return [
        _row("analytics", "refresh_orders", "procedure", proc),
        _row("analytics", "audit", "procedure", AUDIT),
        _row("shop", "order_total", "function", FUNC, "(integer)", "sql"),
        _row("analytics", "v_daily", "view", VIEW, "", "sql"),
        _row("shop", "orders_trg", "trigger", TRIG, "on orders", ""),
        _row("shop", "trg_fn", "function", TRGFN, "()", "plpgsql"),
        _row("s", "py", "function", PYFN, "()", "plpythonu"),
    ]


def fake(routine_rows):
    def fn(conn, sql, exec_target, timeout_s):
        return routine_rows if "pg_get_functiondef" in sql else []
    return fn


DAG = '''
from airflow import DAG
from airflow.providers.postgres.operators.postgres import PostgresOperator
with DAG("daily", schedule="@daily") as dag:
    PostgresOperator(task_id="r", postgres_conn_id="wh_conn", sql="CALL analytics.refresh_orders()")
'''

CFG = """
sources:
  - name: warehouse
    type: postgres
    conn: {{dbname: shop, user: ro}}
{second}
scans:
  - name: pipelines
    type: airflow
    path: pipelines
    dialect: postgres
{conns}
  - name: repo_sql
    type: sql
    path: sql
"""


def make_hub(tmp_path, second="", conns="", repo_sql=None):
    hub = tmp_path / "hub"
    (hub / "pipelines" / "dags").mkdir(parents=True)
    (hub / "pipelines" / "dags" / "daily.py").write_text(DAG)
    (hub / "sql").mkdir()
    (hub / "sql" / "keep.sql").write_text("select 1;\n" if repo_sql is None else repo_sql)
    (hub / "de-agent.yaml").write_text(CFG.format(second=second, conns=conns))
    (hub / "platform").mkdir()
    return hub


def snap(hub, routine_rows=None, only=None):
    cfg = load_config(hub / "de-agent.yaml")
    return take_snapshot(hub, cfg, only=only, query_fn=fake(rows() if routine_rows is None else routine_rows), do_render=False)


def scan(hub):
    return run_scan(hub, load_config(hub / "de-agent.yaml"))


def edges_of(hub):
    return json.loads((hub / "catalog" / "edges.json").read_text())


def test_collect_time_parse_fills_the_row_contract(tmp_path):
    hub = make_hub(tmp_path)
    out = snap(hub)
    got = {(r["name"], r["kind"]): r for r in out.results[0]["data"]["routines"]}
    p = got[("refresh_orders", "procedure")]
    assert p["reads"] == ["shop.orders"] and p["writes"] == ["analytics.daily_orders"] and p["calls"] == ["analytics.audit"]
    assert got[("v_daily", "view")]["reads"] == ["analytics.daily_orders"] and got[("v_daily", "view")]["parse"] == "full"
    assert got[("orders_trg", "trigger")]["calls"] == ["shop.trg_fn"]
    py = got[("py", "function")]
    assert py["parse"] == "none" and py["reads"] == [] and py["writes"] == []      # never parse non-SQL as SQL


def test_parse_functions_directly():
    assert rt.parse_routine("function", "javascript", "return 'select * from a.b'", "snowflake")["parse"] == "none"
    assert rt.parse_routine("procedure", "plpgsql", "", "postgres")["parse"] == "none"
    big = "select 1 from a.b;" * 20000
    assert rt.parse_routine("procedure", "sql", big, "postgres")["parse"] == "heuristic"


def test_catalog_reuses_unchanged_routines_by_body_hash(tmp_path, monkeypatch):
    hub = make_hub(tmp_path)
    snap(hub)
    scan(hub)
    cat = json.loads((hub / "catalog" / "routines.json").read_text())
    assert cat["stats"]["parsed"] == 7 and cat["stats"]["reused"] == 0
    calls = []
    real = rt.parse_routine
    monkeypatch.setattr(rt, "parse_routine", lambda *a, **k: calls.append(a) or real(*a, **k))
    scan(hub)
    assert calls == []                                       # nothing re-parsed
    assert json.loads((hub / "catalog" / "routines.json").read_text())["stats"] == {"parsed": 0, "reused": 7}
    snap(hub, rows(PROC.replace("count(*)", "count(*) + 1")))
    calls.clear()
    scan(hub)
    assert len(calls) == 1 and "count(*) + 1" in calls[0][2]  # only the changed body


def test_nodes_edges_and_calls(tmp_path):
    hub = make_hub(tmp_path)
    snap(hub)
    scan(hub)
    e = edges_of(hub)
    t = e["tables"]
    ids = lambda side, tbl: {x["id"] for x in t[tbl][side]}  # noqa: E731
    assert "proc:warehouse:analytics.refresh_orders" in ids("readers", "shop.orders")
    assert "proc:warehouse:analytics.refresh_orders" in ids("writers", "analytics.daily_orders")
    assert "view:warehouse:analytics.v_daily" in ids("readers", "analytics.daily_orders")
    assert "view:warehouse:analytics.v_daily" in ids("writers", "analytics.v_daily")
    assert "function:warehouse:shop.order_total" in ids("readers", "shop.orders")
    w = next(x for x in t["analytics.daily_orders"]["writers"] if x["id"].startswith("proc:"))
    assert w["via"] == "routine" and w["file"] == "warehouse:analytics.refresh_orders"
    pairs = {(c["from"], c["to"], c["via"]) for c in e["calls"]}
    assert ("dag:daily", "proc:warehouse:analytics.refresh_orders", "call") in pairs        # DAG -> routine
    assert ("proc:warehouse:analytics.refresh_orders", "proc:warehouse:analytics.audit", "call") in pairs
    assert ("trigger:warehouse:shop.orders_trg", "function:warehouse:shop.trg_fn", "trigger") in pairs
    # the DAG keeps its table edges through the procedure (compat) and the procedure is a node on the path
    assert any(x["id"] == "dag:daily" and x["via"] == "proc" for x in t["analytics.daily_orders"]["writers"])
    assert e["routines"]["proc:warehouse:analytics.refresh_orders"]["reads"] == ["shop.orders"]


def test_downstream_through_routines_and_callers(tmp_path):
    hub = make_hub(tmp_path)
    snap(hub)
    scan(hub)
    e = edges_of(hub)
    rows_ = downstream(e, "shop.orders", 5)
    readers = {r["reader"] for r in rows_}
    assert "proc:warehouse:analytics.refresh_orders" in readers and "view:warehouse:analytics.v_daily" in readers
    # trigger on shop.orders reaches what its function writes
    assert any(r["reader"] == "trigger:warehouse:shop.orders_trg" for r in rows_)
    assert callers(e, "proc:warehouse:analytics.audit") == ["dag:daily", "proc:warehouse:analytics.refresh_orders"]
    assert find_routines(e, "proc:analytics.refresh_orders") == ["proc:warehouse:analytics.refresh_orders"]
    assert find_routines(e, "proc:warehouse:analytics.audit") == ["proc:warehouse:analytics.audit"]
    assert find_routines(e, "function:analytics.refresh_orders") == []


def test_impact_cli_table_and_proc(tmp_path, monkeypatch):
    hub = make_hub(tmp_path)
    snap(hub)
    scan(hub)
    monkeypatch.chdir(hub)
    monkeypatch.setenv("DEA_HUB", str(hub))
    r = CliRunner().invoke(app, ["impact", "shop.orders"])
    assert r.exit_code == 0, r.output
    assert "proc:warehouse:analytics.refresh_orders" in r.output and "called by dag:daily" in r.output
    r = CliRunner().invoke(app, ["impact", "proc:analytics.refresh_orders"])
    assert r.exit_code == 0, r.output
    assert "reads: shop.orders" in r.output and "writes: analytics.daily_orders" in r.output
    assert "called by:\n  dag:daily" in r.output and "view:warehouse:analytics.v_daily" in r.output
    r = CliRunner().invoke(app, ["impact", "proc:nope.nothing"])
    assert r.exit_code != 0


# ---------------- resolution order (section 3.2) ----------------

def _two_sources(tmp_path, conns):
    second = "  - name: other\n    type: postgres\n    conn: {dbname: other, user: ro}\n"
    hub = make_hub(tmp_path, second=second, conns=conns)
    snap(hub)
    scan(hub)
    return hub


def test_resolution_1_connection_mapping_wins_over_ambiguity(tmp_path):
    hub = _two_sources(tmp_path, "    connections: {wh_conn: other}")
    e = edges_of(hub)
    assert [c["to"] for c in e["calls"] if c["from"] == "dag:daily"] == ["proc:other:analytics.refresh_orders"]
    assert not any(u["name"] == "analytics.refresh_orders" for u in e["unresolved"])


def test_resolution_4_two_live_matches_without_mapping_is_unresolved_with_candidates(tmp_path):
    hub = _two_sources(tmp_path, "")
    e = edges_of(hub)
    u = next(u for u in e["unresolved"] if u["name"] == "analytics.refresh_orders")
    assert u["candidates"] == ["proc:other:analytics.refresh_orders", "proc:warehouse:analytics.refresh_orders"]
    assert not [c for c in e["calls"] if c["from"] == "dag:daily"]


def test_resolution_2_unique_live_match(tmp_path):
    hub = make_hub(tmp_path)
    snap(hub)
    scan(hub)
    assert [c["to"] for c in edges_of(hub)["calls"] if c["from"] == "dag:daily"] == ["proc:warehouse:analytics.refresh_orders"]


def test_resolution_3_repo_match_when_live_is_ambiguous(tmp_path):
    repo = "create procedure analytics.refresh_orders() language plpgsql as $$ begin insert into analytics.daily_orders select 1; end; $$;\n"
    second = "  - name: other\n    type: postgres\n    conn: {dbname: other, user: ro}\n"
    hub = make_hub(tmp_path, second=second, repo_sql=repo)
    snap(hub)
    scan(hub)
    e = edges_of(hub)
    assert not any(u["name"] == "analytics.refresh_orders" for u in e["unresolved"])
    assert any(x["id"] == "dag:daily" and x.get("proc") == "analytics.refresh_orders"
               for x in e["tables"]["analytics.daily_orders"]["writers"])
    assert not [c for c in e["calls"] if c["from"] == "dag:daily"]       # repo match: no live node to point at


def test_config_connections_validation():
    import pytest
    from de_agent.config import ConfigError, parse_config

    with pytest.raises(ConfigError, match="unknown source"):
        parse_config("scans:\n  - {name: a, type: airflow, path: x, connections: {c: nope}}\n")
    with pytest.raises(ConfigError, match="airflow scans"):
        parse_config("sources:\n  - {name: w, type: postgres, conn: {dbname: d, user: u}}\n"
                     "scans:\n  - {name: a, type: sql, path: x, connections: {c: w}}\n")


# ---------------- drift (section 3.3) ----------------

def test_repo_vs_live_drift_signal(tmp_path):
    same = "create or replace procedure analytics.audit() language plpgsql as $procedure$\nbegin\n  insert into analytics.audit_log select now();\nend;\n$procedure$;\n"
    hub = make_hub(tmp_path, repo_sql=same)
    snap(hub)
    out = scan(hub)
    assert not [w for w in out.warnings if "differs" in w]                  # identical inner body: no drift
    changed = same.replace("select now()", "select now(), 1")
    (hub / "sql" / "keep.sql").write_text(changed)
    out = scan(hub)
    msgs = [w for w in out.warnings if "differs" in w]
    assert msgs == ["warehouse: routine analytics.audit differs from keep.sql:1 (live changed outside the repo, or the repo is ahead)"]
    cat = json.loads((hub / "catalog" / "routines.json").read_text())
    assert cat["drift"][0]["routine"] == "analytics.audit"
    assert "## Repo drift" in (hub / "catalog" / "routines.md").read_text()


def test_unqualified_tables_use_the_routine_schema_and_are_flagged_heuristic():
    live = [{"id": "proc:w:app.p", "source": "w", "kind": "procedure", "qname": "app.p", "schema": "app", "args": "()",
             "reads": ["orders"], "writes": ["rollup"], "calls": [], "parse": "full"}]
    e = build_edges([], None, (), routines=live)
    assert e["tables"]["app.orders"]["readers"][0]["heuristic"] is True
    assert "app.rollup" in e["tables"]


def test_temp_tables_a_routine_creates_are_not_platform_tables():
    from de_agent.scanners.routines import parse_routine, temp_tables

    body = """CREATE OR REPLACE PROCEDURE fn.p() LANGUAGE plpgsql AS $$
BEGIN
  DROP TABLE IF EXISTS tmp_logins;
  CREATE TEMP TABLE tmp_logins AS SELECT login FROM api.accounts;
  CREATE TEMPORARY TABLE IF NOT EXISTS "tmp_ts" AS SELECT t.* FROM api.trades t JOIN tmp_logins l USING (login);
  SELECT * INTO TEMP tmp_x FROM tmp_ts;
  INSERT INTO fn.result SELECT * FROM tmp_ts JOIN #rs_tmp USING (login);
END $$;"""
    assert temp_tables(body) == {"tmp_logins", "tmp_ts", "tmp_x"}
    r = parse_routine("procedure", "plpgsql", body, "postgres")
    tables = set(r["reads"]) | set(r["writes"])
    assert {"api.accounts", "api.trades"} <= set(r["reads"]) and "fn.result" in r["writes"]
    assert not any("tmp_" in t or "#" in t for t in tables), tables       # live check 2026-11-08: temp-table phantoms
