from pathlib import Path

import pytest
import yaml
from de_agent.sqlclass import classify

CASES = yaml.safe_load((Path(__file__).parents[1] / "src" / "de_agent" / "data" / "sql_cases.yaml").read_text())


def test_corpus_size():
    assert len(CASES) >= 150


@pytest.mark.parametrize("case", CASES, ids=[f"{i}:{c['sql'][:40]!r}" for i, c in enumerate(CASES)])
def test_corpus(case):
    got = classify(case["sql"], case.get("dialect", "postgres"))
    want_kind = case["kind"] if isinstance(case["kind"], list) else [case["kind"]]
    assert [s.kind for s in got] == want_kind, [(s.kind, s.type, s.reason) for s in got]
    if "type" in case:
        want_type = case["type"] if isinstance(case["type"], list) else [case["type"]]
        assert [s.type for s in got] == want_type


def test_hash_is_stable_and_normalized():
    a = classify("DELETE   FROM orders WHERE id=1")[0]
    b = classify("delete from orders where id = 1  ;")[0]
    assert a.hash == b.hash and len(a.hash) == 64
    assert a.hash != classify("delete from orders where id = 2")[0].hash


def test_unparseable_fails_closed():
    s = classify("garbage in !!")[0]
    assert s.kind == "write" and "fail closed" in s.reason


def test_empty_input_has_no_statements():
    assert classify("") == [] and classify(" ; ") == []
