#!/usr/bin/env python3
"""PostToolUse hook (Bash): append one JSONL line per DB statement to <project>/.dea/logs/queries.jsonl.

Never blocks, never prints, always exits 0. Python 3.9 compatible (stdlib + vendored sqlglot).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "_vendor"), os.path.join(os.path.dirname(HERE), "cli", "src")]


def exit_status(resp):
    if isinstance(resp, dict):
        for k in ("exit_code", "exitCode", "returncode", "return_code", "status"):
            v = resp.get(k)
            if isinstance(v, int) and not isinstance(v, bool):
                return v
        if resp.get("interrupted"):
            return -1
    return None


def main():
    try:
        data = json.loads(sys.stdin.read())
        if not isinstance(data, dict) or data.get("tool_name") != "Bash":
            return
        ti = data.get("tool_input") or {}
        from de_agent.clients import dialect_for
        from de_agent.extract import extract_sql
        from de_agent.sqlclass import classify

        pairs = extract_sql(str(ti.get("command", "")), data.get("cwd"))
        if not pairs:
            return
        project = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or "."
        status = exit_status(data.get("tool_response"))
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = []
        for source, sql in pairs:
            dialect = dialect_for(source)
            for st in classify(sql, dialect):
                lines.append(json.dumps({
                    "ts": ts, "session_id": data.get("session_id"), "agent_type": data.get("agent_type"),
                    "source": source, "kind": st.kind, "type": st.type, "hash": st.hash,
                    "sql": st.text[:300], "exit_status": status,
                }))
        if lines:
            logdir = os.path.join(project, ".dea", "logs")
            os.makedirs(logdir, exist_ok=True)
            with open(os.path.join(logdir, "queries.jsonl"), "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
    except BaseException:
        return


if __name__ == "__main__":
    main()
    sys.exit(0)
