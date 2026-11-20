"""Routines (v0.3 section 1): row contract, normalization, redaction, content-addressed body store.

Bodies never go in the snapshot JSON: a row keeps `body_hash`, the text lives once in
`<hub>/.dea/routines/<hash[:2]>/<hash>.sql`. Everything here is offline string handling; no engine access.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable

from de_agent.secretscan import (
    _PLACEHOLDER,
    AWS_KEY_ID,
    AWS_SECRET,
    ENV_ASSIGN,
    PASSWORD_KV,
    URI_RE,
)

KINDS = ("procedure", "function", "view", "materialized_view", "trigger")
ROUTINE_OPTION_KEYS = {"routines", "routine_kinds", "routines_max", "routines_max_bytes"}
DEFAULT_MAX_ROWS = 5000
DEFAULT_MAX_BYTES = 524288
MAX_BYTES_CEILING = 4 * 1024 * 1024
MAX_ROWS_CEILING = 100_000
REDACTED = "<redacted>"
KEY = ["schema", "name", "args"]
REQUIRED_COLUMNS = ("schema", "name", "kind", "args", "language", "returns", "owner", "definition")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_OVERSHOOT = 65536  # chars read past the byte cap before redacting, so a secret straddling the cut is never half-kept

# secrets in quoted form (`password = 'x'`, `"token": "x"`, `password $$x$$`) that the connection-string guard patterns do not see
_SECRET_KEY = r"(?:password|passwd|pwd|secret|secret_key|secret_access_key|token|api_?key|access_key|auth_token|private_key|client_secret)"
_QUOTED_VALUE = r"""(?:E?'(?:[^'\n\\]|''|\\.){1,512}'|"[^"\n]{1,512}"|\$(\w*)\$.{1,512}?\$\2\$)"""
_QUOTED_SECRET = re.compile(
    # with no separator the key must be followed by whitespace (`PASSWORD 'x'`): `'reset-password' THEN 'x'` is not a secret
    rf"(\b{_SECRET_KEY}\b(?:[\"']?\s*(?:=>|:=|=|:)\s*|\s+)){_QUOTED_VALUE}", re.I | re.S)
_CREDENTIALS_CLAUSE = re.compile(
    r"(\b(?:credentials|iam_role|aws_access_key_id|aws_secret_access_key|identified\s+by|encrypted\s+password)\s+)"
    r"(?:'[^'\n]{1,512}'|\"[^\"\n]{1,512}\")", re.I)
_BEARER = re.compile(r"(\bbearer\s+)[A-Za-z0-9._~+/=-]{16,}", re.I)
# well-known token shapes, redacted wherever they appear
_TOKEN_SHAPES = re.compile(
    r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b|\bxox[abprs]-[A-Za-z0-9-]{10,}\b|"
    r"\bsk-[A-Za-z0-9_-]{20,}\b|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b|\bASIA[0-9A-Z]{16}\b")
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)
_GROUP_PATTERNS = (URI_RE, PASSWORD_KV, ENV_ASSIGN, AWS_SECRET)  # secret value is group 1


def validate_routine_options(options: dict[str, Any]) -> None:
    if "routines" in options and not isinstance(options["routines"], bool):
        raise ValueError("options.routines must be true or false")
    if "routine_kinds" in options:
        k = options["routine_kinds"]
        if not isinstance(k, list) or not k or not all(isinstance(x, str) and x in KINDS for x in k):
            raise ValueError(f"options.routine_kinds must be a non-empty list from: {', '.join(KINDS)}")
    for key, ceiling in (("routines_max", MAX_ROWS_CEILING), ("routines_max_bytes", MAX_BYTES_CEILING)):
        if key in options:
            v = options[key]
            if isinstance(v, bool) or not isinstance(v, int) or not 0 < v <= ceiling:
                raise ValueError(f"options.{key} must be an integer between 1 and {ceiling}")


def split_routine_options(options: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(pack params, routine options): packs accept the reserved routine options next to their own params."""
    rt = {k: v for k, v in options.items() if k in ROUTINE_OPTION_KEYS}
    return {k: v for k, v in options.items() if k not in ROUTINE_OPTION_KEYS}, rt


def routine_settings(options: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": bool(options.get("routines", True)),
        "kinds": list(options.get("routine_kinds") or KINDS),
        "max_rows": int(options.get("routines_max", DEFAULT_MAX_ROWS)),
        "max_bytes": int(options.get("routines_max_bytes", DEFAULT_MAX_BYTES)),
    }


def normalize_body(text: str) -> str:
    """CRLF -> LF, strip trailing whitespace per line, drop a leading and a trailing blank block. Comments stay."""
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


_deny_cache: tuple[tuple[str, float], list[str]] | None = None


def _denylist() -> list[str]:
    global _deny_cache
    path = os.environ.get("DEA_DENYLIST_FILE")
    if not path:
        return []
    try:
        stamp = (path, os.stat(path).st_mtime)
        if _deny_cache and _deny_cache[0] == stamp:
            return _deny_cache[1]
        entries = [e.strip() for e in Path(path).read_text(encoding="utf-8").splitlines() if e.strip()]
    except OSError:
        return []
    _deny_cache = (stamp, entries)
    return entries


def redact_body(text: str) -> tuple[str, bool]:
    """Replace secret-looking values and DEA_DENYLIST_FILE entries with `<redacted>`. Returns (text, changed)."""
    out = text
    def _group(m: re.Match[str]) -> str:
        if _PLACEHOLDER.match(m.group(1).strip("'\"")):
            return m.group(0)
        return m.group(0).replace(m.group(1), REDACTED)

    for rx in _GROUP_PATTERNS:
        out = rx.sub(_group, out)
    out = AWS_KEY_ID.sub(REDACTED, out)
    for rx in (_QUOTED_SECRET, _CREDENTIALS_CLAUSE):
        out = rx.sub(lambda m: m.group(1) + REDACTED, out)
    out = _BEARER.sub(lambda m: m.group(1) + REDACTED, out)
    out = _PEM.sub(REDACTED, _TOKEN_SHAPES.sub(REDACTED, out))
    for entry in _denylist():
        out = re.sub(re.escape(entry), REDACTED, out, flags=re.I)
    return out, out != text


def _cut(text: str, max_bytes: int) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    return raw[:max_bytes].decode("utf-8", "ignore"), True


def prepare_body(body: Any, max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    """Raw engine text -> {text, body_hash, body_bytes, truncated, redacted}. Hash covers the redacted, cut, normalized text."""
    raw = "" if body is None else (body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body))
    body_bytes = len(raw.encode("utf-8"))
    head = normalize_body(raw[: max_bytes + _OVERSHOOT])  # bounds regex work; the exact cut below is on bytes
    text, redacted = redact_body(head)
    text, truncated = _cut(text, max_bytes)
    truncated = truncated or body_bytes > max_bytes
    text = normalize_body(text)
    return {"text": text, "body_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "body_bytes": body_bytes, "truncated": truncated, "redacted": redacted}


def safe_component(name: str) -> str:
    """A file-name component from an engine identifier: no separators, no dots-only, bounded."""
    s = re.sub(r"[^A-Za-z0-9_$.-]+", "_", name or "")
    s = re.sub(r"\.{2,}", "_", s).strip(".") or "_"
    return s[:120]


class RoutineStore:
    """Content-addressed body store under <hub>/.dea/routines. Paths derive from a validated sha256 only."""

    def __init__(self, hub: Path) -> None:
        self.root = Path(hub) / ".dea" / "routines"

    def path(self, body_hash: str) -> Path | None:
        if not isinstance(body_hash, str) or not HASH_RE.match(body_hash):
            return None
        return self.root / body_hash[:2] / f"{body_hash}.sql"

    def put(self, body_hash: str, text: str) -> None:
        p = self.path(body_hash)
        if p is None or p.exists():
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        # unique temp name: two sources (threads) can store the same body at the same moment
        tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(p)

    def get(self, body_hash: str) -> str | None:
        p = self.path(body_hash)
        if p is None:
            return None
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return None

    def prune(self, keep: Iterable[str]) -> int:
        """Delete stored bodies no retained snapshot references (and stray temp files). Returns files removed."""
        keep_set = set(keep)
        removed = 0
        if not self.root.is_dir():
            return 0
        for d in sorted(self.root.iterdir()):
            if not d.is_dir():
                continue
            for f in sorted(d.iterdir()):
                if f.is_file() and not f.is_symlink() and (f.suffix == ".tmp" or f.stem not in keep_set):
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
            try:
                d.rmdir()
            except OSError:
                pass
        return removed


def referenced_hashes(results: Iterable[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for r in results:
        for row in ((r or {}).get("data") or {}).get("routines") or []:
            if isinstance(row, dict) and isinstance(row.get("body_hash"), str):
                out.add(row["body_hash"])
    return out


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def finalize_rows(
    raw_rows: list[dict[str, Any]], opts: dict[str, Any], store: RoutineStore | None,
    parser: Callable[[dict[str, Any], str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Engine rows (with a `definition` column) -> contract rows. Bodies go to `store` (if any), never into the row.

    Raises ValueError when the row count exceeds routines_max (no silent cut)."""
    cfg = routine_settings(opts)
    kinds = set(cfg["kinds"])
    rows: list[dict[str, Any]] = []
    for r in raw_rows:
        kind = str(r.get("kind") or "")
        if kind not in kinds:
            continue
        rows.append(r)
    if len(rows) > cfg["max_rows"]:
        raise ValueError(f"{len(rows)} routines exceed routines_max={cfg['max_rows']}; raise routines_max or narrow schemas/routine_kinds")
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for r in rows:
        key = (str(r.get("schema") or ""), str(r.get("name") or ""), str(r.get("args") or "").lower())
        if key in seen:
            continue
        seen.add(key)
        prep = prepare_body(r.get("definition"), cfg["max_bytes"])
        if store is not None and prep["text"]:
            store.put(prep["body_hash"], prep["text"])
        row = {
            "schema": key[0], "name": key[1], "kind": str(r.get("kind")), "args": key[2],
            "language": str(r.get("language") or "").lower(), "returns": str(r.get("returns") or "").lower(),
            "owner": str(r.get("owner") or ""), "body_hash": prep["body_hash"] if prep["text"] else "",
            "body_bytes": max(prep["body_bytes"], _int(r.get("body_bytes"))),
            "truncated": prep["truncated"] or _int(r.get("body_bytes")) > cfg["max_bytes"], "redacted": prep["redacted"],
            "reads": [], "writes": [], "calls": [], "parse": "none",
        }
        if parser is not None and prep["text"]:
            row.update(parser(row, prep["text"]))
        out.append(row)
    out.sort(key=lambda x: (x["schema"], x["name"], x["args"]))
    return out
