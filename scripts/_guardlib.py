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


def guard_block(text):
    """The body of the top-level `guard:` mapping in de-agent.yaml (flow `{...}` or an indented block), else ''."""
    import re

    m = re.search(r"^guard\s*:\s*(\{[^}]*\}|(?:\n[ \t]+.*)+)", text or "", re.M)
    return m.group(1) if m else ""


def _unquote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        inner = s[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\") if s[0] == '"' else inner.replace("''", "'")
    return s


def _split_flow(body):
    """Items of a flow sequence body `a, "b, c", 'd'` (commas inside quotes are kept)."""
    items, cur, q = [], [], None
    for ch in body:
        if q:
            cur.append(ch)
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
            cur.append(ch)
        elif ch == ",":
            items.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        items.append("".join(cur))
    return [_unquote(i) for i in items if i.strip()]


def parse_cli_allow(text):
    """`guard.cli_allow` from de-agent.yaml text, stdlib only: flow `[a, "b"]`, a block list, or inside `guard: {...}`.
    Used by cli_guard (to allow) and self_guard (to detect edits), so both read the same list."""
    import re

    block = "\n".join(ln for ln in guard_block(text).split("\n") if not ln.lstrip().startswith("#"))
    if not block:
        return []
    m = re.search(r"cli_allow[ \t]*:[ \t]*(\[.*?\]|[^\n]*)", block, re.S)
    if not m:
        return []
    rest = m.group(1).strip()
    if rest.startswith("["):
        return _split_flow(rest[1:rest.index("]")] if "]" in rest else rest[1:])
    out = []
    lines = block[m.end():].split("\n") if not rest or rest.startswith("#") else []
    key_line = block[:m.start()].rsplit("\n", 1)[-1]
    key_indent = len(key_line) - len(key_line.lstrip())
    for ln in lines:
        if not ln.strip():
            continue
        indent = len(ln) - len(ln.lstrip())
        lm = re.match(r"^\s*-\s+(.*?)\s*$", ln)
        if indent <= key_indent or not lm:
            break
        item = lm.group(1)
        if item[:1] in "'\"":
            out.append(_unquote(item))
        else:
            out.append(item.split(" #")[0].strip())
    return out
