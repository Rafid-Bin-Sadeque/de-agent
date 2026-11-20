"""v0.3 section 2.3: the reserved `routines` pack section (check rules, generic store/redaction in PackCollector)."""
import json

import pytest
from de_agent.collectors import get_collector
from de_agent.collectors.base import RunContext
from de_agent.config import parse_config
from de_agent.packs import check_text
from de_agent.routines import RoutineStore

COLS = "[schema, name, kind, args, language, returns, owner, definition]"


def pack_text(section_extra="", columns=COLS, key="[schema, name, args]", limit=""):
    return (
        "pack: t\nversion: 1\nkind: sql\ndialect: postgres\nrequires: {extra: pg}\nvalidated: fixtures\ndescription: x fictional\n"
        "params: {}\nsections:\n  - name: routines\n    sql: select 1 as schema\n"
        f"    key: {key}\n    columns: {columns}\n{limit}{section_extra}")


def test_valid_routines_section():
    pack, errs = check_text(pack_text())
    assert errs == [] and pack is not None


@pytest.mark.parametrize("kw,needle", [
    ({"key": "[schema, name]"}, "needs `key: [schema, name, args]`"),
    ({"columns": "[schema, name, kind, args, language, returns, owner]"}, "missing column(s): definition"),
    ({"columns": "[schema, name]"}, "missing column(s)"),
])
def test_routines_section_contract_is_enforced(kw, needle):
    _, errs = check_text(pack_text(**kw))
    assert any(needle in e for e in errs), errs


def test_routines_needs_declared_columns():
    text = pack_text().replace(f"    columns: {COLS}\n", "")
    _, errs = check_text(text)
    assert any("must declare `columns:`" in e for e in errs), errs


def test_limit_above_1000_only_for_routines():
    _, errs = check_text(pack_text(limit="    limit: 5000\n"))
    assert errs == []
    other = pack_text().replace("name: routines", "name: other").replace(f"    columns: {COLS}\n", "").replace(
        "key: [schema, name, args]", "key: schema") + "    limit: 5000\n"
    _, errs = check_text(other)
    assert any("limit may not exceed 1000" in e for e in errs), errs


def _src(options="{}"):
    return parse_config(
        "sources:\n  - name: wh\n    type: sql\n    pack: postgres-lite\n    conn: {url: 'postgresql+psycopg://u@h/d'}\n"
        f"    options: {options}\n").sources[0]


def _collect(rows, options="{}", store=None):
    src = _src(options)

    def fn(section, item, sql):
        return rows if section.name == "routines" else []

    ctx = RunContext(timeout_seconds=30, pack_fn=fn, routine_store=store)
    return get_collector("sql").collect(src, ctx)


ROW = {"schema": "s", "name": "p", "kind": "procedure", "args": "()", "language": "plpgsql", "returns": "", "owner": "o",
       "body_bytes": 5, "definition": "begin end"}


def test_collect_hashes_redacts_stores_and_drops_definition(tmp_path):
    store = RoutineStore(tmp_path)
    leaky = {**ROW, "name": "leaky", "definition": "select 'password=hunter2'"}
    out = _collect([ROW, leaky], store=store)
    assert out["errors"] == [], out["errors"]
    rows = {r["name"]: r for r in out["data"]["routines"]}
    assert "definition" not in rows["p"] and store.get(rows["p"]["body_hash"]) == "begin end"
    assert rows["leaky"]["redacted"] and "hunter2" not in store.get(rows["leaky"]["body_hash"])
    assert "hunter2" not in json.dumps(out)
    assert out["data"]["meta"]["list_keys"]["routines"] == ["schema", "name", "args"]
    assert "owner" in out["data"]["meta"]["volatile"]


def test_collect_null_definitions_keep_rows_and_raise_an_info_signal():
    out = _collect([{**ROW, "definition": None}])
    (row,) = out["data"]["routines"]
    assert row["body_hash"] == "" and row["parse"] == "none"
    sig = out["data"]["meta"]["signals"]
    assert any(s["severity"] == "info" and "without a body" in s["text"] for s in sig)


def test_routine_options_are_reserved_and_honoured():
    assert "routines" not in _collect([ROW], "{routines: false}")["data"]
    out = _collect([ROW, {**ROW, "name": "v", "kind": "view"}], "{routine_kinds: [view]}")
    assert [r["name"] for r in out["data"]["routines"]] == ["v"]
    out = _collect([ROW, {**ROW, "name": "v"}], "{routines_max: 1}")
    assert "routines" not in out["data"] and any("routines_max" in e for e in out["errors"])
    big = _collect([{**ROW, "definition": "x" * 5000}], "{routines_max_bytes: 100}")["data"]["routines"][0]
    assert big["truncated"] and big["body_bytes"] == 5000
    with pytest.raises(Exception, match="unknown option"):
        _src("{bogus: 1}")
    with pytest.raises(Exception, match="routines_max"):
        _src("{routines_max: 0}")


def test_limit_of_a_routines_section_follows_routines_max_not_the_pack_limit():
    rows = [{**ROW, "name": f"p{i}"} for i in range(450)]  # more than the default section limit (200)
    out = _collect(rows)
    assert len(out["data"]["routines"]) == 450 and not out["data"]["meta"]["truncated"]
