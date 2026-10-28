#!/usr/bin/env python3
"""PreToolUse hook: keep credentials out of the transcript (PLAN section 4.3 / P4).

Denies inline secrets in Bash commands and reads of credential files (Bash readers and the Read tool).
Fails closed on malformed input or internal errors.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "cli", "src")]
from _guardlib import input_too_large  # noqa: E402

URI_RE = re.compile(r"\b(postgres(ql)?|redshift|mysql|mariadb|mongodb(\+srv)?)://[^:/\s@]+:[^@\s]+@", re.I)
PASSWORD_KV = re.compile(r"\bpassword=[^\s'\"&]+", re.I)
ASSIGN_RE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<val>.+)$", re.S)
SECRET_ENV = re.compile(r"^(PGPASSWORD|MYSQL_PWD|AWS_SECRET_ACCESS_KEY|.*(PASSWORD|PASSWD).*)$", re.I)
DSN_HINT = re.compile(r"\b(host|dbname|user|port)=|\?", re.I)
try:
    from de_agent.clients import DB_CLIENTS
except Exception:  # fail closed in main(): without the shared list nothing is checked
    DB_CLIENTS = None
SCRIPT_RUNNERS = ("python", "node", "ruby", "perl", "php")
READERS = {
    "cat", "less", "more", "head", "tail", "grep", "egrep", "fgrep", "rg", "sed", "awk", "bat",
    "strings", "xxd", "od", "base64", "cp", "source", ".", "nl", "tac", "cut", "sort", "diff", "open",
    "ln", "link", "install", "rsync", "mv", "gpg", "gpg2", "vi", "vim", "nvim", "view", "nano", "emacs", "ed", "ex",
    "micro", "pico", "dd", "paste", "fold", "rev", "column", "jq", "yq", "csvcat", "zcat", "xz", "bzcat",
}
EDITORS = {"vi", "vim", "nvim", "view", "nano", "emacs", "ed", "ex", "micro", "pico"}
ENV_DUMPERS = {"printenv"}
SECRET_VAR_REF = re.compile(r"\$\{?[A-Za-z_0-9]*(?:PASS|SECRET|TOKEN|KEY)[A-Za-z_0-9]*", re.I)
_CWD = None
WRAPPERS = {"sudo", "env", "nohup", "time", "command", "exec", "xargs"}


def emit(decision: str, reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }))


def is_secret_path(arg: str) -> bool:
    """True for a credential path, judged on the path as written AND on what it resolves to (symlinks, ~, ..)."""
    a = arg.strip().strip("'\"")
    if a.startswith(("if=", "of=")) and len(a) > 3:
        a = a[3:]
    if _secret_name(a):
        return True
    if not a or a.startswith("-"):
        return False
    try:
        p = os.path.expanduser(a)
        if not os.path.isabs(p):
            p = os.path.join(_CWD or os.getcwd(), p)
        real = os.path.realpath(p)
    except (OSError, ValueError):
        return False
    return real != p and _secret_name(real)


def _secret_name(arg: str) -> bool:
    a = arg.strip().strip("'\"")
    if re.search(r"^/proc/(?:self|\d+|\*)/environ$", a):
        return True
    if not a or a.startswith("-"):
        return False
    base = os.path.basename(a.rstrip("/"))
    if base in (".pgpass", "pgpass", "approve.key"):
        return True
    if base == "de-agent" and os.path.basename(os.path.dirname(a.rstrip("/"))) == ".config":
        return True  # the dir holding approve.key (Grep over a directory would print it)
    if base == ".env" or (base.startswith(".env.") and base.split(".")[-1] not in ("example", "sample", "template", "dist")):
        return True
    if base.endswith(".pem"):
        return True
    if a.endswith(".aws/credentials") or a.endswith(".aws/config"):
        return True
    if base == ".aws" or "/.aws/sso/cache" in a or "/.aws/cli/cache" in a or a.endswith((".aws/sso", ".aws/cli")):
        return True  # the dir (Grep over it would print the files) and cached SSO / assumed-role tokens
    return False


# ---- Grep tool / grep-like globs (M4.1: `path=<dir>` + `glob=**/.env*` leaked a demo password)
SECRET_GLOB_RE = re.compile(r"\.env|\.pem\b|\.key\b|pgpass|credentials|\.aws|approve|id_rsa|id_ed25519|\.netrc|\.npmrc", re.I)
SECRET_SAMPLES = (".env", ".env.local", ".env.production", ".env.staging", "server.pem", "tls.key", "approve.key",
                  ".pgpass", "pgpass", "credentials", ".aws/credentials", ".aws/config")
SECRET_TYPES = {"env", "dotenv", "pem", "key", "secret", "secrets", "credentials", "pgpass"}
SECRET_PATTERN_RE = re.compile(r"password|passwd|\bpwd\b|secret|token|api[_-]?key|credential", re.I)
WALK_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "target", "dbt_packages", ".tox", ".mypy_cache"}
WALK_CAP = 20000


def _expand_braces(g):
    m = re.search(r"\{([^{}]*)\}", g)
    if not m:
        return [g]
    out = []
    for alt in m.group(1).split(","):
        out += _expand_braces(g[:m.start()] + alt + g[m.end():])
    return out


def split_globs(value):
    """A Grep `glob`/`include` value -> individual non-negated glob patterns (space/comma separated, braces expanded)."""
    out = []
    for piece in re.split(r"\s+|,(?![^{]*\})", str(value).strip()):
        piece = piece.strip().strip("'\"")
        if piece and not piece.startswith("!"):
            out += _expand_braces(piece)
    return out


def glob_targets_secrets(value):
    """True when a glob names or could match credential files (explicit names, or a wildcard that matches one)."""
    for g in split_globs(value):
        if SECRET_GLOB_RE.search(g):
            return True
        base = g.rstrip("/").rsplit("/", 1)[-1]
        if not base.strip("*?"):
            continue  # `*`, `**`: unrestricted, handled by the pattern rule
        full = g if "/" in g else "**/" + g
        for smp in SECRET_SAMPLES:
            if fnmatch.fnmatch(smp, g) or fnmatch.fnmatch("a/b/" + smp, full):
                return True
    return False


def _restricts(value):
    """A glob that narrows the search to named files (`*.py`, `src/**/*.sql`), not a match-everything wildcard."""
    pieces = split_globs(value)
    return bool(pieces) and all(p.rstrip("/").rsplit("/", 1)[-1].strip("*?") for p in pieces)


def tree_has_secrets(root):
    """Bounded walk: does the tree under `root` hold a credential file? Fails closed when the cap is hit."""
    if os.path.isfile(root):
        return is_secret_path(root)
    n = 0
    for _dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in WALK_SKIP]
        for name in files + dirs:
            n += 1
            if n > WALK_CAP:
                return True
            if is_secret_path(name) or name in (".pgpass", ".aws"):
                return True
    return False


def check_grep(ti, cwd):
    path = str(ti.get("path") or "")
    if path and is_secret_path(path):
        return "read of a credential file"
    globs = [str(ti[k]) for k in ("glob", "include") if ti.get(k)]
    if any(glob_targets_secrets(g) for g in globs):
        return "search over credential files (glob matches .env / key / credential files)"
    typ = str(ti.get("type") or "").lower()
    if typ in SECRET_TYPES:
        return "search over credential files (file type)"
    if SECRET_PATTERN_RE.search(str(ti.get("pattern") or "")):
        restricted = bool(typ) or any(_restricts(g) for g in globs)
        if not restricted:
            root = path or cwd or "."
            if os.path.isdir(root) and tree_has_secrets(root):
                return "search for secrets (password/token/key) where credential files are in scope"
    return None


def simple_commands(command: str) -> list[list[str]]:
    lex = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    cmds, cur = [], []
    for t in lex:
        if t and all(ch in ";&|()" for ch in t):
            if cur:
                cmds.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        cmds.append(cur)
    return cmds


def uses_db_client(cmds: list[list[str]]) -> bool:
    for words in cmds:
        names = [os.path.basename(w) for w in words]
        if any(n in DB_CLIENTS for n in names):
            return True
        if any(n.startswith(SCRIPT_RUNNERS) for n in names) and any("connect(" in w for w in words):
            return True
    return False


AWS_SECRET_KEYS = {"aws_secret_access_key", "aws_session_token", "aws_security_token"}


def aws_secret_command(args: list[str]) -> bool:
    """`aws configure get aws_secret_access_key`, `aws configure export-credentials`, `aws ... get-session-token`."""
    words = [a for a in args if not a.startswith("-")]
    if words[:2] == ["configure", "get"] and any(a.lower() in AWS_SECRET_KEYS for a in words[2:]):
        return True
    if words[:2] == ["configure", "export-credentials"] or "export-credentials" in words[:2]:
        return True
    return "get-session-token" in words[:2] or "get-secret-value" in words[:2]


def glob_option_values(args):
    vals = []
    for i, a in enumerate(args):
        for opt in ("--include=", "--glob=", "-g="):
            if a.startswith(opt):
                vals.append(a[len(opt):])
        if a in ("--include", "--glob", "-g", "--iglob") and i + 1 < len(args):
            vals.append(args[i + 1])
    return vals


def env_dump(words: list[str]) -> str | None:
    """Commands that print the whole environment, or an echo of a secret-looking variable."""
    i = 0
    while i < len(words) and (ASSIGN_RE.match(words[i]) or words[i] in WRAPPERS - {"env"}):
        i += 1
    if i >= len(words):
        return None
    name = os.path.basename(words[i])
    args = words[i + 1:]
    if name in ENV_DUMPERS:
        return "prints environment variables (may hold secrets)"
    if name == "env" and not [a for a in args if not a.startswith("-") and not ASSIGN_RE.match(a)]:
        return "prints the environment (may hold secrets)"
    if name == "set" and not args:
        return "prints shell variables (may hold secrets)"
    if name == "export" and (not args or "-p" in args):
        return "prints exported variables (may hold secrets)"
    if name in ("declare", "typeset") and any(re.fullmatch(r"-[A-Za-z]*[px][A-Za-z]*", a) for a in args):
        return "prints shell variables (may hold secrets)"
    if name == "compgen" and any(a in ("-v", "-e") for a in args):
        return "lists shell variables"
    if name in ("echo", "printf") and any(SECRET_VAR_REF.search(a) for a in args):
        return "prints a secret-looking environment variable"
    return None


def check_python(command: str, cwd) -> str | None:
    try:
        from de_agent.extract import python_codes

        for code in python_codes(command, cwd):
            from de_agent.pyscan import secret_opens

            if secret_opens(code, is_secret_path):
                return "python code opens a credential file"
    except Exception as ex:  # fail closed
        raise RuntimeError(f"python scan failed: {ex}") from ex
    return None


def check_bash(command: str, cwd=None) -> str | None:
    if URI_RE.search(command):
        return "password inside a connection URI"
    try:
        cmds = simple_commands(command)
    except ValueError:
        cmds = [command.split()]
    for words in cmds:
        why = env_dump(words)
        if why:
            return why
    why = check_python(command, cwd)
    if why:
        return why
    # (b) env assignment / export of a secret
    for words in cmds:
        for w in words:
            m = ASSIGN_RE.match(w)
            if m and SECRET_ENV.match(m.group("name")):
                lead = words[: words.index(w)]
                if all(ASSIGN_RE.match(x) or x in WRAPPERS or x == "export" or x.startswith("-")
                       for x in lead):
                    return "inline secret environment variable"
    # (a) DB client / connection tool, or (c) DSN-shaped string carrying password=
    client = uses_db_client(cmds)
    for words in cmds:
        for w in words:
            if PASSWORD_KV.search(w) and (client or DSN_HINT.search(w)):
                return "inline password= in a DSN or client command"
    for words in cmds:
        i = 0
        while i < len(words) and (ASSIGN_RE.match(words[i]) or words[i] in WRAPPERS):
            i += 1
        if i >= len(words):
            continue
        name = os.path.basename(words[i])
        args = words[i + 1:]
        if name == "aws" and aws_secret_command(args):
            return "reads an AWS secret from the credential store"
        if name in READERS and any(is_secret_path(a) for a in args):
            return "read of a credential file"
        if name in ("python", "python3") or name.startswith("python"):
            pass  # handled by check_python
        if name in EDITORS and any(is_secret_path(a) for a in args):
            return "edit/read of a credential file"
        if name == "gpg" and any(a in ("-d", "--decrypt") or a.startswith("--decrypt") for a in args):
            return "decrypts a file (may print a secret)"
        if name == "cp" and any(a in ("-s", "--symbolic-link", "-l", "--link") for a in args) and any(is_secret_path(a) for a in args):
            return "links a credential file"
        if name == "ln" and any(is_secret_path(a) for a in args):
            return "links a credential file"
        if name in ("grep", "egrep", "fgrep", "rg", "ag", "ack"):
            for gv in glob_option_values(args):
                if glob_targets_secrets(gv):
                    return "search over credential files (glob matches .env / key / credential files)"
        for j, w in enumerate(words):  # redirect: cmd < ~/.pgpass
            if w == "<" and j + 1 < len(words) and is_secret_path(words[j + 1]):
                return "read of a credential file"
    return None


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
        if not isinstance(data, dict):
            raise ValueError("hook input is not a JSON object")
    except Exception as ex:
        emit("deny", f"de-agent guard error: {ex}")
        return
    try:
        global _CWD
        if DB_CLIENTS is None:
            raise RuntimeError("shared client list unavailable (de_agent.clients)")
        tool = data.get("tool_name", "")
        ti = data.get("tool_input") or {}
        _CWD = data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR")
        why = input_too_large(data)
        if why:
            emit("deny", why)
            return
        if tool == "Bash":
            why = check_bash(str(ti.get("command", "")), _CWD)
        elif tool == "Grep":
            why = check_grep(ti, data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR"))
        elif tool == "Read":
            p = str(ti.get("file_path") or ti.get("path") or "")
            if is_secret_path(p):
                why = "read of a credential file"
        if why:
            emit("deny", f"de-agent: {why}; secrets must not enter the transcript. "
                         "Use a secret reference (env var, ~/.pgpass, AWS profile) instead.")
    except BaseException as ex:
        emit("deny", f"de-agent guard error: {ex}")


if __name__ == "__main__":
    main()
