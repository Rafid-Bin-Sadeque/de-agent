#!/usr/bin/env python3
"""PreToolUse hook: guard the guards (SAFETY.md, "Guarding the guards").

For EVERY agent (main session included) this denies:
  1. Write/Edit/MultiEdit/NotebookEdit, and Bash writes (sed -i, >, >>, tee, cp, mv, rm, chmod, ln, install, dd of=,
     truncate, rsync, perl/ruby -i), that target hooks/, scripts/, .claude-plugin/, agents/ or skills/ under
     $CLAUDE_PLUGIN_ROOT. Maintainers developing the plugin itself start Claude with DEA_ALLOW_PLUGIN_EDITS=1.
  2. Edits to a hub's de-agent.yaml that change the effective `guard:` mode or any `writable:` value (before/after is
     computed like test_guard). Adding sources or scans stays allowed. Bash writes to de-agent.yaml are denied (use Edit).
  3. Edits to .claude/settings*.json that change `hooks` or `disableAllHooks`. Bash writes to those files are denied.
Stdlib only, Python 3.9 compatible. Fails closed on internal errors. Shell indirection / obfuscation is out of scope.
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _guardlib import input_too_large  # noqa: E402
from test_guard import _REDIR, after_content, read_text, split_commands  # noqa: E402

PROTECTED = ("hooks", "scripts", ".claude-plugin", "agents", "skills")
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
WRAPPERS = {"sudo", "env", "nohup", "time", "command", "exec", "nice", "stdbuf", "builtin"}
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
MSG = "de-agent: the guards protect themselves"
HATCH = (". If you are a human developing de-agent itself, quit Claude and restart it with "
         "DEA_ALLOW_PLUGIN_EDITS=1 set in your shell (see CONTRIBUTING.md)")
_WRITE_ALL = {"rm", "unlink", "shred", "rmdir", "mv", "chmod", "chown", "chgrp", "truncate", "tee", "ln", "touch"}


def emit(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))


def plugin_root():
    r = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not r or os.environ.get("DEA_ALLOW_PLUGIN_EDITS") == "1":
        return None
    return os.path.realpath(r)


def resolve(p, cwd):
    p = os.path.expandvars(os.path.expanduser(str(p).strip().strip("'\"")))
    if not os.path.isabs(p):
        p = os.path.join(cwd or os.getcwd(), p)
    return os.path.realpath(p)


def in_plugin(real, root):
    if not root:
        return False
    rel = os.path.relpath(real, root)
    if rel.startswith(".."):
        return False
    return rel.split(os.sep)[0] in PROTECTED


def is_yaml(real):
    return os.path.basename(real) == "de-agent.yaml"


def is_settings(real):
    b = os.path.basename(real)
    return b.startswith("settings") and b.endswith(".json") and os.path.basename(os.path.dirname(real)) == ".claude"


def _mode(text):
    m = re.search(r"^guard\s*:\s*(\{[^}]*\}|(?:\n[ \t]+.*)+)", text, re.M)
    if m:
        mm = re.search(r"mode\s*:\s*[\"']?(strict|ask)", m.group(1))
        if mm:
            return mm.group(1)
    return "strict"


def _writable(text):
    out = []
    for ln in text.splitlines():
        if ln.lstrip().startswith("#"):
            continue
        out += [v.strip("'\"").lower() for v in re.findall(r"\bwritable\s*:\s*([^,}\s#]+)", ln)]
    return out


def yaml_verdict(before, after):
    before = before or ""
    if _mode(before) != _mode(after):
        return "changes the `guard:` mode in de-agent.yaml"
    if _writable(before) != _writable(after):
        return "sets or alters a `writable:` source in de-agent.yaml"
    return None


def settings_verdict(before, after):
    def parse(t):
        try:
            d = json.loads(t) if t and t.strip() else {}
            return d if isinstance(d, dict) else None
        except ValueError:
            return None

    b, a = parse(before), parse(after)
    if b is not None and a is not None:
        for k in ("hooks", "disableAllHooks"):
            if b.get(k) != a.get(k):
                return f"changes `{k}` in a Claude settings file"
        return None
    for w in ("hooks", "disableAllHooks"):
        if (before or "").count(w) != (after or "").count(w):
            return f"changes `{w}` in a Claude settings file"
    return None


def check_edit(tool, ti, cwd, root):
    for k in ("file_path", "path", "notebook_path"):
        if not ti.get(k):
            continue
        real = resolve(ti[k], cwd)
        if in_plugin(real, root):
            return f"{MSG}: no agent may edit the plugin's own hooks, scripts, agents or skills ({os.path.relpath(real, root)}){HATCH}"
        if is_yaml(real) or is_settings(real):
            before = read_text(real)
            after = after_content(tool, ti, before)
            if after is None:
                return f"{MSG}: cannot compute the result of this edit to {os.path.basename(real)}"
            why = (yaml_verdict if is_yaml(real) else settings_verdict)(before, after)
            if why:
                return f"{MSG}: this edit {why}; a human changes that"
    return None


def _targets(name, args):
    """Path arguments a write-ish command would modify (best effort)."""
    nonopt = [a for a in args if not a.startswith("-")]
    if name in _WRITE_ALL:
        return nonopt
    if name in ("sed", "gsed") and any(a == "--in-place" or a.startswith("--in-place=") or re.match(r"^-[A-Za-z]*i", a) for a in args):
        return nonopt
    if name in ("perl", "ruby") and any(re.match(r"^-[A-Za-z]*i", a) for a in args):
        return nonopt
    if name in ("cp", "install", "rsync", "scp") and nonopt:
        t = [a.split("=", 1)[1] for a in args if a.startswith("--target-directory=")]
        return t or nonopt[-1:]
    if name == "dd":
        return [a[3:] for a in args if a.startswith("of=")]
    return []


def strip_redirects(words):
    """(args without redirections, [every target a redirect writes: >, >|, >>, &>])."""
    out, targets = [], []
    i = 0
    while i < len(words):
        m = _REDIR.match(words[i])
        if not m:
            out.append(words[i])
            i += 1
            continue
        pre, op, operand = m.groups()
        if not operand and i + 1 < len(words):
            operand = words[i + 1]
            i += 1
        if op in (">", ">|", ">>") or (op == ">&" and operand and not re.fullmatch(r"\d+-?|-", operand)):
            targets.append(operand)
        i += 1
    return out, targets


def check_simple(words, cwd, root, depth=0):
    args_all, redirs = strip_redirects(words)
    for t in redirs:
        why = _target_verdict(t, cwd, root)
        if why:
            return why
    i = 0
    while i < len(args_all) and (ASSIGN_RE.match(args_all[i]) or args_all[i] in WRAPPERS):
        i += 1
    if i >= len(args_all):
        return None
    name = os.path.basename(args_all[i])
    args = args_all[i + 1:]
    if name in ("bash", "sh", "zsh", "dash") and "-c" in args and depth < 3:
        k = args.index("-c")
        if k + 1 < len(args):
            for sub in split_commands(args[k + 1]):
                why = check_simple(sub, cwd, root, depth + 1)
                if why:
                    return why
    for a in _targets(name, args):
        why = _target_verdict(a, cwd, root)
        if why:
            return why
    return None


def _target_verdict(path, cwd, root):
    if not path or path in ("/dev/null", "/dev/stdout", "/dev/stderr"):
        return None
    real = resolve(path, cwd)
    if in_plugin(real, root):
        return f"{MSG}: no agent may write to the plugin's own hooks, scripts, agents or skills ({os.path.relpath(real, root)}){HATCH}"
    if is_yaml(real):
        return f"{MSG}: shell writes to de-agent.yaml are blocked (it holds the guard mode and writable sources); use the Edit tool"
    if is_settings(real):
        return f"{MSG}: shell writes to Claude settings files are blocked (they hold the hooks); a human edits those"
    return None


def check_bash(command, cwd, root):
    try:
        cmds = split_commands(command)
    except ValueError:
        cmds = [command.split()]
    for words in cmds:
        why = check_simple(words, cwd, root)
        if why:
            return why
        if words and os.path.basename(words[0]) in ("cd", "pushd") and len(words) > 1 and not words[1].startswith("-"):
            cwd = resolve(words[1], cwd)
    return None


def main():
    try:
        data = json.loads(sys.stdin.read())
        if not isinstance(data, dict):
            raise ValueError("hook input is not a JSON object")
    except Exception as ex:
        emit(f"de-agent guard error: {ex}")
        return
    try:
        why = input_too_large(data)
        tool = data.get("tool_name", "")
        ti = data.get("tool_input") or {}
        cwd = data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        root = plugin_root()
        if not why and tool in EDIT_TOOLS:
            why = check_edit(tool, ti, cwd, root)
        elif not why and tool == "Bash":
            why = check_bash(str(ti.get("command", "")), cwd, root)
        if why:
            emit(why)
    except BaseException as ex:
        emit(f"de-agent guard error: {ex}")


if __name__ == "__main__":
    main()
