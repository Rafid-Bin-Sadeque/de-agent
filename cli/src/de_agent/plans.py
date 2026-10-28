"""Plan files: parse, check, state (M2 section 1). Plans live in <hub>/.dea/plans/<id>.md.

A plan is the unit of approval: `dea plan check` classifies every statement, `dea approve` (human, TTY) signs the
statement hashes, `dea apply` (executor only) runs them. Statement hashes are the sqlclass hashes, i.e. the same
normalisation sql_guard uses, so a hash approved here is the hash the guard sees.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from de_agent.sqlclass import Stmt, _split_statements, classify

PLAN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,120}$")
RISKS = ("low", "medium", "high")
DEFAULT_TIMEOUT_S = 300
SECTIONS = {"why", "steps", "rollback", "not doing"}
EXPECT_RE = re.compile(
    r"^\s*(?:(?P<op>=|==|!=|<>|>=|<=|>|<)\s*(?P<num>-?\d+(?:\.\d+)?)"
    r"|between\s+(?P<lo>-?\d+(?:\.\d+)?)\s+and\s+(?P<hi>-?\d+(?:\.\d+)?)"
    r"|is\s+(?P<not>not\s+)?null)\s*$", re.I)


class PlanError(Exception):
    """Plan missing or unusable."""


def plans_dir(hub: Path) -> Path:
    return hub / ".dea" / "plans"


def plan_path(hub: Path, plan_id: str) -> Path:
    return plans_dir(hub) / f"{plan_id}.md"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Block:
    section: str  # "step" | "rollback"
    step: int  # 0 for rollback
    verify: bool
    source: str | None
    sql: str
    expect: str | None
    transaction: bool
    line: int
    kind: str = "sql"  # sql | diff | shell (diff/shell: `sql` holds the patch / the command)
    repo: str | None = None  # diff: scan name
    file: str | None = None  # diff: path relative to the repo
    exec: str = "local"  # shell: local | ssh:<host>
    cwd: str | None = None  # shell
    timeout: int | None = None  # shell, seconds
    expect_exit: int | None = None  # shell verify
    expect_stdout: str | None = None  # shell verify (regex)


@dataclass
class Step:
    n: int
    title: str
    blocks: list[Block] = field(default_factory=list)


@dataclass
class Plan:
    id: str
    title: str
    risk: str
    sources: list[str]
    statement_timeout_s: int
    steps: list[Step]
    rollback: list[Block]
    text: str
    path: Path | None = None
    errors: list[str] = field(default_factory=list)  # parse errors
    meta: dict[str, Any] = field(default_factory=dict)


def parse_duration(v: Any) -> int | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v) if v > 0 else None
    m = re.fullmatch(r"\s*(\d+)\s*(ms|s|m|h)?\s*", str(v or ""))
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2) or "s"
    secs = {"ms": n / 1000, "s": n, "m": n * 60, "h": n * 3600}[unit]
    return max(1, int(secs)) if n else None


def parse_expect(expect: str) -> re.Match[str] | None:
    return EXPECT_RE.match(expect)


def eval_expect(expect: str, value: Any) -> bool:
    """Evaluate `= 0`, `> 0`, `between a and b`, `is null` against a scalar (None when the query returned no row)."""
    m = parse_expect(expect)
    if not m:
        raise ValueError(f"unsupported expect {expect!r}")
    if m.group("op") is None and m.group("lo") is None:  # is [not] null
        return (value is not None) if m.group("not") else (value is None)
    if value is None or isinstance(value, bool):
        return False
    try:
        x = float(value)
    except (TypeError, ValueError):
        return False
    if m.group("lo") is not None:
        return float(m.group("lo")) <= x <= float(m.group("hi"))
    n, op = float(m.group("num")), m.group("op")
    return {"=": x == n, "==": x == n, "!=": x != n, "<>": x != n, ">=": x >= n, "<=": x <= n,
            ">": x > n, "<": x < n}[op]


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str, int]:
    """Return (meta, body, body_start_line_offset)."""
    from de_agent.lint import parse_frontmatter

    meta, body = parse_frontmatter(text)
    offset = text[: len(text) - len(body)].count("\n") if body != text else 0
    return meta, body, offset


def _parse_info(info: str) -> tuple[dict[str, Any], list[str]]:
    """Parse the text after ```sql into attributes."""
    attrs: dict[str, Any] = {"verify": False, "source": None, "expect": None, "transaction": True}
    errs: list[str] = []
    try:
        toks = shlex.split(info)
    except ValueError as ex:
        return attrs, [f"cannot parse fence attributes ({ex})"]
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "verify":
            attrs["verify"] = True
        elif t.startswith("source="):
            attrs["source"] = t.split("=", 1)[1] or None
        elif t.startswith("expect="):
            attrs["expect"] = t.split("=", 1)[1]
        elif t.startswith("transaction=") or t == "transaction:" or t.startswith("transaction:"):
            if t.startswith("transaction="):
                val = t.split("=", 1)[1]
            elif t == "transaction:":
                i += 1
                val = toks[i] if i < len(toks) else ""
            else:
                val = t.split(":", 1)[1]
            if val.lower() in ("false", "no", "0"):
                attrs["transaction"] = False
            elif val.lower() in ("true", "yes", "1"):
                attrs["transaction"] = True
            else:
                errs.append(f"transaction must be true or false, got {val!r}")
        else:
            errs.append(f"unknown fence attribute {t!r}")
        i += 1
    return attrs, errs


def _parse_info_ext(kind: str, info: str) -> tuple[dict[str, Any], list[str]]:
    """Fence attributes of a ```diff (file= repo=) or ```shell (exec= cwd= timeout= verify expect_exit= expect_stdout~=)."""
    attrs: dict[str, Any] = {"verify": False, "source": None, "expect": None, "transaction": True, "kind": kind,
                             "repo": None, "file": None, "exec": "local", "cwd": None, "timeout": None,
                             "expect_exit": None, "expect_stdout": None}
    errs: list[str] = []
    try:
        toks = shlex.split(info)
    except ValueError as ex:
        return attrs, [f"cannot parse fence attributes ({ex})"]
    for t in toks:
        if kind == "diff" and t.startswith("file="):
            attrs["file"] = t.split("=", 1)[1] or None
        elif kind == "diff" and t.startswith("repo="):
            attrs["repo"] = t.split("=", 1)[1] or None
        elif kind == "shell" and t == "verify":
            attrs["verify"] = True
        elif kind == "shell" and t.startswith("exec="):
            attrs["exec"] = t.split("=", 1)[1]
            from de_agent.stepexec import SSH_HOST_RE

            if attrs["exec"] != "local" and not SSH_HOST_RE.match(attrs["exec"]):
                errs.append(f"exec must be local or ssh:<host-alias>, got {attrs['exec']!r}")
        elif kind == "shell" and t.startswith("cwd="):
            attrs["cwd"] = t.split("=", 1)[1] or None
        elif kind == "shell" and t.startswith("timeout="):
            attrs["timeout"] = parse_duration(t.split("=", 1)[1])
            if attrs["timeout"] is None:
                errs.append(f"timeout {t.split('=', 1)[1]!r} is not a duration like 900 or 15m")
        elif kind == "shell" and t.startswith("expect_exit="):
            try:
                attrs["expect_exit"] = int(t.split("=", 1)[1])
            except ValueError:
                errs.append(f"expect_exit must be an integer, got {t.split('=', 1)[1]!r}")
        elif kind == "shell" and t.startswith("expect_stdout~="):
            attrs["expect_stdout"] = t.split("~=", 1)[1]
            try:
                re.compile(attrs["expect_stdout"])
            except re.error as ex:
                errs.append(f"expect_stdout~= is not a valid regex ({ex})")
        else:
            errs.append(f"unknown fence attribute {t!r}")
    return attrs, errs


def parse_plan(text: str, path: Path | None = None) -> Plan:
    meta, body, offset = _split_frontmatter(text)
    errors: list[str] = []
    pid = str(meta.get("id") or "").strip()
    risk = str(meta.get("risk") or "").strip().lower()
    srcs = meta.get("sources")
    sources = [str(s) for s in srcs] if isinstance(srcs, list) else []
    timeout = parse_duration(meta["statement_timeout"]) if "statement_timeout" in meta else DEFAULT_TIMEOUT_S
    if timeout is None:
        errors.append(f"statement_timeout {meta.get('statement_timeout')!r} is not a duration like 300s")
        timeout = DEFAULT_TIMEOUT_S
    plan = Plan(pid, str(meta.get("title") or "").strip(), risk, sources, timeout, [], [], text, path, errors, meta)

    section = ""
    cur_step: Step | None = None
    fence: dict[str, Any] | None = None
    buf: list[str] = []
    for i, line in enumerate(body.splitlines(), start=1 + offset):
        stripped = line.strip()
        if fence is not None:
            if stripped.startswith("```"):
                if fence["sql"]:
                    _add_block(plan, section, cur_step, fence, "\n".join(buf), errors)
                fence, buf = None, []
            else:
                buf.append(line)
            continue
        if stripped.startswith("```"):
            lang_rest = stripped[3:].strip()
            lang, _, info = lang_rest.partition(" ")
            kind = lang.lower() if lang.lower() in ("sql", "diff", "shell") else ""
            # diff / shell fences are only steps inside ## Steps / ## Rollback (elsewhere they are illustration)
            is_sql = bool(kind) and (kind == "sql" or section in ("steps", "rollback"))
            if not is_sql:
                attrs, errs = {}, []
            elif kind == "sql":
                attrs, errs = _parse_info(info)
                attrs["kind"] = "sql"
            else:
                attrs, errs = _parse_info_ext(kind, info)
            errors += [f"line {i}: {e}" for e in errs]
            fence = {"sql": is_sql, "line": i, **attrs}
            continue
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m and not line.startswith("###"):
            section = m.group(1).strip().lower()
            cur_step = None
            continue
        m = re.match(r"^###\s+(?:(\d+)\s*[.):-]?\s*)?(.*?)\s*$", line)
        if m and section == "steps":
            n = int(m.group(1)) if m.group(1) else len(plan.steps) + 1
            cur_step = Step(n, m.group(2))
            plan.steps.append(cur_step)
    if fence is not None:
        errors.append(f"line {fence['line']}: unterminated code fence")
    return plan


def _add_block(plan: Plan, section: str, cur_step: Step | None, fence: dict[str, Any], sql: str,
               errors: list[str]) -> None:
    if section not in ("steps", "rollback"):
        return  # illustrative SQL in Why / Not doing is never executed
    line = fence["line"]
    if section == "steps" and cur_step is None:
        errors.append(f"line {line}: {fence['kind']} block outside a numbered step (### 1. title)")
        return
    if fence["expect"] is not None and not fence["verify"]:
        errors.append(f"line {line}: expect= is only valid on a verify block")
    kind = fence["kind"]
    body = sql.strip("\n")
    if kind == "diff":
        from de_agent.stepexec import exact_patch

        body = exact_patch(body)  # exact text for `git apply`; the hash normalizes separately (diff_hash)
    blk = Block("step" if section == "steps" else "rollback", cur_step.n if cur_step else 0,
                bool(fence["verify"]), fence["source"], body, fence["expect"],
                bool(fence["transaction"]), line, kind, fence.get("repo"), fence.get("file"), fence.get("exec", "local"),
                fence.get("cwd"), fence.get("timeout"), fence.get("expect_exit"), fence.get("expect_stdout"))
    if cur_step is not None and section == "steps":
        cur_step.blocks.append(blk)
    else:
        plan.rollback.append(blk)


def load_plan(hub: Path, plan_id: str) -> Plan:
    plan_id = plan_id.removesuffix(".md")
    if not PLAN_ID_RE.match(plan_id):
        raise PlanError(f"invalid plan id {plan_id!r} (lowercase letters, digits, - _ .)")
    p = plan_path(hub, plan_id)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        raise PlanError(f"no such plan: {p}") from None
    return parse_plan(text, p)


# ---- statements

def statements(sql: str, dialect: str = "postgres") -> list[tuple[str, Stmt]]:
    """Split a block into (executable text, classified statement). Raises ValueError when it cannot be split safely."""
    out: list[tuple[str, Stmt]] = []
    if any(ln.strip().startswith("\\") for ln in sql.splitlines()):
        raise ValueError("psql meta-commands are not allowed in plans")
    for piece in _split_statements(sql, dialect):
        cl = classify(piece, dialect)
        if len(cl) != 1:
            raise ValueError(f"cannot split safely near: {piece[:60]!r}")
        out.append((piece.strip() + ";", cl[0]))
    return out


def is_destructive(st: Stmt) -> bool:
    if st.type in ("DROP", "TRUNCATE"):
        return True
    if st.type == "DELETE":
        try:
            import sqlglot

            return not sqlglot.parse_one(st.text, read="postgres").args.get("where")
        except Exception:
            return not re.search(r"\bwhere\b", st.text, re.I)
    return False


@dataclass
class StmtRow:
    where: str  # "step 1" | "rollback"
    step: int
    verify: bool
    source: str | None
    kind: str
    type: str
    destructive: bool
    hash: str
    text: str
    line: int


@dataclass
class CheckReport:
    rows: list[StmtRow]
    errors: list[str]
    warnings: list[str]
    sources: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def hashes(self) -> list[str]:
        seen: dict[str, None] = {}
        for r in self.rows:
            seen.setdefault(r.hash, None)
        return list(seen)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings, "sources": self.sources,
                "statements": [r.__dict__ for r in self.rows]}


def repo_path(hub: Path | None, cfg: Any, name: str | None) -> Path | None:
    """The git root of a configured scan (its `path`, relative to the hub)."""
    scan = next((s for s in cfg.scans if s.name == name), None)
    if scan is None or hub is None:
        return None
    return (hub / scan.path).resolve()


def check_plan(plan: Plan, cfg: Any, filename_id: str | None = None, hub: Path | None = None,
               tree: bool = True) -> CheckReport:
    """`hub` lets diff steps be checked with `git apply --check` against the current tree (`tree=False` skips that,
    used when reversing an already-applied plan)."""
    errors = list(plan.errors)
    warnings: list[str] = []
    rows: list[StmtRow] = []
    if not plan.id:
        errors.append("frontmatter: id is required")
    elif not PLAN_ID_RE.match(plan.id):
        errors.append(f"frontmatter: id {plan.id!r} is not a valid slug")
    elif filename_id and plan.id != filename_id:
        errors.append(f"frontmatter id {plan.id!r} does not match the file name {filename_id!r}")
    if not plan.title:
        errors.append("frontmatter: title is required")
    if plan.risk not in RISKS:
        errors.append(f"frontmatter: risk must be one of {', '.join(RISKS)}, got {plan.risk!r}")
    has_sql = any(b.kind == "sql" for b in [x for s in plan.steps for x in s.blocks] + plan.rollback)
    if not plan.sources and (has_sql or not plan.steps):
        errors.append("frontmatter: sources must be a non-empty list")
    if not plan.steps:
        errors.append("plan has no steps (## Steps / ### 1. title)")
    known = {s.name: s for s in cfg.sources}
    used: dict[str, None] = {}

    seen_files: dict[tuple[str, str], int] = {}

    def _secret(b: Block) -> None:
        from de_agent.secretscan import find_secret

        why = find_secret(b.sql)
        if why:
            errors.append(f"line {b.line}: {b.kind} step contains a secret ({why}); use the repo's connection-id convention")

    def add_diff(b: Block, where: str) -> None:
        from de_agent import stepexec as sx

        _secret(b)

        if b.verify:
            errors.append(f"line {b.line}: a diff block cannot be a verify block")
        if not b.repo or not b.file:
            errors.append(f"line {b.line}: diff block needs repo=<scan name> and file=<path relative to the repo>")
            return
        repo = repo_path(hub, cfg, b.repo)
        if not any(s.name == b.repo for s in cfg.scans):
            errors.append(f"line {b.line}: unknown repo {b.repo!r} (not a scan in de-agent.yaml)")
        if not sx.safe_relpath(b.file):
            errors.append(f"line {b.line}: file= must be a relative path inside the repo, got {b.file!r}")
        paths = sx.patch_paths(b.sql)
        if not paths:
            errors.append(f"line {b.line}: no ---/+++ file headers found in the patch")
        elif paths != [b.file]:
            errors.append(f"line {b.line}: the patch touches {paths}, but file= says {b.file!r} (one file per diff step)")
        if where != "rollback":
            key = (b.repo, b.file)
            if key in seen_files:
                errors.append(f"line {b.line}: step {seen_files[key]} already patches {b.file}; merge them into one diff "
                              "(each patch is checked against the current tree)")
            seen_files.setdefault(key, b.step)
        if tree and repo is not None and not any(e.startswith(f"line {b.line}:") for e in errors):
            ok, out = sx.git_apply_check(repo, b.sql)
            if not ok:
                if sx.git_apply_check(repo, b.sql, reverse=True)[0]:
                    warnings.append(f"line {b.line}: the patch is already applied to {b.file}")
                else:
                    errors.append(f"line {b.line}: patch does not apply to {b.repo}/{b.file}: {out[:200]}")
        elif tree and repo is None and hub is None:
            warnings.append(f"line {b.line}: patch not checked against the tree (no hub given)")
        rows.append(StmtRow(where, b.step, False, b.repo, "write", "DIFF", False, sx.diff_hash(b.repo, b.sql),
                            f"{b.repo}:{b.file}", b.line))

    def add_shell(b: Block, where: str) -> None:
        from de_agent import stepexec as sx

        _secret(b)

        probs = sx.shell_problems(b.sql)
        errors.extend(f"line {b.line}: {p}" for p in probs)
        if b.expect_exit is not None and not b.verify:
            errors.append(f"line {b.line}: expect_exit= is only valid on a verify block")
        if b.expect_stdout is not None and not b.verify:
            errors.append(f"line {b.line}: expect_stdout~= is only valid on a verify block")
        why = None if probs else sx.shell_destructive(b.sql)
        if why and plan.risk == "low":
            errors.append(f"line {b.line}: destructive command ({why}) in a plan marked risk: low")
        if b.verify and b.expect_exit is None and b.expect_stdout is None:
            warnings.append(f"line {b.line}: shell verify has no expect_exit=/expect_stdout~=; defaulting to expect_exit=0")
        rows.append(StmtRow(where, b.step, b.verify, b.exec, "read" if b.verify else "write", "SHELL", bool(why),
                            sx.shell_hash(b.exec, b.cwd, b.sql), b.sql, b.line))

    def add(blocks: list[Block], where: str) -> None:
        for b in blocks:
            if b.kind == "diff":
                add_diff(b, where)
                continue
            if b.kind == "shell":
                add_shell(b, where)
                continue
            if b.source is None:
                errors.append(f"line {b.line}: sql block has no source=")
                continue
            used.setdefault(b.source, None)
            if b.source not in known:
                errors.append(f"line {b.line}: unknown source {b.source!r} (not in de-agent.yaml)")
            elif b.source not in plan.sources:
                warnings.append(f"line {b.line}: source {b.source!r} is used but not listed in frontmatter sources")
            try:
                pairs = statements(b.sql)
            except Exception as ex:
                errors.append(f"line {b.line}: {ex}")
                continue
            if not pairs:
                errors.append(f"line {b.line}: empty sql block")
            if b.verify and len(pairs) != 1:
                errors.append(f"line {b.line}: a verify block must hold exactly one statement")
            if b.verify and b.expect is None:
                warnings.append(f"line {b.line}: verify block has no expect=; it only has to run without error")
            if b.expect is not None and parse_expect(b.expect) is None:
                errors.append(f"line {b.line}: unsupported expect {b.expect!r} "
                              "(use = N, != N, > N, >= N, < N, <= N, between A and B, is null, is not null)")
            for _, st in pairs:
                d = is_destructive(st)
                rows.append(StmtRow(where, b.step, b.verify, b.source, st.kind, st.type, d, st.hash, st.text, b.line))
                if st.type == "UNPARSEABLE":
                    errors.append(f"line {b.line}: statement cannot be parsed ({st.reason}); it would be refused at apply")
                if b.verify and st.kind != "read":
                    errors.append(f"line {b.line}: verify block must be read-only but classifies as a write ({st.type})")
                if not b.verify and st.kind == "write" and b.source in known and not known[b.source].writable:
                    errors.append(f"line {b.line}: {st.type} writes to source {b.source!r}, which is not `writable: true` "
                                  "in de-agent.yaml")
                if d and plan.risk == "low":
                    errors.append(f"line {b.line}: destructive {st.type} in a plan marked risk: low")

    for step in plan.steps:
        add(step.blocks, f"step {step.n}")
    add(plan.rollback, "rollback")
    if not plan.rollback:
        warnings.append("no Rollback section")
    return CheckReport(rows, errors, warnings, list(used))


def render_report(plan: Plan, rep: CheckReport) -> str:
    out = [f"plan {plan.id}: {plan.title}", f"risk: {plan.risk}   statement_timeout: {plan.statement_timeout_s}s   "
           f"sources: {', '.join(rep.sources) or '-'}", ""]
    out.append(f"{'where':10} {'source':12} {'verify':6} {'kind':5} {'type':16} {'destr':5} hash")
    for r in rep.rows:
        out.append(f"{r.where:10} {(r.source or '-'):12} {'yes' if r.verify else '':6} {r.kind:5} {r.type:16} "
                   f"{'YES' if r.destructive else '':5} {r.hash[:16]}  {r.text[:70]}")
    for w in rep.warnings:
        out.append(f"WARNING {w}")
    for e in rep.errors:
        out.append(f"ERROR   {e}")
    out.append("OK: no errors" if rep.ok else f"{len(rep.errors)} error(s)")
    return "\n".join(out)


def block_hashes(blocks: list[Block]) -> list[str]:
    from de_agent import stepexec as sx

    out: list[str] = []
    for b in blocks:
        if b.kind == "diff":
            out.append(sx.diff_hash(b.repo or "", b.sql))
        elif b.kind == "shell":
            out.append(sx.shell_hash(b.exec, b.cwd, b.sql))
        else:
            out += [st.hash for _, st in statements(b.sql)]
    return out


def blocks_covered(blocks: list[Block], ok: set[str]) -> list[tuple[Block, str]]:
    """(block, hash) for every statement NOT covered by the approved set. A diff is covered by its exact hash, or by the
    legacy whitespace-normalized hash when the exact and normalized patch texts are identical."""
    from de_agent import stepexec as sx

    bad: list[tuple[Block, str]] = []
    for b in blocks:
        if b.kind == "diff":
            cands = sx.diff_hash_candidates(b.repo or "", b.sql)
            if not any(c in ok for c in cands):
                bad.append((b, cands[0]))
        else:
            bad += [(b, h) for h in block_hashes([b]) if h not in ok]
    return bad


def plan_hashes(plan: Plan) -> list[str]:
    """Every statement hash in the plan (steps, verifies and rollback), deduplicated, in order."""
    blocks = [b for s in plan.steps for b in s.blocks] + plan.rollback
    return list(dict.fromkeys(block_hashes(blocks)))


# ---- state

def _load_json(p: Path) -> dict[str, Any] | None:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def plan_state(hub: Path, plan_id: str, key: bytes | None) -> str:
    """draft | checked | approved | applied | failed | rolled back."""
    from de_agent.approval import read_approval

    d = plans_dir(hub)
    md = d / f"{plan_id}.md"
    try:
        plan_sha = sha256_text(md.read_text(encoding="utf-8"))
    except OSError:
        return "draft"
    rb = _load_json(d / f"{plan_id}.rolledback.json")
    ap = _load_json(d / f"{plan_id}.applied.json")
    if rb and rb.get("status") == "ok" and (not ap or rb.get("finished_at", "") >= ap.get("finished_at", "")):
        return "rolled back"
    if ap:
        if ap.get("status") == "ok":
            return "applied"
        if ap.get("status") in ("failed", "running"):
            return "failed"
    data, _ = read_approval(str(d / f"{plan_id}.approved"), key, plan_id)
    if data is not None:
        try:
            plan = parse_plan(md.read_text(encoding="utf-8"), md)
            if not blocks_covered([b for st in plan.steps for b in st.blocks] + plan.rollback, set(data["hashes"])):
                return "approved"
        except Exception:
            pass
    ck = _load_json(d / f"{plan_id}.checked.json")
    if ck and ck.get("plan_sha256") == plan_sha:
        return "checked"
    return "draft"


def write_checked(hub: Path, plan: Plan, rep: CheckReport, now: str) -> None:
    p = plans_dir(hub) / f"{plan.id}.checked.json"
    p.write_text(json.dumps({"plan_id": plan.id, "plan_sha256": sha256_text(plan.text), "checked_at": now,
                             "hashes": rep.hashes}, indent=2) + "\n", encoding="utf-8")
