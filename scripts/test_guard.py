#!/usr/bin/env python3
"""PreToolUse hook: removing or weakening a dbt test is never an incident fix (M4.1).

Inside a dbt project (an ancestor directory has dbt_project.yml) this denies, for EVERY agent:
  - Edit/Write/MultiEdit of a *.yml / *.yaml whose resulting content has fewer test entries than before, or that adds
    weakening config (`severity: warn`, `enabled: false`, `where:`, a loosened `warn_if` / `error_if`)
  - Edit/Write that deletes or empties a singular test file under the project's test-paths (default `tests/`)
  - Bash `rm` / `git rm` / `mv` / `sed -i` / `truncate` / `> file` (and a few equivalents) aimed at those files
Adding or tightening tests is allowed. `dea apply` (executor) is unaffected: it applies approved diffs with `git apply`,
not through these tools. Stdlib only, Python 3.9 compatible. Fails closed on internal errors.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _guardlib import input_too_large  # noqa: E402

MESSAGE = (
    "de-agent: removing or weakening a dbt test is never an incident fix; if the test itself is wrong, "
    "write a plan with a diff step for a human to approve."
)
YAML_EXT = (".yml", ".yaml")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SKIP_DIRS = {"target", "logs", "dbt_packages", "dbt_modules", ".git", "node_modules", ".venv", "venv", "__pycache__"}
WALK_CAP = 20000


def emit(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


# ------------------------------------------------------------------ project discovery

def find_project(path):
    """The nearest ancestor directory (of a file or directory path) that holds dbt_project.yml, or None."""
    d = os.path.abspath(path)
    if not os.path.isdir(d):
        d = os.path.dirname(d)
    while True:
        if os.path.isfile(os.path.join(d, "dbt_project.yml")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def test_paths(root):
    """`test-paths` from dbt_project.yml (flow or block list), default ['tests']."""
    try:
        text = open(os.path.join(root, "dbt_project.yml"), encoding="utf-8", errors="replace").read()
    except OSError:
        return ["tests"]
    m = re.search(r"^test-paths\s*:\s*(.*)$", text, re.M)
    if not m:
        return ["tests"]
    val = m.group(1).split("#")[0].strip()
    if val.startswith("["):
        items = [x.strip().strip("'\"") for x in val.strip("[]").split(",")]
    elif val:
        items = [val.strip("'\"")]
    else:
        items = []
        for ln in text[m.end():].splitlines():
            mm = re.match(r"^\s*-\s*(.+?)\s*(?:#.*)?$", ln)
            if mm:
                items.append(mm.group(1).strip("'\""))
            elif ln.strip() and not ln.lstrip().startswith("#"):
                break
    return [i.strip("/") for i in items if i.strip("/")] or ["tests"]


def rel_to_project(path, root):
    rel = os.path.relpath(os.path.abspath(path), root).replace(os.sep, "/")
    return rel


def in_skipped(rel):
    return rel.split("/")[0] in SKIP_DIRS


def under_test_path(rel, tpaths):
    return any(rel == t or rel.startswith(t + "/") for t in tpaths)


# ------------------------------------------------------------------ yaml scanning (indentation-aware, stdlib only)

_KEY_RE = re.compile(r"^(\s*)(?:-\s+)?(?:tests|data_tests)\s*:\s*(.*?)\s*$")


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _flow_items(text):
    """Number of top-level items in a flow list body like `[unique, {accepted_values: {values: [a, b]}}]`."""
    depth, n, seen = 0, 0, False
    quote = None
    for ch in text:
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            seen = True
        elif ch in "[{(":
            depth += 1
            seen = True
        elif ch in "]})":
            depth -= 1
        elif ch == "," and depth == 0:
            n += 1
            seen = False
        elif not ch.isspace():
            seen = True
    return n + (1 if seen else 0)


def count_tests(text):
    """Count test entries under every `tests:` / `data_tests:` key (flow lists and block lists)."""
    lines = text.splitlines()
    total = 0
    i = 0
    while i < len(lines):
        raw = lines[i]
        if raw.lstrip().startswith("#"):
            i += 1
            continue
        m = _KEY_RE.match(raw)
        if not m:
            i += 1
            continue
        key_indent = len(m.group(1)) + (2 if re.match(r"^\s*-\s", raw) else 0)
        rest = re.sub(r"\s+#.*$", "", m.group(2))
        if rest.startswith("["):
            body = rest
            j = i
            while body.count("[") + body.count("{") > body.count("]") + body.count("}") and j + 1 < len(lines):
                j += 1
                body += " " + lines[j].strip()
            total += _flow_items(body.strip()[1:-1] if body.strip().endswith("]") else body.strip()[1:])
            i = j + 1
            continue
        if rest and rest not in ("|", ">"):
            i += 1  # `tests: something` scalar: not a list we count
            continue
        item_indent = None
        j = i + 1
        while j < len(lines):
            ln = lines[j]
            if not ln.strip() or ln.lstrip().startswith("#"):
                j += 1
                continue
            ind = _indent(ln)
            is_item = ln.lstrip().startswith("- ") or ln.strip() == "-"
            if item_indent is None:
                if is_item and ind >= key_indent:
                    item_indent = ind
                else:
                    break
            if ind < item_indent or (ind == key_indent and not is_item):
                break
            if ind == item_indent and is_item:
                total += 1
            j += 1
        i = j
    return total


_WEAK = {
    "severity_warn": re.compile(r"""(?:^|[\s+{,])severity\s*:\s*['"]?warn\b""", re.I),
    "enabled_false": re.compile(r"""(?:^|[\s+{,])enabled\s*:\s*['"]?(?:false|no|0)\b""", re.I),
    "where": re.compile(r"""(?:^|[\s+{,])where\s*:""", re.I),
}
_IF_RE = re.compile(r"""(?:^|[\s+{,])(?:warn_if|error_if)\s*:\s*['"]?\s*(.*?)\s*['"]?\s*$""", re.I)
_STRICT_IF = {">0", "> 0", "!=0", "!= 0", ">=1", ">= 1"}


def weak_counts(text):
    counts = {k: 0 for k in _WEAK}
    counts["loose_if"] = 0
    for ln in text.splitlines():
        s = ln.split(" #")[0]
        if s.lstrip().startswith("#"):
            continue
        for k, rx in _WEAK.items():
            if rx.search(s):
                counts[k] += 1
        m = _IF_RE.search(s)
        if m and m.group(1).replace(" ", "") not in {x.replace(" ", "") for x in _STRICT_IF}:
            counts["loose_if"] += 1
    return counts


def yaml_verdict(before, after):
    """Deny reason when `after` has fewer tests or more weakening config than `before`, else None."""
    nb, na = count_tests(before), count_tests(after)
    if na < nb:
        return f"removes a dbt test ({nb} -> {na} test entries)"
    wb, wa = weak_counts(before), weak_counts(after)
    for k in wa:
        if wa[k] > wb[k]:
            return f"adds weakening config to a dbt test ({k.replace('_', ' ')})"
    return None


def sql_is_empty(text):
    """No code left: only whitespace and comments (`--`, `/* */`, `{# #}`)."""
    t = re.sub(r"/\*.*?\*/|\{#.*?#\}", "", text, flags=re.S)
    t = re.sub(r"--[^\n]*", "", t)
    return not t.strip()


# ------------------------------------------------------------------ Edit / Write

def read_text(path):
    try:
        return open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None


def apply_edit(text, old, new, replace_all):
    if old not in text:
        return text  # the Edit tool itself will fail; nothing is removed
    return text.replace(old, new) if replace_all else text.replace(old, new, 1)


def after_content(tool, ti, before):
    if tool == "Write":
        return str(ti.get("content", ""))
    if before is None:
        return None
    if tool == "Edit":
        return apply_edit(before, str(ti.get("old_string", "")), str(ti.get("new_string", "")), bool(ti.get("replace_all")))
    if tool == "MultiEdit":
        cur = before
        for e in ti.get("edits") or []:
            cur = apply_edit(cur, str(e.get("old_string", "")), str(e.get("new_string", "")), bool(e.get("replace_all")))
        return cur
    return None


def check_edit(tool, ti, cwd):
    p = str(ti.get("file_path") or ti.get("path") or ti.get("notebook_path") or "")
    if not p:
        return None
    if not os.path.isabs(p):
        p = os.path.join(cwd or ".", p)
    p = os.path.normpath(os.path.expanduser(p))
    root = find_project(p)
    if root is None:
        return None
    rel = rel_to_project(p, root)
    if rel.startswith("..") or in_skipped(rel):
        return None
    before = read_text(p)
    after = after_content(tool, ti, before)
    if p.endswith(YAML_EXT):
        why = yaml_verdict(before or "", after if after is not None else (before or ""))
        if why:
            return why
    if under_test_path(rel, test_paths(root)) and before is not None and after is not None:
        if before.strip() and (not after.strip() or (not p.endswith(YAML_EXT) and sql_is_empty(after) and not sql_is_empty(before))):
            return "deletes or empties a singular test file"
    return None


# ------------------------------------------------------------------ Bash

WRAPPERS = {"sudo", "env", "nohup", "time", "command", "exec", "xargs", "nice", "stdbuf", "builtin"}
REMOTE = {"docker", "podman", "kubectl", "ssh", "bash", "sh", "zsh", "dash", "su", "chroot", "nsenter", "orb", "colima"}
INTERPRETERS = ("python", "node", "ruby", "perl", "php")
WRITE_HINT = re.compile(
    r"""open\s*\([^)]*,\s*(?:mode\s*=\s*)?['"](?:w|r\+|x)|mode\s*=\s*['"](?:w|r\+|x)|\.write\s*\(|write_text|write_bytes|unlink|"""
    r"""os\.remove|rmtree|os\.rename|shutil\.(?:move|copy)|writeFile|File\.(?:write|delete)|sed\s+-i|\brm\b|\bmv\b""", re.I)
PATH_IN_TEXT = re.compile(r"[\w./~*{}@+-]+\.ya?ml\b")
REDIR_TOKENS = {">", ">|", "&>"}
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def split_commands(command):
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


def has_glob(s):
    return any(ch in s for ch in "*?[")


def _walk_has_protected(d, root, tpaths):
    n = 0
    for dirpath, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for name in files:
            n += 1
            if n > WALK_CAP:
                return True
            full = os.path.join(dirpath, name)
            rel = rel_to_project(full, root)
            if name.endswith(YAML_EXT) or under_test_path(rel, tpaths):
                return True
    return False


def protected(arg, cwd):
    """Is `arg` (a path or glob from a shell command) a dbt test yml or a file under the project's test-paths?"""
    a = os.path.expanduser(arg.strip().strip("'\""))
    if not a or a.startswith("-") or a in ("/dev/null", "/dev/stdout", "/dev/stderr"):
        return False
    if not os.path.isabs(a):
        a = os.path.join(cwd or os.getcwd(), a)
    a = os.path.normpath(a)
    wild = has_glob(a)
    if wild:
        base = a
        while has_glob(base):
            base = os.path.dirname(base)
        probe = base or "/"
    else:
        probe = a
    root = find_project(probe if os.path.exists(probe) else os.path.dirname(probe))
    if root is None:
        # the target may sit above a project (`rm -rf demo/*`, `rm -rf .`): any project below counts
        if os.path.isdir(probe):
            for _dirpath, dirs, files in os.walk(probe):
                dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
                if "dbt_project.yml" in files:
                    return True
        return False
    tpaths = test_paths(root)
    rel = rel_to_project(probe, root)
    if rel.startswith("..") or in_skipped(rel):
        return False
    if wild:
        leaf = a.rsplit("/", 1)[-1]
        if under_test_path(rel, tpaths) or rel == ".":
            return True
        return any(fnmatch.fnmatch(s, leaf) for s in ("schema.yml", "x.yaml", "_models.yml", "sources.yml")) or has_glob(os.path.dirname(a)[len(probe):])
    if os.path.isdir(a):
        return rel == "." or under_test_path(rel, tpaths) or any(rel == t or t.startswith(rel + "/") for t in tpaths) \
            or _walk_has_protected(a, root, tpaths)
    return a.endswith(YAML_EXT) or under_test_path(rel, tpaths)


def remote_text_hit(word):
    """A path inside a container / remote host cannot be resolved here; flag the obvious dbt test locations."""
    w = word.strip("'\"")
    if w.endswith(YAML_EXT) and re.search(r"(?:^|/)(?:models|tests|snapshots|seeds|dbt)(?:/|$)", w):
        return True
    return bool(re.search(r"(?:^|/)tests?/", w) and re.search(r"dbt|\.sql$|\.ya?ml$", w))


_REDIR = re.compile(r"^(\d*|&)(>>|>\||>&|>|<<<|<<|<&|<)(.*)$", re.S)


def strip_redirects(words):
    """(args without redirections, [targets that a redirect overwrites])."""
    out, targets = [], []
    i = 0
    while i < len(words):
        w = words[i]
        m = _REDIR.match(w)
        if not m:
            out.append(w)
            i += 1
            continue
        pre, op, operand = m.groups()
        if not operand and i + 1 < len(words):
            operand = words[i + 1]
            i += 1
        overwrite = op in (">", ">|") or (op == ">&" and operand and not re.fullmatch(r"\d+-?|-", operand))
        if overwrite and operand:
            targets.append(operand)
        i += 1
    return out, targets


def check_simple(words, cwd, depth=0):
    args_all, redirs = strip_redirects(words)
    for t in redirs:
        if t and protected(t, cwd):
            return "overwrites a dbt test file with a shell redirect"
    i = 0
    while i < len(args_all) and (ASSIGN_RE.match(args_all[i]) or args_all[i] in WRAPPERS):
        i += 1
    if i >= len(args_all):
        return None
    name = os.path.basename(args_all[i])
    args = args_all[i + 1:]
    nonopt = [a for a in args if not a.startswith("-")]
    # nested shells: bash -c "rm tests/x.sql"
    if name in ("bash", "sh", "zsh", "dash") and "-c" in args and depth < 3:
        k = args.index("-c")
        if k + 1 < len(args):
            for sub in split_commands(args[k + 1]):
                why = check_simple(sub, cwd, depth + 1)
                if why:
                    return why
    verbs = None
    if name in ("rm", "unlink", "shred", "truncate", "mv", "rmdir"):
        verbs = nonopt
    elif name == "git" and any(x in ("rm", "mv") for x in nonopt[:2]):
        verbs = nonopt[1:]
    elif name == "cp" and nonopt:
        verbs = nonopt[-1:]
    elif name in ("sed", "gsed") and any(a == "--in-place" or a.startswith("--in-place=") or re.match(r"^-[A-Za-z]*i", a) for a in args):
        verbs = nonopt
    elif name in ("perl", "ruby") and any(re.match(r"^-[A-Za-z]*i", a) for a in args):
        verbs = nonopt
    elif name == "tee" and not any(a in ("-a", "--append") for a in args):
        verbs = nonopt
    elif name == "dd":
        verbs = [a[3:] for a in args if a.startswith("of=")]
    elif name in ("ex", "ed", "vi", "vim", "nvim", "view", "nano", "micro", "emacs"):
        verbs = nonopt  # scripted editing: `ex -sc '%d|x' f`, `vi -c 'g/x/d' -c wq f`, `ed f <<EOF`
    elif name == "find" and ("-delete" in args or "-exec" in args):
        verbs = nonopt[:1] if nonopt else ["."]
    if verbs is not None:
        for a in verbs:
            if protected(a, cwd):
                return "removes, moves or rewrites a dbt test file from the shell"
    if name.startswith(INTERPRETERS):
        text = " ".join(args)
        if WRITE_HINT.search(text):
            for pth in PATH_IN_TEXT.findall(text):
                if protected(pth, cwd):
                    return "rewrites a dbt test file from a script"
    if name in REMOTE and depth < 3:
        # docker compose exec <svc> rm .../schema.yml ; ssh host 'sed -i ...' : paths are not resolvable here
        joined = " ".join(args)
        if re.search(r"(?:^|\s|;|&|'|\")(?:rm|mv|unlink|truncate|shred)\s|sed\s+(?:-\w*i|--in-place)|>\s*\S+\.ya?ml", joined) \
                and any(remote_text_hit(w) for w in re.split(r"[\s'\"=;&|]+", joined)):
            return "removes or rewrites a dbt test file inside a container or remote shell"
    return None


def check_bash(command, cwd):
    try:
        cmds = split_commands(command)
    except ValueError:
        cmds = [command.split()]
    for words in cmds:
        why = check_simple(words, cwd)
        if why:
            return why
        if words and os.path.basename(words[0]) in ("cd", "pushd") and len(words) > 1 and not words[1].startswith("-"):
            tgt = os.path.expanduser(words[1])
            cwd = os.path.normpath(tgt if os.path.isabs(tgt) else os.path.join(cwd or os.getcwd(), tgt))
    return None


# ------------------------------------------------------------------ entry point

def main():
    try:
        data = json.loads(sys.stdin.read())
        if not isinstance(data, dict):
            raise ValueError("hook input is not a JSON object")
    except Exception as ex:
        emit(f"de-agent guard error: {ex}")
        return
    try:
        tool = data.get("tool_name", "")
        ti = data.get("tool_input") or {}
        cwd = data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        why = None
        big = input_too_large(data)
        if big:
            emit(big)
            return
        if tool in EDIT_TOOLS:
            why = check_edit(tool, ti, cwd)
        elif tool == "Bash":
            why = check_bash(str(ti.get("command", "")), cwd)
        if why:
            emit(f"{MESSAGE} ({why})")
    except BaseException as ex:  # fail closed, never crash
        emit(f"de-agent guard error: {ex}")


if __name__ == "__main__":
    main()
