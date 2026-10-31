"""Pack parameters and templating. Values are validated, never interpolated blindly.

- cli argv elements, http paths and GraphQL text use `{{name}}`; SQL uses bind parameters `:name` (lists: `in :name`) and
  `{{name}}` only for params of type `ident` (a validated identifier such as a region or dataset name).
- Names come from the source's `options:` (declared in the pack's `params:`), the per-item value of `for_each` (`{{item}}`),
  and built-in time windows `{{since_<N><h|d>_<epoch|epoch_ms|iso>}}` (for example `{{since_24h_epoch}}`).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from de_agent.packs.errors import PackError

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
BIND = re.compile(r"(?<![:\w\\]):([A-Za-z_]\w*)")
BUILTIN = re.compile(r"^since_(\d{1,4})([hd])_(epoch|epoch_ms|iso)$")
SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:/@=+,-]{1,200}$")
IDENT_VALUE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
PARAM_TYPES = ("str", "ident", "int", "bool", "list")


def is_builtin(name: str) -> bool:
    return bool(BUILTIN.match(name))


def builtin_value(name: str, now: datetime) -> str:
    m = BUILTIN.match(name)
    if not m:
        raise PackError(f"unknown template variable {name!r}")
    delta = timedelta(hours=int(m.group(1))) if m.group(2) == "h" else timedelta(days=int(m.group(1)))
    t = (now - delta).astimezone(timezone.utc)
    if m.group(3) == "epoch":
        return str(int(t.timestamp()))
    if m.group(3) == "epoch_ms":
        return str(int(t.timestamp() * 1000))
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _scalar(name: str, typ: str, v: Any) -> Any:
    if typ == "int":
        if isinstance(v, bool) or not isinstance(v, int):
            raise PackError(f"param {name} must be an integer")
        return v
    if typ == "bool":
        if not isinstance(v, bool):
            raise PackError(f"param {name} must be true or false")
        return v
    if isinstance(v, bool) or not isinstance(v, (str, int)):
        raise PackError(f"param {name} must be a plain string")
    s = str(v)
    rx = IDENT_VALUE if typ == "ident" else SAFE_VALUE
    if not rx.match(s) or s.startswith("-"):
        raise PackError(f"param {name}: {s[:40]!r} has characters that are not allowed "
                        f"(letters, digits and _ . : / @ = + , - only; no leading '-')")
    return s


def resolve_params(specs: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    """Declared params with defaults applied and every value validated. Unknown options are an error."""
    unknown = set(options) - set(specs)
    if unknown:
        raise PackError(f"unknown option(s) for this pack: {', '.join(sorted(unknown))}; "
                        f"declared params: {', '.join(sorted(specs)) or 'none'}")
    out: dict[str, Any] = {}
    for name, spec in specs.items():
        if name in options:
            v = options[name]
        elif spec.default is not None:
            v = spec.default
        elif spec.required:
            raise PackError(f"option {name!r} is required by this pack ({spec.description or spec.type})")
        else:
            v = [] if spec.type == "list" else None
        if v is None:
            out[name] = None
        elif spec.type == "list":
            if not isinstance(v, list):
                raise PackError(f"param {name} must be a list")
            out[name] = [_scalar(name, "str", x) for x in v]
        else:
            out[name] = _scalar(name, spec.type, v)
    return out


def used_placeholders(text: str) -> set[str]:
    return {m.group(1) for m in PLACEHOLDER.finditer(text)}


def render(text: str, values: dict[str, Any], item: str | None, now: datetime) -> str:
    """Replace `{{name}}` with validated param values, the for_each item or a built-in time window."""

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name == "item":
            if item is None:
                raise PackError("{{item}} is only available in a section with `for_each`")
            return item
        if is_builtin(name):
            return builtin_value(name, now)
        if name not in values:
            raise PackError(f"unknown template variable {name!r}")
        v = values[name]
        if isinstance(v, list):
            raise PackError(f"{{{{{name}}}}} is a list: iterate it with `for_each: {name}`")
        if v is None:
            raise PackError(f"param {name} has no value")
        return str(v).lower() if isinstance(v, bool) else str(v)

    return PLACEHOLDER.sub(sub, text)
