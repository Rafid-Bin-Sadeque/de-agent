"""End-to-end against the real demo stack (M1b section 5). Skipped unless DEA_DEMO=1.

`DEA_DEMO=1 pytest -m demo -s cli/tests/test_demo_e2e.py` brings the stack up (make demo-up), takes a clean baseline
snapshot of all four sources, then for each seeded fault: inject -> trigger the affected DAG through the Airflow CLI
inside the container -> wait -> snapshot -> assert the expected signal is in `dea diff` -> revert -> recover.
Each fault's trimmed diff and timing go to demo/faults/NN_name/OBSERVED.md. The stack is left running unless
DEA_DEMO_DOWN=1.
"""
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from de_agent.config import load_config
from de_agent.diff import build_diff, render_markdown
from de_agent.render import signals_for
from de_agent.runner import result_status
from de_agent.snapshot import take_snapshot

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "demo"
REAL_HOME = os.environ.get("HOME", "")  # conftest re-points HOME per test; docker needs the real one

pytestmark = [pytest.mark.demo,
              pytest.mark.skipif(os.environ.get("DEA_DEMO") != "1", reason="set DEA_DEMO=1 to run the demo e2e")]

COMPOSE = ["docker", "compose", "-f", str(DEMO / "docker-compose.yml"), "--env-file", str(DEMO / ".env")]


def _env():
    env = dict(os.environ)
    env["HOME"] = REAL_HOME
    for line in (DEMO / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v
    return env


def sh(cmd, timeout=600, check=True):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_env(), cwd=ROOT)
    if check and p.returncode != 0:
        raise AssertionError(f"{' '.join(cmd)} failed: {p.stderr[-500:]}")
    return p


def af(*args, timeout=120):
    return sh([*COMPOSE, "exec", "-T", "airflow", "airflow", *args], timeout=timeout)


def _json_tail(text):
    for line in text.splitlines():
        if line.startswith("[{") or line == "[]":  # Airflow CLI prints log lines before the JSON
            try:
                return json.loads(line)
            except ValueError:
                continue
    return []


def trigger(dag_id):
    return _json_tail(af("dags", "trigger", dag_id, "-o", "json").stdout)[0]["dag_run_id"]


def run_state(dag_id, run_id):
    for r in _json_tail(af("dags", "list-runs", "-d", dag_id, "-o", "json").stdout):
        if r["run_id"] == run_id:
            return r["state"]
    return None


def task_duration(dag_id, run_id, task_id):
    """Task duration in seconds from the REST API (as the read-only agent_ro user)."""
    import base64
    import urllib.parse
    import urllib.request

    env = _env()
    url = (f"http://localhost:{env.get('DEMO_AF_PORT', '8085')}/api/v1/dags/{dag_id}/dagRuns/"
           f"{urllib.parse.quote(run_id, safe='')}/taskInstances/{task_id}")
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"agent_ro:{env['DEMO_AF_PASSWORD']}".encode()).decode())
    with urllib.request.urlopen(req, timeout=20) as resp:
        return float(json.load(resp)["duration"])


def wait_run(dag_id, run_id, want=("success", "failed"), timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = run_state(dag_id, run_id)
        if st in want:
            return st
        time.sleep(3)
    return run_state(dag_id, run_id)


@pytest.fixture(scope="module")
def hub(tmp_path_factory):
    h = tmp_path_factory.mktemp("demo_hub")
    text = (DEMO / "de-agent.yaml").read_text().replace("dbt/acme/target", str(DEMO / "dbt" / "acme" / "target"))
    (h / "de-agent.yaml").write_text(text)
    for k, v in _env().items():
        if k.startswith("DEMO_"):
            os.environ[k] = v
    return h


@pytest.fixture(scope="module")
def stack(hub):
    if os.environ.get("DEA_DEMO_KEEP") != "1":
        sh(["make", "demo-reset"], timeout=300)  # a clean stack: the baseline must have no signals
    t0 = time.time()
    sh(["make", "demo-up"], timeout=900)
    up_s = time.time() - t0
    # wait for the first extract_shop + dbt_build cycle so the baseline is meaningful
    deadline = time.time() + 600
    while time.time() < deadline:
        runs = _json_tail(af("dags", "list-runs", "-d", "dbt_build", "-o", "json").stdout)
        if any(r["state"] == "success" for r in runs):
            break
        time.sleep(5)
    else:
        raise AssertionError("dbt_build never succeeded after demo-up")
    print(f"\n[demo] stack healthy in {up_s:.0f}s, first dbt_build success after {time.time() - t0:.0f}s")
    yield
    if os.environ.get("DEA_DEMO_DOWN") == "1":
        sh(["make", "demo-down"], check=False)


def snap(hub):
    cfg = load_config(hub / "de-agent.yaml")
    out = take_snapshot(hub, cfg)
    time.sleep(1.1)  # snapshot ids have 1 s resolution
    return out


def diff_text(hub):
    return render_markdown(build_diff(hub))


def test_baseline_all_sources_ok_no_signals(stack, hub):
    out = snap(hub)
    for r in out.results:
        assert result_status(r) == "ok", (r["source"], r["errors"])
        assert signals_for(r) == [], (r["source"], signals_for(r))
    assert {r["source"] for r in out.results} == {"warehouse", "source", "airflow", "dbt"}


def _write_observed(name, title, symptom, seen, seconds, diff, notes=""):
    p = DEMO / "faults" / name / "OBSERVED.md"
    body = [f"# {title}: observed", "",
            f"Recorded by `DEA_DEMO=1 pytest -m demo` on {datetime.now(timezone.utc):%Y-%m-%d} (real run, demo stack).", "",
            f"- symptom: {symptom}",
            f"- expected signal seen: {'yes' if seen else 'NO'}",
            f"- seconds from inject to the signal in `dea diff`: {seconds:.0f}", "", "## Trimmed `dea diff`", "", "```"]
    body += [ln for ln in diff.strip().splitlines() if ln.strip()][:40]
    body += ["```", ""]
    if notes:
        body += ["## Notes", "", notes, ""]
    p.write_text("\n".join(body))


# name, title, symptom, inject-then-trigger dag (or None), patterns expected in the diff, recovery dag
FAULTS = [
    ("01_schema_drift", "Fault 01 schema drift", "orders mart is stale", "extract_shop",
     [r"NEW signal: dag extract_shop: \d+ failed run", r"failures_24h: added extract_shop\.extract_orders"], "extract_shop"),
    ("02_dataset_never_fires", "Fault 02 dataset never fires", "revenue dashboard didn't update today", "extract_shop",
     [r"dags extract_shop: is_paused False -> True"], "extract_shop"),
    ("03_bloat", "Fault 03 bloat", "warehouse feels slow", None,
     [r"NEW signal: table raw\.events \d+% dead tuples", r"dags warehouse_maintenance: is_paused False -> True"], None),
    ("04_missing_index", "Fault 04 missing index", "extract_shop got 5x slower", "extract_shop",
     [r"tables raw\.orders: .*n_live_tup"], "extract_shop"),
    ("05_dbt_test_failure", "Fault 05 dbt test failure", "dbt_build failing since this morning", "dbt_build",
     [r"NEW signal: \d+ dbt test\(s\) failing", r"tests: added test\.acme\.unique_stg_order_items_order_item_id"],
     "dbt_build"),
    ("06_lock_blocker", "Fault 06 lock blocker", "dbt_build is hanging", "dbt_build",
     [r"NEW signal: \d+ blocked session"], "dbt_build"),
]


EXTRA_NOTES = {
    "02_dataset_never_fires": [
        "The collector's 'dataset has consumers but no event in 24h' signal needs a real 24 h of silence, so it cannot "
        "fire inside a test run. Within minutes the evidence is `is_paused` flipping plus the triggered run sitting "
        "`queued`; the dataset signal appears the next day if nobody unpauses."],
    "03_bloat": ["The `warehouse_maintenance` pause shows as an airflow diff line; the bloat itself is the "
                 "dead-tuple signal and the table/DB size growth."],
}


@pytest.mark.parametrize("name,title,symptom,dag,patterns,recover", FAULTS, ids=[f[0] for f in FAULTS])
def test_fault(stack, hub, name, title, symptom, dag, patterns, recover):
    fault = DEMO / "faults" / name
    time.sleep(15)  # let the previous fault's revert settle (stats, autovacuum) so the diff shows only this fault
    snap(hub)  # baseline for this fault
    baseline_orders_s = None
    if name == "04_missing_index":  # a clean reference run, so the slowdown can be quantified
        rid0 = trigger("extract_shop")
        wait_run("extract_shop", rid0)
        baseline_orders_s = task_duration("extract_shop", rid0, "extract_orders")
        snap(hub)
    t0 = time.time()
    notes = []
    try:
        sh(["bash", str(fault / "inject.sh")], timeout=600)
        run_id = trigger(dag) if dag else None
        if name == "02_dataset_never_fires":
            time.sleep(20)  # a paused DAG's manual run just sits queued
            notes.append(f"manual run {run_id} state after 20 s: {run_state(dag, run_id)} (paused DAG: never starts)")
        elif name == "06_lock_blocker":
            time.sleep(30)  # dbt hangs on the locked relation
            notes.append(f"dbt_build run state after 30 s: {run_state(dag, run_id)}")
        elif name == "03_bloat":
            time.sleep(3)
        elif run_id:
            st = wait_run(dag, run_id)
            notes.append(f"{dag} run finished: {st}")
            if baseline_orders_s is not None:
                slow = task_duration(dag, run_id, "extract_orders")
                notes.append(f"extract_orders task: {baseline_orders_s:.2f} s before the fault, {slow:.2f} s after "
                             f"({slow / baseline_orders_s:.1f}x). The DAG run's wall time is dominated by scheduler "
                             "latency, so the run-level duration in the snapshot barely moves; the evidence is the "
                             "raw.orders row jump plus seq_scan growth on the table and the missing index in `\\d raw.orders`.")
        snap(hub)
        text = diff_text(hub)
        seconds = time.time() - t0
        seen = all(re.search(p, text) for p in patterns)
        notes += EXTRA_NOTES.get(name, [])
        _write_observed(name, title, symptom, seen, seconds, text, "\n".join(f"- {n}" for n in notes))
        missing = [p for p in patterns if not re.search(p, text)]
        assert not missing, f"expected diff lines missing: {missing}\n{text}"
    finally:
        sh(["bash", str(fault / "revert.sh")], timeout=600, check=False)
        if recover:
            rid = trigger(recover)
            wait_run(recover, rid)
            if name == "02_dataset_never_fires":
                pass
        if name == "06_lock_blocker":
            # the hung run may still be finishing; let it settle
            time.sleep(5)


# ---- fault 01, fixed the code way (M2b): diff + shell + verify steps through plan / approve / apply / rollback

CODE_PATCH = """--- a/dags/extract_shop.py
+++ b/dags/extract_shop.py
@@ -14,7 +14,7 @@
 TABLES = {
     "customers": "id, email, full_name, country, created_at, updated_at",
     "products": "id, sku, name, category, unit_price, created_at, updated_at",
-    "orders": "id, customer_id, status, total_amount, created_at, updated_at",
+    "orders": "id, cust_id, status, total_amount, created_at, updated_at",
     "order_items": "id, order_id, product_id, quantity, unit_price, created_at, updated_at",
     "events": "id, customer_id, event_type, payload, created_at, updated_at",
 }
"""

CODE_PLAN = """---
id: demo-fault01-code-fix
title: Read orders.cust_id in extract_shop
risk: medium
sources: [source, warehouse]
statement_timeout: 120s
---
## Why
The source team renamed orders.customer_id to cust_id; extract_orders selects customer_id and fails with
`column "customer_id" does not exist`. raw.orders keeps its column name (COPY is positional), so only the extract changes.
## Steps
### 1. Read the renamed column in the extractor
```diff file=dags/extract_shop.py repo=pipelines
{patch}```
```sql verify source=source expect="= 1"
SELECT count(*) FROM information_schema.columns WHERE table_name = 'orders' AND column_name = 'cust_id';
```
### 2. Re-run extract_shop and prove orders advanced
```shell exec=local cwd={demo} timeout=120
docker compose --env-file .env exec -T airflow airflow dags trigger extract_shop
```
```shell exec=local cwd={demo} timeout=120
sleep 75
```
```sql verify source=warehouse expect="= 1"
SELECT count(*) FROM raw._watermarks WHERE table_name = 'orders' AND updated_at > now() - interval '5 minutes';
```
## Rollback
Reverse-apply the patch (automatic: `dea apply --rollback`).
## Not doing
Renaming the column back in the source, or editing raw.orders / the dbt tests.
"""


def test_fault_01_fixed_the_code_way(stack, hub, tmp_path):
    sys_py = __import__("sys").executable
    fault = DEMO / "faults" / "01_schema_drift"
    dag_dir = DEMO / "airflow" / "dags"
    ghub = tmp_path / "ghub"
    (ghub / ".dea" / "plans").mkdir(parents=True)
    text = (DEMO / "de-agent.yaml").read_text().replace("dbt/acme/target", str(DEMO / "dbt" / "acme" / "target"))
    text = text.replace("path: airflow", f"path: {DEMO / 'airflow'}").replace("path: dbt/acme", f"path: {DEMO / 'dbt' / 'acme'}")
    (ghub / "de-agent.yaml").write_text(text)
    (ghub / ".dea" / "plans" / "demo-fault01-code-fix.md").write_text(
        CODE_PLAN.format(patch=CODE_PATCH, demo=DEMO))
    env = dict(_env(), DEA_HUB=str(ghub), XDG_CONFIG_HOME=str(tmp_path / "xdg"))

    def dea(*args, timeout=300):
        return subprocess.run([sys_py, "-c", "from de_agent.cli import app; app()", *args], capture_output=True,
                              text=True, env=env, cwd=ghub, timeout=timeout)

    def git_status():
        return subprocess.run(["git", "status", "--porcelain", "--", str(dag_dir)], capture_output=True, text=True,
                              cwd=ROOT).stdout.strip()

    assert git_status() == "", "demo dags must be clean before the test"
    time.sleep(15)
    snap(ghub)  # healthy baseline
    out = {}
    try:
        sh(["bash", str(fault / "inject.sh")], timeout=600)
        rid = trigger("extract_shop")
        assert wait_run("extract_shop", rid) == "failed"
        snap(ghub)
        out["broken_diff"] = diff_text(ghub)
        assert re.search(r"NEW signal: dag extract_shop: \d+ failed run", out["broken_diff"]), out["broken_diff"]

        chk = dea("plan", "check", "demo-fault01-code-fix")
        out["check"] = chk.stdout
        assert chk.returncode == 0 and "DIFF" in chk.stdout and "SHELL" in chk.stdout, chk.stdout + chk.stderr
        code, pty_out = _pty_approve(env, ghub, "demo-fault01-code-fix")
        assert code == 0 and "approved demo-fault01-code-fix" in pty_out, pty_out
        assert "approved" in dea("plans").stdout

        ap = dea("apply", "demo-fault01-code-fix", timeout=400)
        out["apply"] = ap.stdout
        assert ap.returncode == 0, ap.stdout + ap.stderr
        rec = json.loads((ghub / ".dea" / "plans" / "demo-fault01-code-fix.applied.json").read_text())
        diff_act = rec["steps"][0]["actions"][0]
        assert diff_act["kind"] == "diff" and "extract_shop.py" in diff_act["diff_stat"]
        assert "cust_id" in (dag_dir / "extract_shop.py").read_text()
        assert git_status().endswith("extract_shop.py") and git_status().startswith("M")  # modified, never committed
        time.sleep(1.1)
        snap(ghub)
        out["fixed_diff"] = diff_text(ghub)

        rb = dea("apply", "demo-fault01-code-fix", "--rollback")
        out["rollback"] = rb.stdout
        assert rb.returncode == 0, rb.stdout + rb.stderr
        assert "customer_id" in (dag_dir / "extract_shop.py").read_text()
        assert git_status() == "", git_status()  # git clean again
        assert "rolled back" in dea("plans").stdout
    finally:
        sh(["bash", str(fault / "revert.sh")], timeout=600, check=False)
        rid = trigger("extract_shop")
        wait_run("extract_shop", rid)
        subprocess.run(["git", "checkout", "--", str(dag_dir)], cwd=ROOT, check=False)
        print("\n[demo] fault-01 code-path run:")
        for k, v in out.items():
            print(f"--- {k}\n{v}")
    assert re.search(r"extract_shop|watermarks|raw\.orders", out["fixed_diff"]), out["fixed_diff"]


def _pty_approve(env, cwd, plan_id):
    import pty
    import select
    import sys

    master, slave = pty.openpty()
    proc = subprocess.Popen([sys.executable, "-c", "from de_agent.cli import app; app()", "approve", plan_id],
                            stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd, close_fds=True)
    os.close(slave)
    buf, sent, deadline = b"", False, time.time() + 90
    while time.time() < deadline:
        r, _, _ = select.select([master], [], [], 0.5)
        if r:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            if not sent and b"to approve it" in buf:
                os.write(master, (plan_id + "\n").encode())
                sent = True
        elif proc.poll() is not None:
            break
    proc.wait(timeout=10)
    os.close(master)
    return proc.returncode, buf.decode(errors="replace")
