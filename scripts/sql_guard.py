#!/usr/bin/env python3
"""PreToolUse hook: deny database writes outside an approved plan (PLAN section 4.3).

Reads hook JSON on stdin. Prints a permissionDecision JSON to deny/ask, or nothing to allow.
Fails closed: any internal error is a deny.
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# vendored sqlglot first, then the de-agent package (script-relative; no env dependency)
sys.path[:0] = [os.path.join(HERE, "_vendor"), os.path.join(os.path.dirname(HERE), "cli", "src")]

sys.path.insert(0, HERE)
from _guardlib import MAX_RAW, MAX_SQL, TOO_LARGE, input_too_large  # noqa: E402

NOT_INSTALLED = "de-agent CLI not installed: pipx install de-agent"
MCP_NAME = re.compile(r"query|sql|execute", re.I)
MCP_FIELDS = ("sql", "query", "statement")
STRICT_KEY = re.compile(r"^(sql|query|statement|statements|stmt|q|command|cmd|script|queries)$", re.I)
SQL_START = re.compile(
    r"^\s*(?:/\*.*?\*/\s*|--[^\n]*\n\s*|\(\s*)*(?:select\b|with\s+(?:recursive\s+)?\w+\s+as\b|insert\s+(?:into|overwrite)\b|"
    r"update\s+\S+[\s\S]*\bset\b|delete\s+from\b|merge\s+into\b|create\s+(?:or\s+replace\s+)?(?:temp\w*\s+|materialized\s+)?"
    r"(?:table|view|schema|database|index|function|procedure|user|role|extension|sequence|trigger)\b|drop\s+\w+|alter\s+\w+|"
    r"truncate\b|grant\b|revoke\b|copy\s+\S+|call\s+\S+\(|vacuum\b|unload\s*\(|explain\s+analy[sz]e\b|reindex\b|"
    r"refresh\s+materialized\b|lock\s+table\b|set\s+\w+\s*(?:=|to\b)|begin\s*;|commit\s*;|rollback\s*;|do\s+\$|"
    r"show\s+\w+|pragma\b|exec(?:ute)?\s+\w+)", re.I | re.S)
READ_WORDS = re.compile(r"^\s*(?:select|with|show|explain|describe|desc)\b", re.I)
BARE_VAR = re.compile(r"\s*\$(?:\{[A-Za-z_]\w*\}|[A-Za-z_]\w*|\d|[@*])\s*;?\s*")
VAR_MSG = "de-agent: SQL passed via a shell variable can't be checked; inline the literal SQL in the command"


def emit(decision: str, reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }))


def guard_mode(project_dir: str) -> str:
    try:
        text = open(os.path.join(project_dir, "de-agent.yaml"), encoding="utf-8").read()
    except OSError:
        return "strict"
    m = re.search(r"^guard\s*:\s*(\{[^}]*\}|(?:\n[ \t]+.*)+)", text, re.M)
    if m:
        mm = re.search(r"mode\s*:\s*[\"']?(strict|ask)", m.group(1))
        if mm:
            return mm.group(1)
    return "strict"


def approved_hashes(project_dir: str) -> set[str]:
    """Hashes from `.approved` files whose HMAC verifies; unsigned or tampered files grant nothing."""
    from de_agent.approval import approved_hashes as verified

    return verified(project_dir)


def _strings(obj, depth=0, out=None):
    """(key, string) for every string value in a nested tool_input (any key, any depth)."""
    out = [] if out is None else out
    if depth > 12 or len(out) > 5000:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                out.append((str(k), v))
            else:
                _strings(v, depth + 1, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            if isinstance(v, str):
                out.append(("", v))
            else:
                _strings(v, depth + 1, out)
    return out


def gather(data: dict, extract_sql) -> list[tuple[str, str]] | None:
    """Return (source, sql) pairs, or None when the call is not a DB call."""
    tool = data.get("tool_name", "")
    ti = data.get("tool_input") or {}
    if tool == "Bash":
        return extract_sql(str(ti.get("command", "")), data.get("cwd"))
    if tool.startswith("mcp__"):
        found = []
        strict_tool = bool(MCP_NAME.search(tool))
        for key, val in _strings(ti):
            if strict_tool and key.lower() in MCP_FIELDS and val:
                found.append((f"mcp:{key}", val))
            elif SQL_START.match(val):
                found.append((f"mcp:{key}" if STRICT_KEY.match(key) else f"mcp-text:{key or 'value'}", val))
        return found
    return None


def main() -> None:
    try:
        raw = sys.stdin.read()
        if len(raw) > MAX_RAW:
            emit("deny", TOO_LARGE.format(what=f"{len(raw)} bytes of hook input"))
            return
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("hook input is not a JSON object")
    except Exception as ex:
        emit("deny", f"de-agent guard error: {ex}")
        return
    try:
        big = input_too_large(data)
        if big:
            emit("deny", big)
            return
        try:
            from de_agent.extract import extract_sql
        except Exception as ex:
            emit("deny", f"de-agent guard error: {ex}")
            return
        pairs = gather(data, extract_sql)
        if not pairs:
            return
        try:
            from de_agent.clients import dialect_for
            from de_agent.sqlclass import classify
        except ImportError:
            emit("deny", NOT_INSTALLED)
            return
        if any(len(sql) > MAX_SQL for _, sql in pairs):
            emit("deny", TOO_LARGE.format(what="one SQL statement is over 100 KB"))
            return
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or "."
        if any(BARE_VAR.fullmatch(sql) for _, sql in pairs):
            emit("deny", VAR_MSG)  # fail closed: the statement is unknowable, even for an approved executor
            return
        writes = []
        for source, sql in pairs:
            dialect = dialect_for(source)
            for st in classify(sql, dialect):
                if st.kind != "write":
                    continue
                if source.startswith("mcp-text:") and st.type == "UNPARSEABLE" and READ_WORDS.match(sql):
                    continue  # prose in a non-SQL field that merely starts with "select ..."
                writes.append(st)
        if not writes:
            return
        if data.get("agent_type") == "de-agent:executor":
            ok = approved_hashes(project_dir)
            if all(w.hash in ok for w in writes):
                return
        w = writes[0]
        emit(
            "ask" if guard_mode(project_dir) == "ask" else "deny",
            f"de-agent: {w.label} is a write; writes need an approved plan (dea approve). "
            f"Statement: {w.text[:120]}",
        )
    except BaseException as ex:  # fail closed, never crash
        emit("deny", f"de-agent guard error: {ex}")


if __name__ == "__main__":
    main()
