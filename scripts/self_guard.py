#!/usr/bin/env python3
"""PreToolUse hook: guard the guards (SAFETY.md, "Guarding the guards").

For EVERY agent (main session included) this denies:
  1. Write/Edit/MultiEdit/NotebookEdit, and Bash writes (sed -i, >, >>, tee, cp, mv, rm, chmod, ln, install, dd of=,
     truncate, rsync, perl/ruby -i), that target hooks/, scripts/, .claude-plugin/, agents/ or skills/ under
     $CLAUDE_PLUGIN_ROOT. Maintainers developing the plugin itself start Claude with DEA_ALLOW_PLUGIN_EDITS=1.
     Also protected: `cli/src` and `cli/pyproject.toml` (the classifiers the hooks import), `git -C $CLAUDE_PLUGIN_ROOT
     checkout|pull|reset|...`, and `pip install --force-reinstall|-e ... de-agent` / `pip uninstall de-agent` (also `uv pip`).
  2. Edits to a hub's de-agent.yaml that change the effective `guard:` mode, the `guard.cli_allow` list or any `writable:`
     value (before/after is computed like test_guard). Adding sources or scans stays allowed. Bash writes to de-agent.yaml
     are denied (use Edit), including interpreter and tool writes: a command that mentions de-agent.yaml (or a glob that
     matches it) together with python/perl/node/ruby code that writes, awk -i inplace, ed, ex, patch, git apply|checkout|
     restore, yq -i, sponge, vim -c, curl -o, wget -O or a sed `w` command. Known gap: `cp -r src/* .` where src holds one.
  3. Edits to .claude/settings*.json that change `hooks` or `disableAllHooks`. Bash writes to those files are denied.
Stdlib only, Python 3.9 compatible. Fails closed on internal errors. Shell indirection / obfuscation is out of scope.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _guardlib import input_too_large, parse_cli_allow  # noqa: E402
from test_guard import _REDIR, after_content, read_text, split_commands  # noqa: E402

PROTECTED = ("hooks", "scripts", ".claude-plugin", "agents", "skills")
PROTECTED_PATHS = (("cli", "src"), ("cli", "pyproject.toml"))
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
    parts = rel.split(os.sep)
    return parts[0] in PROTECTED or tuple(parts[:2]) in PROTECTED_PATHS


def in_root(real, root):
    return bool(root) and (real == root or real.startswith(root + os.sep))


def is_yaml(real):
    b = os.path.basename(real)
    return b == "de-agent.yaml" or (_glob_tok(b) and fnmatch.fnmatchcase("de-agent.yaml", b))


def _glob_tok(t):
    return any(c in t for c in "*?[") and "{" not in t and len(re.sub(r"[*?\[\]]", "", t)) >= 3


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
    if parse_cli_allow(before) != parse_cli_allow(after):
        return "changes `guard.cli_allow` in de-agent.yaml (it loosens the CLI write guard)"
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
    if root:
        why = _git_verdict(name, args, cwd, root) or _pip_verdict(name, args, cwd, root)
        if why:
            return why
    return None


_GIT_SUBS = {"checkout", "apply", "pull", "reset", "restore", "stash", "merge", "rebase", "am", "cherry-pick", "clean", "switch",
             "revert"}


def _git_verdict(name, args, cwd, root):
    """`git -C $CLAUDE_PLUGIN_ROOT pull` and friends rewrite the hooks and the classifiers they import."""
    if name != "git":
        return None
    dirs, i = [], 0
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a in ("-C", "--git-dir", "--work-tree") and i + 1 < len(args):
            dirs.append(args[i + 1])
            i += 2
            continue
        if a.startswith(("--git-dir=", "--work-tree=")):
            dirs.append(a.split("=", 1)[1])
        i += 2 if a in ("-c", "--namespace", "--exec-path") else 1
    if i >= len(args) or args[i] not in _GIT_SUBS:
        return None
    if any(in_root(resolve(d, cwd), root) for d in (dirs or ["."])):
        return f"{MSG}: no agent may run `git {args[i]}` in the plugin's own checkout{HATCH}"
    return None


def _pip_verdict(name, args, cwd, root):
    """`pip install --force-reinstall|-e ... de-agent` and `pip uninstall de-agent` (also `uv pip`, `python -m pip`)."""
    toks = None
    if re.fullmatch(r"pip[0-9.]*", name):
        toks = args
    elif name == "uv" and "pip" in args:
        toks = args[args.index("pip") + 1:]
    elif re.fullmatch(r"python[0-9.]*", name) and "-m" in args and args[args.index("-m") + 1:args.index("-m") + 2] == ["pip"]:
        toks = args[args.index("-m") + 2:]
    if toks is None:
        return None
    sub = next((a for a in toks if not a.startswith("-")), "")
    if sub not in ("install", "uninstall"):
        return None
    mention = any(re.search(r"de[-_]agent", a) for a in toks) or any(
        in_root(resolve(a, cwd), root) for a in toks if not a.startswith("-") and (a == "." or "/" in a or a.startswith(".")))
    force = any(a in ("--force-reinstall", "--reinstall", "-e", "--editable") or a.startswith("--editable=") for a in toks)
    if mention and (sub == "uninstall" or force):
        return f"{MSG}: no agent may reinstall or uninstall the de-agent package the hooks import{HATCH}"
    return None


_PY_NAME = re.compile(r"python[0-9.]*$")
_PY_WRITE = re.compile(
    r"""open\s*\(.*?,\s*(?:mode\s*=\s*)?['"][rbt]*[wax+]|\.open\s*\(\s*['"][rbt]*[wax+]|\.write\s*\(|write_text|write_bytes|"""
    r"""\.dump\s*\(|shutil\.|os\.(?:rename|replace|remove|unlink|truncate|system|popen)|\.unlink\s*\(|\.touch\s*\(|"""
    r"""\.rename\s*\(|subprocess|\bexec\s*\(|__import__""", re.S)
_EDITORS = {"vi", "vim", "nvim", "view", "ex", "gvim", "vimdiff", "nano", "pico"}


def mentions_yaml(text):
    if "de-agent.yaml" in text:
        return True
    return any(_glob_tok(t) and fnmatch.fnmatchcase("de-agent.yaml", t) for t in re.findall(r"[\w.*?\[\]{}-]+", text))


def _yaml_writer(command, cmds):
    """The program among the commands that can write a mentioned de-agent.yaml (interpreters, patchers, editors, downloaders)."""
    if not mentions_yaml(command):
        return None
    for words in cmds:
        for k, w in enumerate(words):
            name = os.path.basename(w)
            rest = words[k + 1:]
            if _PY_NAME.match(name) and _PY_WRITE.search(command):
                return name
            if name in ("perl", "ruby") and any(re.match(r"-[A-Za-z]*[eEi]", a) for a in rest):
                return name
            if name in ("node", "nodejs", "bun", "deno", "php") and any(a in ("-e", "--eval", "-p", "--print", "-r", "run") for a in rest):
                return name
            if name in ("awk", "gawk", "mawk", "nawk") and (
                    ("inplace" in rest and any(a.startswith("-i") for a in rest))
                    or any(mentions_yaml(a) and re.search(r">>?\s*[\"']|system\s*\(|\|\s*[\"']", a) for a in rest)):
                return name
            if name in ("ed", "patch", "sponge", "ex"):
                return name
            if name == "git" and any(a in ("apply", "checkout", "restore", "am", "checkout-index", "mv", "rm", "stash", "reset") for a in rest):
                return name
            if name == "yq" and any(a == "--inplace" or re.match(r"-[A-Za-z]*i", a) for a in rest if not a.startswith("--")):
                return name
            if name == "yq" and "--inplace" in rest:
                return name
            if name in _EDITORS and any(re.match(r"^(?:-[A-Za-z]*[cseS]|--cmd|\+.*)$", a) for a in rest):
                return name
            if name == "curl" and any(a in ("--output", "--remote-name", "--output-dir") or a.startswith("--output=") or
                                      (re.match(r"-[A-Za-z]*[oO]", a) and not a.startswith("--")) for a in rest):
                return name
            if name == "wget" and any(a.startswith(("--output-document", "--output-file")) or
                                      (re.match(r"-[A-Za-z]*[Oo]", a) and not a.startswith("--")) for a in rest):
                return name
            if name in ("sed", "gsed") and any(mentions_yaml(a) and re.search(r"(?:^|[^A-Za-z_\\])w[ \t]*\S*de-agent", a) for a in rest):
                return name
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
    w = _yaml_writer(command, cmds)
    if w:
        return f"{MSG}: `{w}` can write to de-agent.yaml (it holds the guard mode and writable sources); use the Edit tool"
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
