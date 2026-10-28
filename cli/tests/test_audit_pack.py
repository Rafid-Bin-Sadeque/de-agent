"""The Redshift audit pack (skills/audit/references/redshift.md): every query parses as redshift and is a read."""
import re
from pathlib import Path

import pytest
import sqlglot
from de_agent.sqlclass import classify

PACK = Path(__file__).resolve().parents[2] / "skills" / "audit" / "references" / "redshift.md"


def _blocks():
    text = PACK.read_text()
    return [b.strip().replace("<top user>", "acme_etl") for b in re.findall(r"```sql\n(.*?)```", text, re.S)]


def test_pack_has_the_four_sections_and_the_verdict_rules_in_order():
    t = PACK.read_text()
    heads = [ln for ln in t.splitlines() if ln.startswith("## ")]
    assert heads[:4] == ["## R1. Config and infrastructure", "## R2. Workload attribution (7 days)",
                         "## R3. Pressure signals (7 days)", "## R4. Inventory and hygiene"]
    rules = t[t.index("## Verdict rules"):]
    order = [rules.index(k) for k in ("**Bottleneck layer first.**", "**Sizing claims need at least 7 days",
                                      "**Never attribute load by user name alone.**", "**Hardware last.**")]
    assert order == sorted(order)
    for layer in ("orchestration", "fixable waste", "CPU", "memory", "I/O", "connections", "storage"):
        assert layer in rules


@pytest.mark.parametrize("sql", _blocks(), ids=lambda s: s[:40].replace("\n", " "))
def test_every_pack_query_parses_and_is_a_read(sql):
    assert sqlglot.parse(sql, read="redshift")
    assert [s.kind for s in classify(sql, "redshift")] == ["read"], sql


def test_pack_has_no_real_identifiers():
    t = PACK.read_text()
    assert not re.search(r"\b\d{12}\b", t) and "amazonaws.com" not in t and "@" not in t
