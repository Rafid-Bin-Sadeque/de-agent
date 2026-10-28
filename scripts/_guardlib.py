"""Shared helpers for the guard hooks. Stdlib only, Python 3.9 compatible."""
from __future__ import annotations

MAX_INPUT = 200_000  # bytes of command / tool_input JSON the guards will parse
MAX_SQL = 100_000    # characters of one extracted SQL string
MAX_RAW = 1_000_000  # raw hook stdin; larger is refused before json.loads
TOO_LARGE = "de-agent: input too large to check ({what}); split it into smaller commands (limits: 200 KB input, 100 KB SQL)"


def input_too_large(data) -> str | None:
    """Deny reason when a Bash command or MCP tool_input is over the cap, else None. Runs BEFORE any parsing."""
    import json

    tool = str(data.get("tool_name", ""))
    ti = data.get("tool_input") or {}
    if tool == "Bash":
        n = len(str(ti.get("command", "")).encode("utf-8", "replace"))
    elif tool.startswith("mcp__"):
        n = len(json.dumps(ti).encode("utf-8", "replace"))
    else:
        return None
    return TOO_LARGE.format(what=f"{n} bytes") if n > MAX_INPUT else None
