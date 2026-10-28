"""Scripted walkthrough of demo fault 01 with real `claude -p` runs (M2 section 5). Local only, not CI.

    DEA_DEMO=1 pytest -m walkthrough -s cli/tests/test_walkthrough.py

Needs the demo stack (started if absent), the `claude` CLI, and network access for the model. The hub is a scratch copy
of demo/ (so no parent CLAUDE.md leaks in); the approval key lives in a scratch XDG dir, never in the real ~/.config.
Step 4 (`dea approve`) is the human step, driven through a pty. Result: demo/faults/01_schema_drift/WALKTHROUGH.md.
"""
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from test_demo_e2e import DEMO, ROOT, _env, sh, trigger, wait_run
from test_plans import _pty_run

pytestmark = [
    pytest.mark.walkthrough,
    pytest.mark.skipif(os.environ.get("DEA_DEMO") != "1", reason="set DEA_DEMO=1 to run the walkthrough"),
    pytest.mark.skipif(shutil.which("claude") is None, reason="needs the claude CLI"),
]

PLAN_ID = "fault01-schema-drift"
MODEL = os.environ.get("DEA_WALKTHROUGH_MODEL", "sonnet")
OUT = DEMO / "faults" / "01_schema_drift" / "WALKTHROUGH.md"
DEA = ROOT / "cli" / ".venv" / "bin" / "dea"


class Walk:
    def __init__(self, scratch: Path):
        self.scratch = scratch
        self.hub = scratch / "hub"
        self.xdg = scratch / "xdg"
        self.transcripts = scratch / "transcripts"
        self.transcripts.mkdir(parents=True)
        shutil.copytree(DEMO, self.hub, ignore=shutil.ignore_patterns(".dea", "logs", "__pycache__", ".env", "extra-ca"))
        bindir = scratch / "bin"
        bindir.mkdir()
        (bindir / "dea").symlink_to(DEA)
        env = _env()
        # /usr/bin first: hooks run `python3`, and they must work on the stock 3.9 interpreter
        env["PATH"] = os.pathsep.join([str(bindir), "/usr/bin", "/bin", env.get("PATH", "")])
        env["XDG_CONFIG_HOME"] = str(self.xdg)
        env["DEA_HUB"] = str(self.hub)
        env.pop("CLAUDE_PROJECT_DIR", None)
        self.env = env
        self.steps: list[dict] = []
        self.denials: list[str] = []
        self.session_id: str | None = None
        self.cost = 0.0

    def dea(self, *args, timeout=300, check=True):
        p = subprocess.run([str(DEA), *args], capture_output=True, text=True, env=self.env, cwd=self.hub, timeout=timeout)
        if check and p.returncode != 0:
            raise AssertionError(f"dea {' '.join(args)} -> {p.returncode}\n{p.stdout[-800:]}\n{p.stderr[-800:]}")
        return p

    def claude(self, label: str, prompt: str, resume: bool = False, timeout: int = 1200) -> dict:
        cmd = ["claude", "-p", prompt, "--plugin-dir", str(ROOT), "--output-format", "stream-json", "--verbose",
               "--model", MODEL, "--allowedTools", "Bash", "Read", "Write", "Edit", "Grep", "Glob", "Agent",
               "Skill", "--strict-mcp-config", "--max-budget-usd", "4"]
        if resume and self.session_id:
            cmd += ["--resume", self.session_id]
        t0 = time.time()
        p = subprocess.run(cmd, capture_output=True, text=True, env=self.env, cwd=self.hub, timeout=timeout)
        (self.transcripts / f"{label}.jsonl").write_text(p.stdout)
        (self.transcripts / f"{label}.stderr").write_text(p.stderr)
        result: dict = {}
        for line in p.stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "result":
                result = ev
            self._collect_denials(ev)
        self.session_id = result.get("session_id") or self.session_id
        self.cost += float(result.get("total_cost_usd") or 0)
        return {"text": str(result.get("result") or ""), "turns": result.get("num_turns"),
                "cost": result.get("total_cost_usd"), "seconds": round(time.time() - t0, 1),
                "is_error": result.get("is_error"), "rc": p.returncode, "stderr": p.stderr[-400:]}

    def _collect_denials(self, ev: dict) -> None:
        msg = ev.get("message") or {}
        content = msg.get("content")
        if ev.get("type") != "user" or not isinstance(content, list):
            return
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            body = block.get("content")
            text = " ".join(b.get("text", "") for b in body if isinstance(b, dict)) if isinstance(body, list) else str(body)
            if "de-agent" in text and re.search(r"human-only|executor agent|is a write|secrets must not|guard error|"
                                                   r"not installed|refused", text):
                self.denials.append(text.strip().replace("\n", " ")[:300])

    def step(self, n: int, name: str, ok: bool, detail: str, seconds: float | None = None) -> None:
        self.steps.append({"n": n, "name": name, "ok": ok, "detail": detail, "seconds": seconds})
        print(f"[walkthrough] step {n} {'PASS' if ok else 'FAIL'}: {name} - {detail[:160]}")


def write_report(w: Walk, extras: dict, total_s: float) -> None:
    lines = [
        "# Fault 01 walkthrough (recorded)", "",
        f"Recorded by `DEA_DEMO=1 pytest -m walkthrough` on {datetime.now(timezone.utc):%Y-%m-%d} against the demo stack, "
        f"with real `claude -p --plugin-dir .` runs (model alias `{MODEL}`).", "",
        f"- wall time: {total_s:.0f} s; API cost shown by the CLI: ${w.cost:.2f}",
        f"- plan id: `{PLAN_ID}`", "", "## Steps", "", "| # | step | result | detail |", "|---|---|---|---|",
    ]
    for s in w.steps:
        detail = s["detail"].replace("|", "/").replace("\n", " ")[:200]
        t = f" ({s['seconds']} s)" if s["seconds"] is not None else ""
        lines.append(f"| {s['n']} | {s['name']}{t} | {'PASS' if s['ok'] else 'FAIL'} | {detail} |")
    lines += ["", "## What the model answered (step 2, trimmed)", "", "```", extras.get("triage", "")[:1800], "```", "",
              "## The plan it wrote (trimmed)", "", "```", extras.get("plan", "")[:2500], "```", "",
              "## Guard denials the model actually hit", ""]
    if w.denials:
        lines += [f"- {d}" for d in dict.fromkeys(w.denials)]
    else:
        lines.append("- none (the model never tried a denied action)")
    lines += ["", "## Applied result (`applied.json`, trimmed)", "", "```json", extras.get("applied", "")[:1500], "```", "",
              "## Post-apply `dea diff` (trimmed)", "", "```", extras.get("diff", "")[:1800], "```", ""]
    if extras.get("notes"):
        lines += ["## Notes", ""] + [f"- {n}" for n in extras["notes"]] + [""]
    OUT.write_text("\n".join(lines))


def _stack_ready():
    sh(["make", "demo-up"], timeout=900)  # idempotent: a no-op when the stack is already healthy
    sh(["bash", str(DEMO / "faults" / "01_schema_drift" / "revert.sh")], check=False)  # start from the clean state


def test_fault01_walkthrough(tmp_path_factory):
    t_start = time.time()
    scratch = tmp_path_factory.mktemp("walk", numbered=False)
    w = Walk(scratch)
    extras: dict = {"notes": []}
    try:
        _stack_ready()
        _run(w, extras)
    finally:
        write_report(w, extras, time.time() - t_start)
        print(f"[walkthrough] report: {OUT}")
    failed = [s for s in w.steps if not s["ok"]]
    assert not failed, failed
    assert len(w.steps) == 8


def _run(w: Walk, extras: dict) -> None:
    # 1. inject (baseline snapshot first so `dea diff` has something to compare against)
    t0 = time.time()
    w.dea("scan")
    base = w.dea("snapshot", check=False)
    assert base.returncode == 0, base.stdout + base.stderr
    time.sleep(1.2)
    sh(["bash", str(DEMO / "faults" / "01_schema_drift" / "inject.sh")], timeout=300)
    rid = trigger("extract_shop")
    state = wait_run("extract_shop", rid)
    w.step(1, "inject fault 01, trigger extract_shop", state == "failed", f"run {rid} finished: {state}", round(time.time() - t0, 1))
    assert state == "failed"

    # 2. triage through claude
    r = w.claude("02_triage", "extract_shop is failing, find out why")
    txt = r["text"]
    names_col = "cust_id" in txt and "customer_id" in txt
    cites = bool(re.search(r"does not exist|NEW signal|failures_24h|extract_orders|UndefinedColumn", txt))
    extras["triage"] = txt
    w.step(2, "claude names the renamed column and cites evidence", names_col and cites and not r["is_error"],
           f"names column: {names_col}; cites diff/log line: {cites}; turns {r['turns']}, cost {r['cost']}", r["seconds"])
    assert names_col and cites, txt

    # 3. plan
    pre_approved = (w.hub / ".dea" / "plans" / f"{PLAN_ID}.approved").exists()
    r = w.claude("03_plan", (
        f"Write a plan to fix this. The owner of the source database has agreed to restore the original column name "
        f"customer_id on public.orders. Use plan id {PLAN_ID}, write through the source named source_etl, and verify "
        f"through the read-only source named source. Run dea plan check and then tell me what to do next. "
        f"Do not apply anything."), resume=True)
    plan_file = w.hub / ".dea" / "plans" / f"{PLAN_ID}.md"
    chk = w.dea("plan", "check", PLAN_ID, check=False) if plan_file.exists() else None
    extras["plan"] = plan_file.read_text() if plan_file.exists() else ""
    ok3 = plan_file.exists() and chk.returncode == 0 and not pre_approved
    w.step(3, "plan file exists and passes dea plan check", ok3,
           f"plan exists: {plan_file.exists()}; check exit: {chk.returncode if chk else None}; cost {r['cost']}", r["seconds"])
    assert ok3, (chk.stdout if chk else "no plan file") + r["text"]
    assert not (w.hub / ".dea" / "plans" / f"{PLAN_ID}.approved").exists(), "the model must not be able to approve"

    # 4. the human step, through a pty
    t0 = time.time()
    code, out = _pty_run([str(DEA), "approve", PLAN_ID], w.env, w.hub, PLAN_ID)
    approved = (w.hub / ".dea" / "plans" / f"{PLAN_ID}.approved").is_file()
    st = w.dea("plans").stdout
    w.step(4, "human approves through a pty (dea approve)", code == 0 and approved and "approved" in st,
           f"exit {code}; dea plans: {st.strip().splitlines()[-1][:90]}", round(time.time() - t0, 1))
    assert code == 0 and approved, out

    # 5. the executor applies
    r = w.claude("05_apply", (
        f"Plan {PLAN_ID} is approved. Use the de-agent:executor agent to apply it, then report what happened."),
        resume=True)
    applied_path = w.hub / ".dea" / "plans" / f"{PLAN_ID}.applied.json"
    rec = json.loads(applied_path.read_text()) if applied_path.exists() else {}
    all_ok = bool(rec) and rec.get("status") == "ok" and all(s["status"] == "ok" for s in rec["steps"])
    extras["applied"] = json.dumps({k: rec.get(k) for k in ("plan_id", "mode", "status", "steps")}, indent=2) if rec else ""
    w.step(5, "executor applies; applied.json reports all steps ok", all_ok,
           f"status {rec.get('status')}; steps {[s['status'] for s in rec.get('steps', [])]}; cost {r['cost']}", r["seconds"])
    assert all_ok, r["text"]

    # 6. the failure signal is gone: re-run extract_shop (the plan's follow-up), snapshot, diff
    t0 = time.time()
    rid2 = trigger("extract_shop")
    state2 = wait_run("extract_shop", rid2)
    time.sleep(1.2)
    w.dea("snapshot", check=False)
    diff = w.dea("diff").stdout
    extras["diff"] = diff
    fixed = state2 == "success" and bool(re.search(r"runs: added extract_shop\.\S+ \(success", diff))
    still = "failed run(s) in 24h" in diff
    if still:
        extras["notes"].append("The 'N failed run(s) in 24h' signal for extract_shop is a 24 h rolling window, so the "
                               "old failed run keeps it alive; the evidence of the fix is the new successful run and "
                               "the missing `NEW signal` for a fresh failure.")
    w.step(6, "re-run extract_shop; dea snapshot + dea diff show the fix", fixed,
           f"re-run {rid2}: {state2}; success run in diff: {bool(re.search('added extract_shop', diff))}", round(time.time() - t0, 1))
    assert fixed, diff

    # 7. no write happened outside the approved plan
    log = w.hub / ".dea" / "logs" / "queries.jsonl"
    rows = [json.loads(x) for x in log.read_text().splitlines() if x.strip()] if log.exists() else []
    approved_hashes = set(json.loads((w.hub / ".dea" / "plans" / f"{PLAN_ID}.approved").read_text())["hashes"])
    bad = [x for x in rows if x["kind"] == "write" and not (x.get("agent_type") == "de-agent:executor"
                                                            and x["hash"] in approved_hashes)]
    writes = [x for x in rows if x["kind"] == "write"]
    w.step(7, "no write outside the plan (query_log)", not bad,
           f"{len(rows)} logged DB calls, {len(writes)} writes, {len(bad)} outside the plan")
    extras["notes"].append(f"query_log: {len(rows)} DB calls through Bash, {len(writes)} writes (all by the executor or none); "
                           "`dea apply` runs its own approved SQL and is recorded in applied.json, not in query_log.")
    assert not bad, bad

    # 8. the transcript summary is written by write_report() in the caller
    w.step(8, "transcript summary recorded", True, str(OUT.relative_to(ROOT)))
