#!/usr/bin/env python3
"""Before/after tables between two eval result dirs (M4.1).

    cli/.venv/bin/python evals/compare.py evals/results/2026-10-27 evals/results/2026-10-28 > evals/results/2026-10-28/before_after.md

Cases whose definition changed between the two runs are listed in CHANGED and marked, so they are not compared naively.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

CHANGED = {"P1": "prompt changed (backfill of raw.orders -> ranged rebuild of the incremental mart); grader changed; after = M5 re-run with a seeded late correction",
           "P2": "demo gained a writable warehouse source; grader unchanged",
           "G3": "pre-seeded plan + answer must hand approval to the human; grader changed"}
GROUPS = ["faults", "guardrails", "planning"]
CONDS = ["plugin", "baseline"]


def load(d: Path):
    return json.loads((d / "results.json").read_text())["runs"]


def cell(rs):
    g = [r for r in rs if r.get("pass") is not None]
    return "-" if not g else f"{sum(1 for r in g if r['pass'])}/{len(g)} ({100 * sum(1 for r in g if r['pass']) // len(g)}%)"


def viol(rs):
    return sum(len(r.get("grade", {}).get("violations", [])) for r in rs)


def main(a: Path, b: Path) -> None:
    ra, rb = load(a), load(b)
    L = [f"## Before / after ({a.name} -> {b.name})", "",
         "Run counts differ (before: 2 per fault case and condition, 1 planning run; after: 3 and 2), so compare rates, not counts. "
         "Planning and G3 changed definition (see the marked rows) and are NOT a like-for-like comparison.", "",
         "| group | condition | before | after | guardrail violations before | after |", "|---|---|---|---|---|---|"]
    for grp in GROUPS:
        for cond in CONDS:
            xa = [r for r in ra if r["group"] == grp and r["condition"] == cond]
            xb = [r for r in rb if r["group"] == grp and r["condition"] == cond]
            if not xa and not xb:
                continue
            note = " (definition changed)" if grp == "planning" else ""
            va = viol(xa) if grp == "guardrails" else "-"
            vb = viol(xb) if grp == "guardrails" else "-"
            L.append(f"| {grp}{note} | {cond} | {cell(xa)} | {cell(xb)} | {va} | {vb} |")
    L += ["", "### Per case", "", "| case | condition | before | after | note |", "|---|---|---|---|---|"]
    cases = sorted({r["case"] for r in ra + rb})
    for c in cases:
        for cond in CONDS:
            xa = [r for r in ra if r["case"] == c and r["condition"] == cond]
            xb = [r for r in rb if r["case"] == c and r["condition"] == cond]
            L.append(f"| {c} | {cond} | {cell(xa)} | {cell(xb)} | {'**' + CHANGED[c] + '**' if c in CHANGED else ''} |")
    print("\n".join(L))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
