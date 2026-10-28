"""`dea apply`: run an approved plan (M2 section 1).

Order of operations: verify the HMAC, re-hash every statement and refuse anything not in the approved set, THEN run.
One transaction per step (per source) unless a block says `transaction: false`; verify blocks run after their step's
statements; the first failed statement or verify stops the run. Progress is written to <id>.applied.json after each step.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from de_agent import approval, stepexec
from de_agent.collectors.base import CollectorError
from de_agent.config import Config
from de_agent.plans import (
    Block,
    Plan,
    blocks_covered,
    check_plan,
    eval_expect,
    plans_dir,
    repo_path,
    statements,
)


class ApplyRefused(Exception):
    """Nothing was run: missing/bad approval, unapproved statement, plan errors, or already applied."""


@dataclass
class ApplyResult:
    status: str  # ok | failed
    mode: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    path: Path | None = None


@dataclass
class Unit:
    kind: str  # apply | verify
    source: str
    transaction: bool
    texts: list[str]
    block: Block
    reverse: bool = False  # diff units: reverse-apply (rollback)

    @property
    def bk(self) -> str:
        return self.block.kind


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


def _units(blocks: list[Block], reverse: bool = False) -> list[Unit]:
    """Execution units: the step's statements in plan order (consecutive transactional SQL blocks on one source share a
    transaction; a diff or shell block ends the run), then its verify blocks."""
    units: list[Unit] = []
    for b in (x for x in blocks if not x.verify):
        if b.kind != "sql":
            units.append(Unit("apply", b.source or "", True, [b.sql], b, reverse and b.kind == "diff"))
            continue
        texts = [t for t, _ in statements(b.sql)]
        last = units[-1] if units else None
        if last and last.bk == "sql" and last.source == b.source and last.transaction and b.transaction:
            last.texts.extend(texts)
        else:
            units.append(Unit("apply", b.source or "", b.transaction, texts, b))
    units += [Unit("verify", b.source or "", True, [b.sql], b) for b in blocks if b.verify]
    return units


def _groups(plan: Plan, rollback: bool, applied: set[tuple[int, int]] | None = None
            ) -> list[tuple[int, str, list[Block], bool]]:
    """(step number, title, blocks, reverse). Rollback = the diffs that were applied, reversed newest first, then the
    Rollback section in plan order."""
    if not rollback:
        return [(s.n, s.title, s.blocks, False) for s in plan.steps]
    out: list[tuple[int, str, list[Block], bool]] = []
    for s in reversed(plan.steps):
        diffs = [b for b in reversed(s.blocks) if b.kind == "diff" and (applied is None or (s.n, b.line) in applied)]
        if diffs:
            out.append((s.n, f"revert step {s.n}: {s.title}", diffs, True))
    if plan.rollback:
        out.append((0, "rollback", plan.rollback, False))
    return out


def _applied_diffs(path: Path) -> set[tuple[int, int]]:
    """(step, line) of every diff the earlier apply actually applied (a failed run may have applied only some)."""
    prev = _load(path) or {}
    return {(st["step"], a["line"]) for st in prev.get("steps", []) for a in st.get("actions", [])
            if a.get("kind") == "diff" and a.get("mode") == "applied"}


def authorize(hub: Path, cfg: Config, plan: Plan, rollback: bool, key: bytes | None) -> dict[str, Any]:
    """Raise ApplyRefused unless the plan is checked clean, signed, and every statement to run is approved."""
    rep = check_plan(plan, cfg, filename_id=plan.path.stem if plan.path else None, hub=hub, tree=not rollback)
    if not rep.ok:
        raise ApplyRefused("plan has check errors (run `dea plan check`): " + "; ".join(rep.errors[:3]))
    path = plans_dir(hub) / f"{plan.id}.approved"
    if not path.is_file():
        raise ApplyRefused(
            f"plan {plan.id} is not approved: the human must run `dea approve {plan.id}` in their own terminal")
    data, why = approval.read_approval(str(path), key, plan.id)
    if data is None:
        raise ApplyRefused(f"approval rejected: {why}")
    ok = set(data["hashes"])
    blocks = ([b for s in plan.steps for b in s.blocks if b.kind == "diff"] + plan.rollback) if rollback else \
        [b for s in plan.steps for b in s.blocks]
    if rollback and not blocks:
        raise ApplyRefused("plan has no Rollback section")
    bad = blocks_covered(blocks, ok)
    if bad:
        raise ApplyRefused(f"{len(bad)} statement(s) not in the approved set (plan edited after approval?): "
                           f"{bad[0][0].kind} {bad[0][1][:12]}")
    return data


def dry_run_lines(plan: Plan, rollback: bool) -> list[str]:
    out: list[str] = []
    for n, title, blocks, rev in _groups(plan, rollback):
        out.append(f"== {title if rollback else f'step {n}: {title}'}")
        for u in _units(blocks, rev):
            if u.bk == "diff":
                out.append(f"  [{'reverse ' if u.reverse else ''}git apply {u.block.repo}/{u.block.file}]")
            elif u.bk == "shell":
                out.append(f"  [{u.kind} shell on {u.block.exec}]")
                out.append(f"    {u.texts[0][:100]}")
            else:
                out.append(f"  [{u.kind} on {u.source}{'' if u.transaction else ', no transaction'}]")
                out += [f"    {t[:100]}" for t in u.texts]
    return out


def _load(p: Path) -> dict[str, Any] | None:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def apply_plan(
    hub: Path, cfg: Config, plan: Plan, rollback: bool = False, key: bytes | None = None,
    write_fn: Callable[..., list[int | None]] | None = None,
    read_fn: Callable[..., list[dict[str, Any]]] | None = None,
) -> ApplyResult:
    from de_agent import sqlexec

    write_fn = write_fn or sqlexec.run_write
    read_fn = read_fn or sqlexec.run_query
    approval_data = authorize(hub, cfg, plan, rollback, key)
    out_path = plans_dir(hub) / (f"{plan.id}.rolledback.json" if rollback else f"{plan.id}.applied.json")
    if not rollback:
        prev = _load(out_path)
        if prev and prev.get("status") == "ok":
            raise ApplyRefused(f"plan {plan.id} was already applied; write a new plan instead of re-running it")

    groups = _groups(plan, rollback, _applied_diffs(plans_dir(hub) / f"{plan.id}.applied.json") if rollback else None)
    record: dict[str, Any] = {
        "plan_id": plan.id, "mode": "rollback" if rollback else "apply", "status": "running",
        "approved_by": approval_data.get("approver"), "approved_at": approval_data.get("approved_at"),
        "started_at": _now(), "finished_at": None,
        "steps": [{"step": n, "title": t, "status": "pending", "rowcount": None, "duration_s": None,
                   "verify": [], "actions": [], "error": None} for n, t, _, _ in groups],
    }
    _atomic_json(out_path, record)
    failed = False
    for idx, (_n, _title, blocks, reverse) in enumerate(groups):
        rec = record["steps"][idx]
        if failed:
            rec["status"] = "skipped"
            continue
        t0 = time.monotonic()
        total, known = 0, False
        try:
            for u in _units(blocks, reverse):
                if u.bk == "diff":
                    _run_diff(hub, cfg, u, rec, out_path, record)
                elif u.bk == "shell":
                    _run_shell(hub, plan, u, rec)
                else:
                    src = cfg.source(u.source)
                    if src.conn is None:
                        raise CollectorError(f"source {u.source} has no conn")
                    timeout = float(plan.statement_timeout_s)
                    if u.kind == "apply":
                        for c in write_fn(src.conn, u.texts, src.exec, timeout, u.transaction):
                            if c is not None:
                                total, known = total + c, True
                    else:
                        rows = read_fn(src.conn, u.texts[0], src.exec, timeout)
                        value = next(iter(rows[0].values()), None) if rows else None
                        ok = True if u.block.expect is None else eval_expect(u.block.expect, value)
                        rec["verify"].append({"source": u.source, "sql": u.texts[0][:200], "expect": u.block.expect,
                                              "value": value, "ok": ok})
                        if not ok:
                            raise CollectorError(f"verify failed: got {value!r}, expected {u.block.expect}")
            rec["status"] = "ok"
        except Exception as ex:
            rec["status"] = "failed"
            rec["error"] = stepexec.redact_output(str(ex))[:500]
            failed = True
        rec["rowcount"] = total if known else None
        rec["duration_s"] = round(time.monotonic() - t0, 3)
        _atomic_json(out_path, record)
    record["status"] = "failed" if failed else "ok"
    record["finished_at"] = _now()
    _atomic_json(out_path, record)
    return ApplyResult(record["status"], record["mode"], record["steps"], out_path)


def _run_diff(hub: Path, cfg: Config, u: Unit, rec: dict[str, Any], out_path: Path, record: dict[str, Any]) -> None:
    b = u.block
    repo = repo_path(hub, cfg, b.repo)
    if repo is None:
        raise CollectorError(f"unknown repo {b.repo!r}")
    act = {"kind": "diff", "mode": "pending", "repo": b.repo, "file": b.file, "line": b.line, "diff_stat": None}
    rec["actions"].append(act)
    try:
        act["diff_stat"] = stepexec.git_apply(repo, b.sql, b.file or "", reverse=u.reverse)
    except RuntimeError as ex:
        act["mode"] = "failed"
        raise CollectorError(str(ex)) from None
    act["mode"] = "reverted" if u.reverse else "applied"
    _atomic_json(out_path, record)


def _run_shell(hub: Path, plan: Plan, u: Unit, rec: dict[str, Any]) -> None:
    b = u.block
    cwd = None
    if b.exec == "local":
        cwd = (hub / b.cwd).resolve() if b.cwd else hub
        if not cwd.is_dir():
            raise CollectorError(f"cwd {b.cwd!r} does not exist")
    timeout = float(b.timeout or plan.statement_timeout_s)
    try:
        res = stepexec.run_shell(b.sql, b.exec, cwd, timeout, remote_cwd=b.cwd)
    except RuntimeError as ex:
        raise CollectorError(str(ex)) from None
    if u.kind == "apply":
        rec["actions"].append({"kind": "shell", "command": b.sql, "exec": b.exec, "exit_code": res.exit_code,
                               "stdout_tail": res.stdout_tail, "stderr_tail": res.stderr_tail, "line": b.line})
        if res.exit_code != 0:
            raise CollectorError(f"command exited {res.exit_code}: {(res.stderr_tail or res.stdout_tail)[-300:]}")
        return
    want_exit = b.expect_exit if b.expect_exit is not None or b.expect_stdout is not None else 0
    ok = (want_exit is None or res.exit_code == want_exit) and \
        (b.expect_stdout is None or re.search(b.expect_stdout, res.stdout_full) is not None)
    exp = " and ".join(x for x in (None if want_exit is None else f"exit={want_exit}",
                                   None if b.expect_stdout is None else f"stdout~={b.expect_stdout}") if x)
    rec["verify"].append({"source": f"shell:{b.exec}", "sql": b.sql[:200], "expect": exp,
                          "value": res.exit_code if b.expect_stdout is None else res.stdout_tail[-200:],
                          "ok": ok, "exit_code": res.exit_code, "stdout_tail": res.stdout_tail})
    if not ok:
        raise CollectorError(f"verify failed: exit {res.exit_code}, expected {exp}")
