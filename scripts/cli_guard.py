#!/usr/bin/env python3
"""PreToolUse hook: universal CLI write guard (v0.2 section 3).

Classifies the cloud / data CLIs in a Bash command (aws, gcloud, gsutil, bq, databricks, kubectl, helm, terraform, tofu, pulumi,
az, snow, dbt, airflow, dagster, prefect, kafka-topics, kafka-configs; docker / docker compose / kubectl exec recurse into the
inner command) as read or write. A write is denied: CLI writes run only as an approved plan shell step through `dea apply`
(which runs them itself, not through the Bash tool). A verb of a known CLI that is not on its read list is denied too (fail
closed). SQL-carrying commands (`aws redshift-data`, `bq query`, `snow sql`, ...) are left to sql_guard, but must have readable SQL.
`guard: {mode: ask}` turns a deny into an ask. `guard: {cli_allow: [...]}` lists exact command prefixes a human allows.
Stdlib only, Python 3.9 compatible, size cap from _guardlib. Fails closed on internal errors.
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "_vendor"), os.path.join(os.path.dirname(HERE), "cli", "src")]
sys.path.insert(0, HERE)
from _guardlib import MAX_RAW, TOO_LARGE, guard_block, input_too_large, parse_cli_allow  # noqa: E402


def emit(decision: str, reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }))


def read_config(project_dir: str):
    """(mode, cli_allow) from the hub's de-agent.yaml; a missing or unreadable file means strict and no allow list."""
    try:
        text = open(os.path.join(project_dir, "de-agent.yaml"), encoding="utf-8").read()
    except OSError:
        return "strict", []
    mm = re.search(r"mode\s*:\s*[\"']?(strict|ask)", guard_block(text))
    return (mm.group(1) if mm else "strict"), parse_cli_allow(text)


def check(data: dict):
    """(decision, reason) or None."""
    if data.get("tool_name") != "Bash":
        return None
    command = str((data.get("tool_input") or {}).get("command", ""))
    try:
        from de_agent.cliclass import classify_command, first_denial
        from de_agent.extract import extract_sql
    except Exception as ex:
        return "deny", f"de-agent guard error: {ex}"
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or "."
    mode, allow = read_config(project_dir)
    verdicts = classify_command(command, data.get("cwd"), allow)
    bad = first_denial(verdicts)
    decision = "ask" if mode == "ask" else "deny"
    if bad is not None:
        more = sum(1 for v in verdicts if v.denied) - 1
        return decision, bad.message() + (f" (+{more} more in this command)" if more > 0 else "")
    sqlv = next((v for v in verdicts if v.kind == "sql"), None)
    if sqlv is not None and not extract_sql(command, data.get("cwd")):
        label = f"{sqlv.cli} {sqlv.verb}".strip()
        return decision, (f"de-agent: `{label}` carries SQL that cannot be read here; "
                          "use `dea query` or put it in a plan as a shell step")
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
        res = check(data)
        if res:
            emit(*res)
    except BaseException as ex:  # fail closed, never crash
        emit("deny", f"de-agent guard error: {ex}")


if __name__ == "__main__":
    main()
