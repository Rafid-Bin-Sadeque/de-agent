#!/usr/bin/env python3
"""Replace one case's runs in a result set with the runs of a later, case-only re-run (an addendum).

    cli/.venv/bin/python evals/merge_addendum.py evals/results/<date> <addendum results.json> <CASE> [--note TEXT]

The replaced runs are kept under `superseded` and the addendum is recorded under `addenda`, so the original numbers stay auditable.
Re-render with report.py and compare.py afterwards.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(d: Path, add: Path, case: str, note: str) -> None:
    base = json.loads((d / "results.json").read_text())
    new = json.loads(add.read_text())
    new_runs = [r for r in new["runs"] if r["case"] == case]
    if not new_runs:
        sys.exit(f"no {case} runs in {add}")
    old_runs = [r for r in base["runs"] if r["case"] == case]
    base["runs"] = [r for r in base["runs"] if r["case"] != case] + new_runs
    base.setdefault("superseded", []).extend(old_runs)
    base.setdefault("addenda", []).append({
        "case": case, "note": note, "runs": len(new_runs), "cost_usd": new.get("total_cost_usd"),
        "started": new.get("started"), "finished": new.get("finished"), "replaced_runs": [r["key"] for r in old_runs]})
    base["total_cost_usd"] = round((base.get("total_cost_usd") or 0) + (new.get("total_cost_usd") or 0), 4)
    (d / "results.json").write_text(json.dumps(base, indent=1))
    print(f"replaced {len(old_runs)} {case} run(s) with {len(new_runs)}")


if __name__ == "__main__":
    args = sys.argv[1:]
    note = ""
    if "--note" in args:
        i = args.index("--note")
        note = args[i + 1]
        del args[i:i + 2]
    main(Path(args[0]), Path(args[1]), args[2], note)
