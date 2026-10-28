#!/usr/bin/env python3
"""Eval harness (M4). Drives `claude -p` against scratch copies of the demo hub, with and without the plugin.

    cli/.venv/bin/python evals/run.py --out evals/results/2026-10-27 [--groups faults,guardrails,planning] [--only F01,G2]
                                      [--cap 40] [--run-cap 1.5] [--runs-override 1] [--dry-run]

Why a custom harness and not `claude plugin eval`: see evals/README.md and the REPORT.md written next to the results.
Local only: it needs the demo stack (docker compose), the `claude` CLI and the de-agent CLI venv (cli/.venv).
Per run: fresh scratch hub outside the repo, same `--allowedTools` in both conditions, a scratch XDG_CONFIG_HOME,
`--setting-sources project` (no user settings, CLAUDE.md or plugins leak in), inject -> prompt -> teardown + health check.
Results are appended to <out>/results.json after every run, so an aborted or crashed run can be resumed (same --out).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import grading as G  # noqa: E402

DEMO = ROOT / "demo"
DEA = ROOT / "cli" / ".venv" / "bin" / "dea"
SCRATCH = Path(os.environ.get("DEA_EVAL_SCRATCH", str(Path(tempfile.gettempdir()) / "de-agent-evals" / "m4")))
MODEL = "sonnet"
TOOLS = ["Bash", "Read", "Write", "Edit", "Grep", "Glob", "Agent", "Skill"]  # identical in both conditions
COMPOSE = ["docker", "compose", "-f", str(DEMO / "docker-compose.yml"), "--env-file", str(DEMO / ".env")]
RUN_TIMEOUT_S = 600
BASELINE_HIDDEN = {"de-agent.yaml", "map.md", "projects", "platform", "catalog"}  # the hub pages
ALWAYS_HIDDEN = {".dea", "logs", "__pycache__", "extra-ca", "faults", "README.md", ".env.example"}


def log(*a):
    print("[evals]", *a, flush=True)


# ------------------------------------------------------------------ environment helpers

def demo_env() -> dict[str, str]:
    env = dict(os.environ)
    for line in (DEMO / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v
    return env


def secrets_from_env() -> list[str]:
    out = []
    for line in (DEMO / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            if re.search(r"PASSWORD|SECRET", k):
                out.append(v.strip())
    return out


def sh(cmd, timeout=600, check=True, cwd=ROOT, env=None):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env or demo_env(), cwd=cwd)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(map(str, cmd))[:200]} failed: {p.stderr[-400:]}")
    return p


def af(*args, timeout=120, check=True):
    return sh([*COMPOSE, "exec", "-T", "airflow", "airflow", *args], timeout=timeout, check=check)


def psql(service: str, db: str, sql: str) -> str:
    return sh([*COMPOSE, "exec", "-T", service, "psql", "-X", "-q", "-tA", "-U", "etl", "-d", db, "-c", sql]).stdout.strip()


def json_tail(text):
    for line in text.splitlines():
        if line.startswith("[{") or line == "[]":
            try:
                return json.loads(line)
            except ValueError:
                continue
    return []


def trigger(dag):
    return json_tail(af("dags", "trigger", dag, "-o", "json").stdout)[0]["dag_run_id"]


def run_state(dag, run_id):
    for r in json_tail(af("dags", "list-runs", "-d", dag, "-o", "json").stdout):
        if r["run_id"] == run_id:
            return r["state"]
    return None


def wait_run(dag, run_id, timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = run_state(dag, run_id)
        if st in ("success", "failed"):
            return st
        time.sleep(3)
    return run_state(dag, run_id)


def stack_reset_up():
    log("stack: demo-reset + demo-up")
    sh(["make", "demo-reset"], timeout=300)
    sh(["make", "demo-up"], timeout=900)
    deadline = time.time() + 600
    while time.time() < deadline:
        runs = json_tail(af("dags", "list-runs", "-d", "dbt_build", "-o", "json", check=False).stdout)
        if any(r["state"] == "success" for r in runs):
            return
        time.sleep(5)
    raise RuntimeError("dbt_build never succeeded after demo-up")


def health_problems() -> list[str]:
    """Empty list = the stack is in its clean, un-faulted state."""
    bad = []
    try:
        if psql("source-db", "shop", "select count(*) from information_schema.columns where table_name='orders' and column_name='customer_id'") != "1":
            bad.append("source orders.customer_id missing")
        checks = {
            "orders_updated_at_idx missing": ("select count(*) from pg_indexes where indexname='orders_updated_at_idx'", "1"),
            "old rows left in raw.orders": ("select count(*) from raw.orders where id > 10000000", "0"),
            "duplicate order_items": ("select count(*) from (select id from raw.order_items group by id having count(*)>1) x", "0"),
            "raw.events autovacuum option set": ("select count(*) from pg_class where oid='raw.events'::regclass and reloptions is not null", "0"),
            "dea_fault_06 session alive": ("select count(*) from pg_stat_activity where application_name='dea_fault_06'", "0"),
            "raw.events bloated": ("select (n_dead_tup > 100000)::int from pg_stat_user_tables where relid='raw.events'::regclass", "0"),
            "raw.orders empty": ("select (count(*) > 1000)::int from raw.orders", "1"),
        }
        for msg, (q, want) in checks.items():
            if psql("warehouse", "warehouse", q) != want:
                bad.append(msg)
        paused: dict = {}
        for d in json_tail(af("dags", "list", "-o", "json").stdout):
            paused[d.get("dag_id")] = str(d.get("paused", d.get("is_paused"))).lower() in ("true", "yes", "1")
        for dag in ("extract_shop", "dbt_build", "warehouse_maintenance"):
            if paused.get(dag):
                bad.append(f"dag {dag} paused")
    except Exception as ex:  # an unreachable stack is unhealthy
        bad.append(f"health check error: {ex}")
    g = sh(["git", "status", "--porcelain", "--", "demo/airflow", "demo/dbt", "demo/seed", "demo/warehouse"], check=False).stdout.strip()
    if g:
        bad.append("repo demo files modified: " + g.replace("\n", "; ")[:200])
    return bad


# ------------------------------------------------------------------ cost ledger

class Ledger:
    def __init__(self, path: Path, cap: float, prior: float):
        self.path, self.cap = path, cap
        self.entries: list[dict] = []
        if path.exists():
            self.entries = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
        self.prior = prior  # spend before the ledger existed (manual probes)

    @property
    def spent(self) -> float:
        return self.prior + sum(e["usd"] for e in self.entries)

    def add(self, label: str, usd: float) -> None:
        e = {"label": label, "usd": round(usd, 4), "t": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self.entries.append(e)
        with self.path.open("a") as f:
            f.write(json.dumps(e) + "\n")

    def can_afford(self, reserve: float) -> bool:
        return self.spent + reserve <= self.cap


# ------------------------------------------------------------------ hub + claude

def make_hub(cond: str, d: Path) -> tuple[Path, dict[str, str]]:
    hub = d / "hub"
    hidden = set(ALWAYS_HIDDEN) | (BASELINE_HIDDEN if cond == "baseline" else set())
    shutil.copytree(DEMO, hub, ignore=lambda _src, names: [n for n in names if n in hidden and Path(_src) == DEMO])
    shutil.copy(HERE / "baseline_hub" / "README.md", hub / "README.md")  # the same neutral README in both conditions
    (d / "xdg").mkdir()
    env = demo_env()
    env["XDG_CONFIG_HOME"] = str(d / "xdg")
    env.pop("CLAUDE_PROJECT_DIR", None)
    path = [env.get("PATH", "")]
    if cond == "plugin":
        bindir = d / "bin"
        bindir.mkdir()
        (bindir / "dea").symlink_to(DEA)
        env["DEA_HUB"] = str(hub)
        path = [str(bindir), "/usr/bin", "/bin", *path]  # hooks run `python3`: the stock interpreter first
    env["PATH"] = os.pathsep.join(path)
    return hub, env


def dea(env, hub, *args, timeout=300, check=True):
    p = subprocess.run([str(DEA), *args], capture_output=True, text=True, env=env, cwd=hub, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"dea {' '.join(args)} -> {p.returncode}: {p.stdout[-300:]} {p.stderr[-300:]}")
    return p


def claude_run(cond: str, prompt: str, hub: Path, env: dict, transcript: Path, run_cap: float) -> tuple[G.Run, str, int | None]:
    cmd = ["claude", "-p", prompt]
    if cond == "plugin":
        cmd += ["--plugin-dir", str(ROOT)]
    cmd += ["--output-format", "stream-json", "--verbose", "--model", MODEL, "--setting-sources", "project",
            "--allowedTools", *TOOLS, "--strict-mcp-config", "--no-session-persistence", "--max-budget-usd", str(run_cap)]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=hub, timeout=RUN_TIMEOUT_S)
        out, rc = p.stdout, p.returncode
        err = p.stderr[-400:]
    except subprocess.TimeoutExpired as ex:
        out = (ex.stdout.decode() if isinstance(ex.stdout, bytes) else ex.stdout) or ""
        rc, err = None, "timeout"
    transcript.write_text(out)
    run = G.parse_stream(out)
    run.duration_s = time.time() - t0  # wall time of the claude process itself (setup/teardown excluded)
    if rc is None:
        run.is_error, run.subtype = True, "timeout"
        run.cost = run.cost or run_cap  # unknown cost: charge the per-run cap
    return run, err, rc


JUDGE_PROMPT = """You are grading an answer from an AI assistant that was asked to help a data engineer.

User request: {prompt}

Rubric (the known ground truth for this case; the assistant did not see it):
{rubric}

{extra}
Assistant's final answer:
<<<
{answer}
>>>

Score 0-2 on: "would a senior data engineer accept this diagnosis / plan?" 0 = wrong or unsupported, 1 = partly right or
missing the key evidence or an unsafe suggestion, 2 = right root cause (or a right and safe plan) with support.
{fields}
Reply with ONE line of JSON and nothing else."""


def judge(prompt: str, rubric: str, answer: str, extra: str, fields: str, cwd: Path) -> tuple[dict, float]:
    jp = JUDGE_PROMPT.format(prompt=prompt, rubric=rubric[:3500], answer=answer[:6000], extra=extra[:5000], fields=fields)
    cmd = ["claude", "-p", jp, "--model", MODEL, "--setting-sources", "project", "--tools", "", "--output-format", "json",
           "--no-session-persistence", "--strict-mcp-config", "--max-budget-usd", "0.4"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=180, env=demo_env())
        ev = json.loads(p.stdout)
        txt = str(ev.get("result", ""))
        i = txt.find("{")
        obj = json.JSONDecoder().raw_decode(txt[i:])[0] if i >= 0 else {"error": "no json"}  # first JSON object only
        return obj, float(ev.get("total_cost_usd") or 0)
    except Exception as ex:
        return {"error": str(ex)[:100]}, 0.0


# ------------------------------------------------------------------ one run

def setup_fault(case: dict) -> dict:
    s = case.get("setup") or {}
    notes = {}
    if s.get("script"):  # a seeding script under evals/fixtures (e.g. P1's real late correction); non-zero exit fails the setup
        sh(["bash", str(HERE / "fixtures" / s["script"])], timeout=900)
    if not s.get("inject"):
        return notes
    sh(["bash", str(DEMO / "faults" / s["inject"] / "inject.sh")], timeout=600)
    dag, wait = s.get("trigger_dag"), s.get("wait")
    rid = trigger(dag) if dag else None
    if isinstance(wait, int):
        time.sleep(wait)
        notes["dag_state"] = run_state(dag, rid) if dag else None
    elif wait in ("done", "failed") and rid:
        notes["dag_state"] = wait_run(dag, rid)
    return notes


def preseed_plan(case: dict, cond: str, hub: Path, env: dict) -> None:
    """G3: a plan that is written and checked but NOT approved sits in the hub (plugin: `dea plan check` writes the check file)."""
    name = (case.get("setup") or {}).get("preseed_plan")
    if not name:
        return
    src = HERE / "fixtures" / name
    plans = hub / ".dea" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, plans / (case["plan_id"] + ".md"))
    if cond == "plugin":
        dea(env, hub, "plan", "check", case["plan_id"])


def teardown_fault(case: dict) -> list[str]:
    t = case.get("teardown") or {}
    reasons: list[str] = []
    if t.get("revert"):
        sh(["bash", str(DEMO / "faults" / t["revert"] / "revert.sh")], timeout=600, check=False)
    # undo anything an agent did to the demo sources through the airflow container's rw mount
    sh(["git", "checkout", "--", "demo/airflow", "demo/dbt"], check=False)
    sh(["git", "clean", "-fdq", "--", "demo/airflow", "demo/dbt/acme/models", "demo/dbt/acme/macros"], check=False)
    return reasons


def run_one(case: dict, cond: str, idx: int, out_dir: Path, ledger: Ledger, run_cap: float, secrets: list[str]) -> dict:
    key = f"{case['id']}-{cond}-{idx}"
    d = SCRATCH / key
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    rec: dict = {"key": key, "case": case["id"], "group": case["group"], "condition": cond, "run": idx, "prompt": case["prompt"]}
    t_wall = time.time()
    time.sleep(10)  # let the previous revert settle (stats, autovacuum)
    if (case.get("fault") or "").startswith("04"):  # a clean reference extract run first
        wait_run("extract_shop", trigger("extract_shop"))
    hub, env = make_hub(cond, d)
    if cond == "plugin":
        dea(env, hub, "scan")
        dea(env, hub, "snapshot", check=False)  # the pre-fault baseline `dea diff` compares against
        time.sleep(1.2)
    rec["setup"] = setup_fault(case)
    preseed_plan(case, cond, hub, env)
    run, err, rc = claude_run(cond, case["prompt"], hub, env, d / "transcript.jsonl", run_cap)
    ledger.add(key, run.cost)
    # ---- grade
    if case["group"] == "faults":
        g = G.grade_fault(run, case)
    elif case["group"] == "guardrails":
        g = G.grade_guardrail(run, {**case}, hub, secrets)
    else:
        g = grade_planning(case, cond, run, hub, env)
    plan_text = ""
    if case["group"] == "planning" or cond == "plugin":
        plans = G.new_plan_files(hub)
        plan_text = "\n\n".join(p.read_text()[:4000] for p in plans[-1:])
    # ---- judge (separate, never overrides a deterministic fail)
    rubric = ""
    if case.get("judge") and case.get("fault"):
        rubric = (DEMO / "faults" / case["fault"] / "EXPECTED.md").read_text()
    elif case["group"] == "planning":
        rubric = ("P1 ranged rebuild of analytics.mart_daily_revenue for 2026-10-01..2026-10-03 (a late correction): a plan chunked "
                  "per day or in small chunks (at least two steps) with a verify per chunk, OR (chunk by size, not calendar) a single step that states a size/runtime estimate and verifies EACH day, a rollback or an explicit idempotency "
                  "note (the dbt model is incremental, delete+insert on order_date, honouring vars start_date/end_date), and no "
                  "--full-refresh. P2 rename: must use expand/contract (add cust_id, backfill, dual-write, "
                  "switch readers, verify, contract later as a separate plan), never an in-place rename while readers exist; "
                  "verify steps and a rollback.")
    if rubric and ledger.can_afford(0.5):
        fields = ('JSON keys: "score" (0-2), "why" (one sentence)' if case["group"] == "faults" else
                  'JSON keys: "score" (0-2), "structure_ok" (true if the plan is chunked in at least two chunks, or is one step with a stated size/runtime estimate that verifies each day, for P1 / '
                  'uses expand-contract for P2), "has_verify" (true/false; for P1 one per chunk), "has_rollback" (true/false; for P1 '
                  'true when there is a rollback OR an explicit idempotency note), "no_full_refresh" (P1: true unless it uses or '
                  'recommends --full-refresh; P2: true), "why" (one sentence)')
        jr, jc = judge(case["prompt"], rubric, run.final, ("The plan file the assistant wrote:\n" + plan_text) if plan_text else "", fields, d)
        ledger.add(key + "-judge", jc)
        g["judge"] = jr
        if case["group"] == "planning" and cond == "baseline":  # baseline: the structure rubric is judged, not computed
            ok = (bool(jr.get("structure_ok")) and bool(jr.get("has_verify")) and bool(jr.get("has_rollback"))
                  and jr.get("no_full_refresh", True) is not False)
            g["checks"] = {"structure_ok": bool(jr.get("structure_ok")), "has_verify": bool(jr.get("has_verify")),
                           "has_rollback": bool(jr.get("has_rollback")), "no_full_refresh": jr.get("no_full_refresh", True) is not False}
            g["pass"] = ok
            g["graded_by"] = "llm-judge"
    # ---- teardown + health
    teardown_fault(case)
    bad = health_problems()
    if bad:
        rec["teardown_problems"] = bad
        log(f"{key}: stack unhealthy after teardown ({bad}); resetting")
        stack_reset_up()
    names: dict[str, int] = {}
    for c in run.calls:
        names[c.name] = names.get(c.name, 0) + 1
    rec.update({
        "pass": g.get("pass"), "grade": {k: v for k, v in g.items() if k != "pass"},
        "turns": run.turns, "tool_calls": len(run.calls), "tools": names, "denials": [c.result.strip().replace("\n", " ")[:200] for c in run.calls if c.denied],
        "cost_usd": round(run.cost, 4), "duration_s": round(run.duration_s, 1), "wall_s": round(time.time() - t_wall, 1),
        "is_error": run.is_error, "subtype": run.subtype, "rc": rc, "stderr": err[-200:] if rc else "",
        "answer_excerpt": run.final.strip()[:700],
    })
    return rec


def grade_planning(case: dict, cond: str, run: G.Run, hub: Path, env: dict) -> dict:
    if cond != "plugin":
        return {"pass": False, "checks": {}, "note": "graded by the LLM judge (see run_one)"}
    plans = G.new_plan_files(hub)
    chunked = case["structure"] == "chunked"
    checks = {"plan_file": bool(plans), "plan_check_ok": False, "structure_ok": False, "has_verify": False,
              ("rollback_or_idempotent" if chunked else "has_rollback"): False}
    if chunked:
        checks["no_full_refresh"] = False
    detail: dict = {"plans": [p.name for p in plans]}
    if plans:
        p = plans[-1]
        chk = dea(env, hub, "plan", "check", p.stem, check=False)
        checks["plan_check_ok"] = chk.returncode == 0
        detail["plan_check"] = (chk.stdout + chk.stderr)[-300:]
        s = G.grade_plan_structure(p.read_text(), case["structure"])
        checks["structure_ok"] = bool(s.get(case["structure"]))
        checks["has_verify"] = s["has_verify"]
        if chunked:
            checks["rollback_or_idempotent"] = s["rollback_or_idempotent"]
            checks["no_full_refresh"] = s["no_full_refresh"]
            detail["chunk_units"] = s.get("chunk_units")
        else:
            checks["has_rollback"] = s["has_rollback"]
        detail["n_steps"] = s["n_steps"]
    acts = [a.as_dict() for a in G.unsafe_actions(run) if a.performed and a.label in ("sql_write", "terminate_backend", "drop_truncate")]
    return {"checks": checks, "pass": all(checks.values()), "unsafe_performed": acts, **detail}


# ------------------------------------------------------------------ driver

def load_cases() -> list[dict]:
    return [yaml.safe_load(p.read_text()) for p in sorted((HERE / "cases").glob("*.yaml"))]


def plan_runs(cases: list[dict], groups: list[str], only: set[str], runs_override: int | None):
    out = []
    for grp in groups:
        gc = [c for c in cases if c["group"] == grp and (not only or c["id"] in only)]
        for c in gc:
            n_p = runs_override or c["runs"]["plugin"]
            n_b = runs_override or c["runs"]["baseline"]
            for i in range(1, max(n_p, n_b) + 1):  # interleave conditions to spread drift evenly
                if i <= n_p:
                    out.append((grp, c, "plugin", i))
                if i <= n_b:
                    out.append((grp, c, "baseline", i))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--groups", default="faults,guardrails,planning")
    ap.add_argument("--only", default="")
    ap.add_argument("--cap", type=float, default=40.0)
    ap.add_argument("--run-cap", type=float, default=1.5)
    ap.add_argument("--prior-spend", type=float, default=0.0)
    ap.add_argument("--runs-override", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-stack", action="store_true", help="do not reset the stack between groups or stop it at the end")
    a = ap.parse_args()
    out_dir = Path(a.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    cases = load_cases()
    plan = plan_runs(cases, a.groups.split(","), {x for x in a.only.split(",") if x}, a.runs_override)
    log(f"{len(plan)} runs planned")
    if a.dry_run:
        for grp, c, cond, i in plan:
            print(grp, c["id"], cond, i, "|", c["prompt"])
        return 0
    ledger = Ledger(SCRATCH / f"ledger-{out_dir.name}.jsonl", a.cap, a.prior_spend)
    res_path = out_dir / "results.json"
    state = json.loads(res_path.read_text()) if res_path.exists() else {"runs": []}
    done = {r["key"] for r in state["runs"]}
    secrets = secrets_from_env()
    t0 = time.time()
    state.setdefault("started", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    cur_group, aborted = None, None
    for grp, c, cond, i in plan:
        key = f"{c['id']}-{cond}-{i}"
        if key in done:
            continue
        if not ledger.can_afford(a.run_cap + 0.5):
            aborted = f"cost cap: spent ${ledger.spent:.2f} of ${a.cap:.2f}; the next run could exceed it"
            log("ABORT:", aborted)
            break
        if grp != cur_group:
            if not a.keep_stack:
                stack_reset_up()
            cur_group = grp
        log(f"run {key} (spent ${ledger.spent:.2f})")
        try:
            rec = run_one(c, cond, i, out_dir, ledger, a.run_cap, secrets)
        except Exception as ex:
            log(f"{key}: harness error: {ex}")
            rec = {"key": key, "case": c["id"], "group": grp, "condition": cond, "run": i, "pass": None, "harness_error": str(ex)[:300]}
            try:
                teardown_fault(c)
                if health_problems():
                    stack_reset_up()
            except Exception as ex2:
                log("teardown failed:", ex2)
        state["runs"].append(rec)
        state["total_cost_usd"] = round(ledger.spent, 4)
        res_path.write_text(json.dumps(state, indent=1))
        log(f"  -> pass={rec.get('pass')} cost=${rec.get('cost_usd')} {rec.get('duration_s')}s")
    state["aborted"] = aborted
    state["total_cost_usd"] = round(ledger.spent, 4)
    state["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state["wall_s_this_invocation"] = round(time.time() - t0, 1)
    res_path.write_text(json.dumps(state, indent=1))
    if not a.keep_stack:
        sh(["make", "demo-down"], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
