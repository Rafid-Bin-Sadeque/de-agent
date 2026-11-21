"""v0.3.1 section 3: one parse rule for due dates, shared by the session hook and `dea lint`."""
import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest
from de_agent.lint import status_rows

ROOT = Path(__file__).resolve().parents[2]
TODAY = date(2026, 11, 10)


def _hook():
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("session_context_under_test", ROOT / "scripts" / "session_context.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HOOK = _hook()

# (id, status.md text, expected dates that are open, expected dates that are overdue)
CASES = [
    ("bare-date-table", "| Due | Check | State |\n|---|---|---|\n| 2026-11-09 | a | todo |\n", ["2026-11-09"], ["2026-11-09"]),
    ("when-header", "| When | Thing | Status |\n|---|---|---|\n| 2026-11-10 | a | todo |\n", ["2026-11-10"], []),
    ("bold-date", "| Date | Thing | State |\n|---|---|---|\n| **2026-11-09** | a | todo |\n", ["2026-11-09"], ["2026-11-09"]),
    ("underscore-and-star", "| Due | Thing | State |\n|---|---|---|\n| _2026-11-09_ | a | todo |\n| *2026-11-10* | b | todo |\n",
     ["2026-11-09", "2026-11-10"], ["2026-11-09"]),
    ("tilde-prefix", "| Due | Thing | State |\n|---|---|---|\n| ~2026-11-11 | a | todo |\n", ["2026-11-11"], []),
    ("trailing-text", "| Due | Thing | State |\n|---|---|---|\n| 2026-11-09 (overdue) | a | todo |\n| 2026-11-10 morning | b | doing |\n",
     ["2026-11-09", "2026-11-10"], ["2026-11-09"]),
    ("struck-state-done", "| Due | Thing | State |\n|---|---|---|\n| 2026-11-09 | a | done |\n", [], []),
    ("no-leading-date", "| Due | Thing | State |\n|---|---|---|\n| soon, by 2026-11-09 | a | todo |\n| TBD | b | todo |\n", [], []),
    ("bullets-dated-heading", "## Dated checks\n\n- 2026-11-09 verify load\n* **2026-11-10** check report\n1. ~2026-11-11 later\n",
     ["2026-11-09", "2026-11-10", "2026-11-11"], ["2026-11-09"]),
    ("bullets-due-heading-h3", "### Due soon\n- 2026-11-09 a\n", ["2026-11-09"], ["2026-11-09"]),
    ("bullets-deadlines-heading", "# Deadlines\n- 2026-11-10 a\n", ["2026-11-10"], []),
    ("bullet-struck-done", "## Dated\n- ~~2026-11-09 shipped~~\n- 2026-11-09 open one\n", ["2026-11-09"], ["2026-11-09"]),
    ("bullet-checked-done", "## Due\n- [x] 2026-11-09 shipped\n- [ ] 2026-11-09 open\n", ["2026-11-09"], ["2026-11-09"]),
    ("done-section-ignored", "## Done\n- 2026-11-01 finished thing\n- 2026-11-02 other\n", [], []),
    ("section-ends-at-next-heading", "## Dated\n- 2026-11-10 a\n\n## Recently done\n- 2026-11-09 b\n", ["2026-11-10"], []),
    ("bullet-without-date-ignored", "## Dated\n- check the thing\n- 2026-13-45 bad date\n", [], []),
    # v0.3.3: partial strike covering the date is done; strike not covering the date is still open
    ("bullet-partial-strike-done",
     "## Dated\n- ~~**2026-11-08 verify** thing~~ outcome text\n- 2026-11-09 open\n",
     ["2026-11-09"], ["2026-11-09"]),
    ("bullet-strike-not-on-date",
     "## Dated\n- **2026-11-08 task** ~~old note~~\n",
     ["2026-11-08"], ["2026-11-08"]),
]


def _hook_open(text):
    out = HOOK.due_rows(text, TODAY, horizon_days=2)
    return out


@pytest.mark.parametrize("cid,text,open_dates,overdue", CASES, ids=[c[0] for c in CASES])
def test_lint_status_rows(cid, text, open_dates, overdue):
    rows = status_rows(text, TODAY)
    assert sorted(str(r["due"]) for r in rows if not r["done"]) == sorted(open_dates)
    assert sorted(str(r["due"]) for r in rows if r["overdue"]) == sorted(overdue)


@pytest.mark.parametrize("cid,text,open_dates,overdue", CASES, ids=[c[0] for c in CASES])
def test_hook_due_rows(cid, text, open_dates, overdue):
    out = _hook_open(text)
    assert len(out) == len(open_dates)  # every open case here is within today+2
    assert sum(1 for o in out if o.startswith("- [OVERDUE]")) == len(overdue)


def test_bullet_cells_are_first_100_chars():
    long = "2026-11-09 " + "x" * 200
    rows = status_rows(f"## Dated\n- {long}\n", TODAY)
    assert rows[0]["cells"] == [long[:100]] and rows[0]["state"] == ""
    done = status_rows("## Dated\n- [x] 2026-11-09 a\n", TODAY)
    assert done[0]["state"] == "done"


def test_hook_horizon_unchanged():
    text = "## Dated\n- 2026-11-12 in range\n- 2026-11-13 out of range\n"
    assert len(HOOK.due_rows(text, TODAY)) == 1
