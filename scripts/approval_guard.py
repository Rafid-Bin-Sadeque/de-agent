#!/usr/bin/env python3
"""PreToolUse hook: plan approval is human-only; `dea apply` is executor-only (M2 section 1, layer 3).

Denies, for EVERY agent including the main session (absent agent_type):
  - Bash commands that run `dea approve`, or mention `approve.key` or `.approved`
  - Write/Edit/MultiEdit/NotebookEdit on `.dea/plans/*.approved` or approve.key
Denies `dea apply` from Bash unless agent_type == "de-agent:executor".
Stdlib only, Python 3.9 compatible. Fails closed on malformed input that looks like an approval or apply attempt.
"""
from __future__ import annotations

import json
import os
import re
import sys

EXECUTOR = "de-agent:executor"
_LEAD = r"(?:^|[\s;&|(`'\"/=])"
APPROVE_RE = re.compile(_LEAD + r"(?:dea|de_agent(?:\.cli)?)\s+(?:-\S+\s+)*approve\b")
APPLY_RE = re.compile(_LEAD + r"(?:dea|de_agent(?:\.cli)?)\s+(?:-\S+\s+)*apply\b")
GENERIC_RE = re.compile(r"approve\.key|\.approved\b")
LOOSE_RE = re.compile(r"approve\b|\bapply\b|approve\.key|\.approved")
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

HUMAN_ONLY = (
    "de-agent: approving a plan is a human-only action. Ask the human to run `dea approve <id>` in their own "
    "terminal; no agent can approve, edit approval files, or read the approval key."
)
EXECUTOR_ONLY = "de-agent: `dea apply` runs only from the executor agent; hand the plan to the executor agent."


def emit(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def path_is_protected(p):
    norm = os.path.normpath(p).replace("\\", "/")
    if os.path.basename(norm) == "approve.key":
        return True
    return norm.endswith(".approved") and "/.dea/plans/" in "/" + norm.lstrip("/")


def check(data):
    """Return a deny reason or None."""
    tool = data.get("tool_name", "")
    ti = data.get("tool_input") or {}
    if tool == "Bash":
        cmd = str(ti.get("command", ""))
        if APPROVE_RE.search(cmd) or GENERIC_RE.search(cmd):
            return HUMAN_ONLY
        if APPLY_RE.search(cmd) and data.get("agent_type") != EXECUTOR:
            return EXECUTOR_ONLY
    elif tool in WRITE_TOOLS:
        for k in ("file_path", "path", "notebook_path"):
            if ti.get(k) and path_is_protected(str(ti[k])):
                return HUMAN_ONLY
    return None


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("hook input is not a JSON object")
    except Exception as ex:
        if LOOSE_RE.search(raw):
            emit(f"de-agent guard error: {ex}; {HUMAN_ONLY}")
        return
    try:
        why = check(data)
        if why:
            emit(why)
    except BaseException as ex:  # fail closed
        emit(f"de-agent guard error: {ex}")


if __name__ == "__main__":
    main()
