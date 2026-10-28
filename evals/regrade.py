#!/usr/bin/env python3
"""Re-grade the deterministic checks of an existing results.json from the saved transcripts (no model calls, no new runs).

    cli/.venv/bin/python evals/regrade.py evals/results/<date> [--skip G3,...]

`--skip` leaves cases alone whose definition changed since that result set was produced (the current grader would be wrong for them).

Used after a grader fix: the original verdict is kept as `pass_original`, and runs whose verdict changed are listed in
`regraded` at the top of results.json (REPORT.md lists them). Judge scores and planning verdicts are left untouched.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import grading as G  # noqa: E402

SCRATCH = Path(os.environ.get("DEA_EVAL_SCRATCH", str(Path(tempfile.gettempdir()) / "de-agent-evals" / "m4")))


def regrade_plan(r: dict, case: dict, changed: list) -> None:
    """Re-run the plan-structure checks on the plan file the run wrote (`.dea/plans`, or `.gt/plans` for pre-rename scratch)."""
    hub = SCRATCH / r["key"] / "hub"
    plans = [p for sub in (".dea", ".gt") for p in sorted((hub / sub / "plans").glob("*.md"))] if hub.exists() else []
    plans = [p for p in plans if p.name in (r["grade"].get("plans") or [])]
    if not plans:
        print("no plan file for", r["key"], "(left alone)")
        return
    s = G.grade_plan_structure(plans[-1].read_text(), case["structure"])
    checks = dict(r["grade"]["checks"])
    checks["structure_ok"] = bool(s.get(case["structure"]))
    checks["has_verify"] = s["has_verify"]
    checks["rollback_or_idempotent"] = s["rollback_or_idempotent"]
    checks["no_full_refresh"] = s["no_full_refresh"]
    new = all(checks.values())
    if new != r["pass"]:
        changed.append({"key": r["key"], "was": r["pass"], "now": new,
                        "why": "P1 rubric: chunk by size, not calendar; one step with a stated size/runtime estimate and a verify of each day is accepted"})
        r.setdefault("pass_original", r["pass"])
    r["pass"] = new
    r["grade"]["checks"] = checks
    r["grade"]["chunk_units"] = s.get("chunk_units")
    r["grade"]["single_step_sized"] = s.get("single_step_sized")


def main(d: Path, skip: set[str] = frozenset()) -> None:
    data = json.loads((d / "results.json").read_text())
    cases = {c["id"]: c for c in (yaml.safe_load(p.read_text()) for p in (HERE / "cases").glob("*.yaml"))}
    secrets = []
    for line in (HERE.parent / "demo" / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#") and ("PASSWORD" in line.split("=")[0] or "SECRET" in line.split("=")[0]):
            secrets.append(line.split("=", 1)[1].strip())
    changed = []
    for r in data["runs"]:
        tp = SCRATCH / r["key"] / "transcript.jsonl"
        if r["case"] in skip:
            continue
        if r.get("group") == "planning" and r.get("condition") == "plugin" and cases[r["case"]].get("structure") == "chunked":
            regrade_plan(r, cases[r["case"]], changed)  # P1 rubric: structure re-computed from the saved plan file
            continue
        if r.get("group") not in ("faults", "guardrails") or not tp.exists() or r.get("pass") is None:
            continue
        run = G.parse_stream(tp.read_text())
        case = cases[r["case"]]
        new = G.grade_fault(run, case) if r["group"] == "faults" else G.grade_guardrail(run, case, SCRATCH / r["key"] / "hub", secrets)
        judge = r.get("grade", {}).get("judge")
        if new["pass"] != r["pass"]:
            changed.append({"key": r["key"], "was": r["pass"], "now": new["pass"]})
            r.setdefault("pass_original", r["pass"])
        r["pass"] = new["pass"]
        r["grade"] = {k: v for k, v in new.items() if k != "pass"}
        if judge:
            r["grade"]["judge"] = judge
    data["regraded"] = (data.get("regraded") or []) + changed  # keep earlier grader-fix entries
    (d / "results.json").write_text(json.dumps(data, indent=1))
    print("changed:", changed)


if __name__ == "__main__":
    skip_ids: set[str] = set()
    if "--skip" in sys.argv:
        skip_ids = set(sys.argv[sys.argv.index("--skip") + 1].split(","))
    main(Path(sys.argv[1]), skip_ids)
