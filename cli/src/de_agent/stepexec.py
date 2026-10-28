"""Diff and shell plan steps (M2b section 1): normalisation, hashing, plan-time checks, and the executors `dea apply` uses.

Shell steps are one command each: `shlex.split`, `shell=False`. Metacharacters that would chain, pipe or redirect are
refused at plan check (outside quotes), so what was approved is the single program that runs. Diff steps are applied with
`git apply` and never committed.
"""
from __future__ import annotations

import hashlib
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from de_agent.sqlexec import SSH_BASE, redact

TAIL_LINES = 40
GIT_TIMEOUT_S = 60
SSH_HOST_RE = re.compile(r"^ssh:[A-Za-z0-9._@-]+$")


# ---- diffs

def normalize_patch(text: str) -> str:
    """LF endings, no trailing whitespace per line, exactly one trailing newline. This is what is HASHED; `git apply` gets
    `exact_patch` (a removed line that ends in whitespace must still match the file)."""
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


def exact_patch(text: str) -> str:
    """The patch as written in the plan: only CRLF -> LF and one trailing newline. This is what `git apply` is fed."""
    return text.replace("\r\n", "\n").rstrip("\n") + "\n"


def diff_hash(repo: str, patch: str) -> str:
    """Hash of the EXACT patch `git apply` receives (CRLF -> LF only), so trailing-whitespace edits change the hash."""
    return hashlib.sha256(f"diff\nrepo={repo}\n{exact_patch(patch)}".encode()).hexdigest()


def legacy_diff_hash(repo: str, patch: str) -> str:
    """The pre-0.1.0-rc hash over the whitespace-stripped form. Only honoured when stripping changes nothing."""
    return hashlib.sha256(f"diff\nrepo={repo}\n{normalize_patch(patch)}".encode()).hexdigest()


def diff_hash_candidates(repo: str, patch: str) -> list[str]:
    """Hashes an approval may carry for this diff: the exact one, plus the legacy one when exact == normalized."""
    out = [diff_hash(repo, patch)]
    if exact_patch(patch) == normalize_patch(patch):
        out.append(legacy_diff_hash(repo, patch))
    return out


_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def patch_paths(patch: str) -> list[str]:
    """Paths named by the file headers (---/+++), skipping /dev/null; header-like lines inside hunks are ignored."""
    out: list[str] = []
    old_n = new_n = 0
    for ln in patch.split("\n"):
        if old_n > 0 or new_n > 0:
            if ln.startswith("+"):
                new_n -= 1
            elif ln.startswith("-"):
                old_n -= 1
            elif ln.startswith("\\"):
                pass
            else:
                old_n -= 1
                new_n -= 1
            continue
        m = _HUNK.match(ln)
        if m:
            old_n = int(m.group(1)) if m.group(1) is not None else 1
            new_n = int(m.group(2)) if m.group(2) is not None else 1
            continue
        if ln.startswith(("--- ", "+++ ")):
            p = ln[4:].split("\t")[0].strip()
            if p == "/dev/null":
                continue
            if p[:2] in ("a/", "b/"):
                p = p[2:]
            if p not in out:
                out.append(p)
    return out


def safe_relpath(p: str) -> bool:
    parts = Path(p).parts
    return bool(parts) and not p.startswith("/") and ".." not in parts and ".git" not in parts


def run_git(args: list[str], cwd: Path, patch: str | None = None) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, input=patch, capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return 127, str(ex)
    return r.returncode, (r.stdout + r.stderr).strip()


def git_apply_check(repo: Path, patch: str, reverse: bool = False) -> tuple[bool, str]:
    rc, out = run_git(["apply", "--check", *(["-R"] if reverse else []), "-"], repo, exact_patch(patch))
    return rc == 0, out


def git_apply(repo: Path, patch: str, file: str, reverse: bool = False) -> str:
    """Apply (or reverse-apply) the patch; return `git diff --stat` for the file. Raises RuntimeError on failure."""
    patch = exact_patch(patch)
    ok, out = git_apply_check(repo, patch, reverse)
    if not ok:
        raise RuntimeError(f"git apply{' -R' if reverse else ''} --check failed: {out[:400]}")
    rc, out = run_git(["apply", *(["-R"] if reverse else []), "-"], repo, patch)
    if rc != 0:
        raise RuntimeError(f"git apply failed: {out[:400]}")
    _, stat = run_git(["diff", "--stat", "--", file], repo)
    if not stat:  # a new (untracked) or fully reverted file does not show in git diff
        _, stat = run_git(["apply", "--stat", *(["-R"] if reverse else []), "-"], repo, patch)
    return stat


# ---- shell

def shell_hash(exec_target: str, cwd: str | None, command: str) -> str:
    return hashlib.sha256(f"shell\nexec={exec_target}\ncwd={cwd or ''}\n{command.strip()}".encode()).hexdigest()


def shell_problems(command: str) -> list[str]:
    """Reasons this text is not a single plain command. Quoted text may hold `;` `|` `>` etc. (it is one argument)."""
    cmd = command.strip()
    if not cmd:
        return ["empty command"]
    if "\n" in cmd or "\r" in cmd:
        return ["a shell step is exactly one single-line command"]
    bad: list[str] = []
    if "`" in cmd or "$(" in cmd:
        bad.append("command substitution (` or $()")
    quote = ""
    esc = False
    for ch in cmd:
        if esc:
            esc = False
        elif ch == "\\" and quote != "'":
            esc = True
        elif quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch in "|&;<>":
            label = {"|": "pipe", "&": "& / &&", ";": ";", "<": "input redirection", ">": "output redirection"}[ch]
            if label not in bad:
                bad.append(label)
    if quote:
        bad.append("unbalanced quote")
    if bad:
        return [f"refused: {', '.join(bad)}; one plain command per step (no pipes, chaining or redirection)"]
    try:
        if not shlex.split(cmd):
            return ["empty command"]
    except ValueError as ex:
        return [f"cannot parse command ({ex})"]
    return []


_DROP_TRUNC = re.compile(r"\b(drop|truncate)\b", re.I)
_AWS_VERB = re.compile(r"^(delete|remove|terminate)(-|$)", re.I)


def shell_destructive(command: str, _depth: int = 0) -> str | None:
    """Why this command is destructive, else None. Looks inside `bash|sh|zsh -c <cmd>` and `env ... <cmd>`."""
    try:
        toks = shlex.split(command)
    except ValueError:
        return "unparseable"
    low = [t.lower() for t in toks]
    base = [Path(t).name for t in low]
    if _depth < 3:
        for k, b in enumerate(base):
            if b in ("bash", "sh", "zsh", "dash") and k + 1 < len(toks):
                for j in range(k + 1, len(toks) - 1):
                    if re.fullmatch(r"-[A-Za-z]*c", toks[j]):
                        inner = shell_destructive(toks[j + 1], _depth + 1)
                        if inner:
                            return inner
            if b == "env":
                for t in toks[k + 1:]:
                    if re.search(r"\s", t):
                        inner = shell_destructive(t, _depth + 1)
                        if inner:
                            return inner
    if "rm" in base:
        return "rm"
    if _DROP_TRUNC.search(command):
        return "drop/truncate"
    if "--full-refresh" in low:
        return "--full-refresh"
    for a, b in zip(low, low[1:]):
        if (a, b) in {("dags", "delete"), ("db", "reset")}:
            return f"{a} {b}"
    if "airflow" in base and "clear" in low:
        return "airflow tasks clear"
    if "aws" in base and any(_AWS_VERB.match(t) for t in low):
        return "aws delete/remove/terminate"
    return None


def shell_argv(command: str, exec_target: str, cwd: str | None) -> list[str]:
    """Local: the split command. ssh: ssh <host> -- [cd <cwd> &&] <each arg shlex.quoted>."""
    argv = shlex.split(command)
    if exec_target == "local":
        return argv
    host = exec_target[4:]
    pre = ["cd", shlex.quote(cwd), "&&"] if cwd else []
    return [*SSH_BASE, host, "--", *pre, *(shlex.quote(a) for a in argv)]


_EXTRA_SECRETS = [
    re.compile(r"(\b(?:secret|token|passwd|api[_-]?key|access[_-]?key)\s*[=:]\s*)[^\s'\"&]+", re.I),
    re.compile(r"(\bAuthorization:\s*(?:Bearer|Basic)\s+)\S+", re.I),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]


def redact_output(text: str) -> str:
    text = redact(text)
    for rx in _EXTRA_SECRETS:
        text = rx.sub(lambda m: (m.group(1) if m.groups() else "") + "***", text)
    return text


def tail(text: str, n: int = TAIL_LINES) -> str:
    return "\n".join(redact_output(text).splitlines()[-n:])


@dataclass
class ShellResult:
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    stdout_full: str = ""


def run_shell(command: str, exec_target: str, cwd: Path | None, timeout_s: float,
              remote_cwd: str | None = None) -> ShellResult:
    argv = shell_argv(command, exec_target, remote_cwd)
    try:
        r = subprocess.run(argv, cwd=cwd if exec_target == "local" else None, shell=False, capture_output=True,
                           text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as ex:
        raise RuntimeError(f"command timed out after {timeout_s:g}s") from ex
    except OSError as ex:
        raise RuntimeError(f"cannot run {argv[0]!r}: {ex}") from ex
    return ShellResult(r.returncode, tail(r.stdout), tail(r.stderr), r.stdout)
