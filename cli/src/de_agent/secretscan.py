"""Patterns for secrets that must never sit in a hub file (mirrors scripts/secret_guard.py)."""
from __future__ import annotations

import re

_PLACEHOLDER = re.compile(r"^(\$\{?\w+\}?|<[^>]*>|\{\{.*\}\}|\*+|x{3,}|%s|env:\w+|pgpass|none)$", re.I)

# a value that is a code reference, not a literal: IDENT[...], IDENT(...), IDENT.IDENT[...], os.environ..., os.getenv(...)
_CODE_REF = re.compile(r"^[A-Za-z_][\w.]*\s*[\[(]")

URI_RE = re.compile(r"\b(?:postgres(?:ql)?|redshift|mysql|mariadb|mongodb(?:\+srv)?)://[^:/\s@]+:([^@\s]+)@", re.I)
PASSWORD_KV = re.compile(r"\bpassword=([^\s'\"&]+)", re.I)
ENV_ASSIGN = re.compile(r"\b(?:PGPASSWORD|MYSQL_PWD)=(\S+)")
AWS_SECRET = re.compile(r"\bAWS_SECRET_ACCESS_KEY\s*[=:]\s*[\"']?([A-Za-z0-9/+=]{20,})")
AWS_KEY_ID = re.compile(r"\bAKIA[0-9A-Z]{16}\b")


def find_secret(text: str) -> str | None:
    """Return a short description of the first secret-looking thing in `text`, else None."""
    for label, rx in (
        ("password inside a connection URI", URI_RE),
        ("inline password=", PASSWORD_KV),
        ("inline PGPASSWORD/MYSQL_PWD", ENV_ASSIGN),
        ("AWS secret access key", AWS_SECRET),
    ):
        for m in rx.finditer(text):
            val = m.group(1).strip("'\"")
            if _PLACEHOLDER.match(val) or (rx is PASSWORD_KV and _CODE_REF.match(val)):
                continue
            return label
    if AWS_KEY_ID.search(text):
        return "AWS access key id"
    return None
