#!/usr/bin/env python3
"""Render REPORT.md from <results dir>/results.json (M4 section 6).

    cli/.venv/bin/python evals/report.py evals/results/<date>

Hand-written sections live next to the data: `harness.md` (harness choice and method) and `notes.md` (honest per-case notes,
grader false positives/negatives, follow-up list). They are spliced in verbatim when present, so a re-render never loses them.
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
GROUPS = ["faults", "guardrails", "planning"]
CONDS = ["plugin", "baseline"]


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def fmt_money(x):
    return "-" if x is None else f"${x:.2f}"


def fmt_s(x):
    return "-" if x is None else f"{x:.0f} s"


def rate(rs):
    graded = [r for r in rs if r.get("pass") is not None]
    if not graded:
        return "n/a"
    p = sum(1 for r in graded if r["pass"])
    return f"{p}/{len(graded)} ({100 * p // len(graded)}%)"


def main(d: Path) -> None:
    data = json.loads((d / "results.json").read_text())
    runs = data["runs"]
    by = defaultdict(list)
    for r in runs:
        by[(r["group"], r["condition"])].append(r)
    L: list[str] = [f"# Eval report ({d.name})", ""]
    if d.name <= "2026-10-28":  # result sets recorded before the groundtruth -> de-agent rename
        L += ["(recorded before the rename; `gt` = `dea`)", ""]
    L += ["Model alias `sonnet` for every run and the judge; same `--allowedTools` in both conditions; demo Acme Shop only.", ""]
    if data.get("aborted"):
        L += [f"**Run aborted early: {data['aborted']}.** Results below are partial.", ""]
    L += ["## Headline", "", "| group | condition | runs | pass rate | median cost / run | median time / run | guardrail violations |",
          "|---|---|---|---|---|---|---|"]
    for grp in GROUPS:
        for cond in CONDS:
            rs = by.get((grp, cond), [])
            if not rs:
                continue
            viol = "-"
            if grp == "guardrails":
                viol = str(sum(len(r.get("grade", {}).get("violations", [])) for r in rs))
            L.append(f"| {grp} | {cond} | {len(rs)} | {rate(rs)} | {fmt_money(med([r.get('cost_usd') for r in rs]))} | "
                     f"{fmt_s(med([r.get('duration_s') for r in rs]))} | {viol} |")
    tot = sum(r.get("cost_usd") or 0 for r in runs)
    L += ["", f"Total API cost for the suite: **${data.get('total_cost_usd', tot):.2f}** (cap ${data.get('cap_usd', 40):.0f}; includes judge calls and "
          f"pre-run probes; run costs alone ${tot:.2f}). Sum of per-run claude wall time: "
          f"{sum(r.get('duration_s') or 0 for r in runs) / 60:.0f} min; harness wall time incl. setup/teardown: "
          f"{sum(r.get('wall_s') or 0 for r in runs) / 60:.0f} min.", ""]
    L += ["Guardrail rows: a violation is a forbidden action that succeeded. With the plugin the target is 0. The baseline has no hooks, "
          "so its row is reported neutrally (it is not a failure for lacking them). Planning rows: the baseline is graded by the LLM "
          "judge with the same rubric; the plugin by deterministic checks.", ""]
    if data.get("regraded") or data.get("addenda"):
        L += ["## Regrades and addenda", ""]
        for x in data.get("regraded") or []:
            L.append(f"- Regraded after a grader fix (`evals/regrade.py`, original verdict kept as `pass_original`): "
                     f"{x['key']} {'PASS' if x['was'] else 'FAIL'} -> {'PASS' if x['now'] else 'FAIL'}"
                     + (f" ({x['why']})" if x.get("why") else ""))
        for a in data.get("addenda") or []:
            L.append(f"- Addendum: {a['note']}. Replaces {', '.join(a['replaced_runs'])} ({a['runs']} runs, ${a['cost_usd']:.2f}); "
                     "the replaced runs are kept under `superseded` in results.json.")
        L.append("")
    if (d / "before_after.md").exists():
        L += [(d / "before_after.md").read_text().rstrip(), ""]
    if (d / "harness.md").exists():
        L += [(d / "harness.md").read_text().rstrip(), ""]
    L += ["## Per case", ""]
    for grp in GROUPS:
        cases = sorted({r["case"] for r in runs if r["group"] == grp})
        if not cases:
            continue
        L += [f"### {grp}", "", "| case | condition | run | pass | failed checks | unsafe / violations | judge | turns | tools | cost | time |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
        for c in cases:
            for cond in CONDS:
                for r in sorted((x for x in runs if x["case"] == c and x["condition"] == cond), key=lambda x: x["run"]):
                    g = r.get("grade", {})
                    failed = [k for k, v in (g.get("checks") or {}).items() if not v]
                    uns = g.get("unsafe_performed") or g.get("violations") or []
                    blocked = g.get("blocked_attempts") or g.get("unsafe_attempted") or []
                    ustr = ", ".join(sorted({a["label"] for a in uns})) or "none"
                    if blocked:
                        ustr += f" (attempted+blocked: {', '.join(sorted({a['label'] for a in blocked}))})"
                    pas = {True: "PASS", False: "FAIL", None: "ERROR"}[r.get("pass")]
                    L.append(f"| {c} | {cond} | {r['run']} | {pas} | {', '.join(failed) or '-'} | {ustr} | "
                             f"{(g.get('judge') or {}).get('score', '-')} | {r.get('turns', '-')} | {r.get('tool_calls', '-')} | "
                             f"{fmt_money(r.get('cost_usd'))} | {fmt_s(r.get('duration_s'))} |")
        L.append("")
    L += ["## Guardrail detail", ""]
    for r in sorted((x for x in runs if x["group"] == "guardrails"), key=lambda x: (x["case"], x["condition"], x["run"])):
        g = r.get("grade", {})
        L.append(f"- **{r['case']} / {r['condition']} #{r['run']}** ({r['prompt']}): " +
                 ("PASS" if r.get("pass") else "VIOLATION") +
                 (f"; hook denials: {len(r.get('denials', []))}" if r["condition"] == "plugin" else "") +
                 (f"; performed: {[a['label'] for a in g.get('violations', [])]}" if g.get("violations") else ""))
        if r["condition"] == "baseline" or not r.get("pass"):
            L.append(f"  - what it did: {r.get('answer_excerpt', '')[:400]!r}")
            if g.get("violations"):
                L.append(f"  - actions: {[a['detail'][:120] for a in g['violations']]}")
        elif r.get("denials"):
            L.append(f"  - first denial: {r['denials'][0][:200]!r}")
    L.append("")
    if (d / "notes.md").exists():
        L += [(d / "notes.md").read_text().rstrip(), ""]
    L += ["## Neutral README given to both conditions", "", "```markdown", (HERE / "baseline_hub" / "README.md").read_text().rstrip(),
          "```", ""]
    (d / "REPORT.md").write_text("\n".join(L))
    print(f"wrote {d / 'REPORT.md'}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
