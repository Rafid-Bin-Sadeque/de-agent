"""sqlglot-based SQL statement classifier (PLAN section 4.3). Fails closed.

READ  = SELECT / WITH-select / VALUES / SHOW / EXPLAIN (without ANALYZE) / safe psql meta-commands.
WRITE = everything else, including anything that cannot be parsed.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.tokens import TokenType

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)


@dataclass
class Stmt:
    kind: str  # "read" | "write"
    type: str
    text: str
    hash: str
    reason: str

    @property
    def label(self) -> str:
        """Human label for denial messages: 'SELECT calling pg_terminate_backend()' for side-effect functions."""
        if self.type == "SELECT" and self.reason.startswith("side-effect function "):
            return "SELECT calling " + self.reason[len("side-effect function "):]
        return self.type


_SEMI = getattr(exp, "Semicolon", None)
_SETOP = getattr(exp, "SetOperation", exp.Union)
_READ_TOPS = (exp.Select, _SETOP, exp.Subquery, exp.Values)

# Node classes that mean a statement changes state wherever they appear (e.g. inside a CTE).
_WRITE_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter,
    exp.TruncateTable, exp.Copy, exp.Grant, exp.Revoke, exp.Analyze, exp.Set, exp.Transaction,
    exp.Commit, exp.Rollback, exp.Comment, exp.Use, exp.Command, exp.Into,
)

_TYPE_NAMES = {
    exp.Insert: "INSERT", exp.Update: "UPDATE", exp.Delete: "DELETE", exp.Merge: "MERGE",
    exp.Create: "CREATE", exp.Drop: "DROP", exp.Alter: "ALTER", exp.TruncateTable: "TRUNCATE",
    exp.Copy: "COPY", exp.Grant: "GRANT", exp.Revoke: "REVOKE", exp.Analyze: "ANALYZE",
    exp.Set: "SET", exp.Transaction: "BEGIN", exp.Commit: "COMMIT", exp.Rollback: "ROLLBACK",
    exp.Comment: "COMMENT", exp.Use: "USE", exp.Into: "SELECT INTO",
}

# Functions with side effects, callable from a plain SELECT.
_DANGEROUS_FUNCS = {
    "pg_terminate_backend", "pg_cancel_backend", "nextval", "setval", "set_config",
    "pg_reload_conf", "pg_rotate_logfile", "lo_import", "lo_export", "lo_unlink", "lo_create",
    "lo_from_bytea", "lo_put", "dblink_exec", "dblink_send_query", "pg_switch_wal",
    "pg_create_restore_point", "pg_create_physical_replication_slot",
    "pg_create_logical_replication_slot", "pg_drop_replication_slot", "pg_advisory_lock",
    "pg_advisory_xact_lock", "pg_try_advisory_lock", "pg_try_advisory_xact_lock",
    "pg_advisory_lock_shared", "pg_stat_reset", "pg_stat_reset_shared", "pg_replication_origin_create",
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_file_write", "pg_backup_start",
    "pg_backup_stop", "pg_promote", "pg_wal_replay_pause", "pg_wal_replay_resume",
}

_META_READ = re.compile(r"^\\(d[A-Za-z+]*|l\+?|x|timing|conninfo|encoding|z|sf\+?|sv\+?|\?|h|q|pset|echo|qecho)(\s|$)")
_META_GOTO = re.compile(r"\\g(exec|set|desc|x|f)?(\s|$)")


def _norm_text(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _mk(kind: str, typ: str, text: str, reason: str, normalized: str | None = None) -> Stmt:
    return Stmt(kind, typ, text, _hash(normalized if normalized is not None else _norm_text(text)), reason)


def normalized_sql(e: exp.Expression, dialect: str) -> str:
    try:
        return e.sql(dialect=dialect, normalize=True)
    except Exception:  # pragma: no cover
        return _norm_text(str(e))


def _func_names(e: exp.Expression):
    for n in e.find_all(exp.Func):
        yield (n.name if isinstance(n, exp.Anonymous) else n.sql_name()).lower()


_FALLBACK_TYPES = (exp.Alias, exp.Column, exp.Identifier, exp.Table, exp.Var, exp.Anonymous, exp.Literal)


def _type_of(e: exp.Expression) -> str:
    for cls, name in _TYPE_NAMES.items():
        if isinstance(e, cls):
            return name
    if isinstance(e, _FALLBACK_TYPES):
        m = re.match(r"[A-Za-z]+", normalized_sql(e, "postgres"))
        if m:
            word = m.group(0).upper()
            return "BEGIN" if word == "START" else word
    return type(e).__name__.upper()


def _command_stmt(e: exp.Command, text: str, norm: str) -> Stmt:
    word = str(e.this).upper()
    rest = e.sql()
    if word == "SHOW":
        return _mk("read", "SHOW", text, "SHOW", norm)
    if word == "EXPLAIN":
        if re.search(r"\banaly[sz]e\b", rest, re.I):
            return _mk("write", "EXPLAIN ANALYZE", text, "EXPLAIN ANALYZE executes the query", norm)
        return _mk("read", "EXPLAIN", text, "EXPLAIN without ANALYZE", norm)
    if word == "SET" and re.match(r"(?i)set\s+(session\s+|local\s+)?(role|session\s+authorization)\b", rest):
        return _mk("write", "SET ROLE", text, "changes session role", norm)
    return _mk("write", word, text, f"{word} is not a read statement", norm)


def _classify_expr(e: exp.Expression, dialect: str) -> Stmt:
    norm = normalized_sql(e, dialect)
    text = norm
    if isinstance(e, exp.Command):
        return _command_stmt(e, text, norm)
    if isinstance(e, exp.Describe):
        return _mk("read", "DESCRIBE", text, "DESCRIBE", norm)
    if isinstance(e, _READ_TOPS):
        for n in e.walk():
            if isinstance(n, exp.Into):
                return _mk("write", "SELECT INTO", text, "SELECT ... INTO creates a table", norm)
            if isinstance(n, exp.Command):
                return _mk("write", str(n.this).upper(), text, "embedded command", norm)
            if isinstance(n, _WRITE_NODES):
                return _mk("write", _type_of(n), text, "data-modifying statement nested in query", norm)
            if isinstance(n, exp.Select) and n.args.get("locks"):
                return _mk("write", "SELECT FOR UPDATE", text, "row-locking select", norm)
        bad = [f for f in _func_names(e) if f in _DANGEROUS_FUNCS]
        if bad:
            return _mk("write", "SELECT", text, f"side-effect function {bad[0]}()", norm)
        top = "SELECT" if not isinstance(e, exp.Values) else "VALUES"
        return _mk("read", top, text, "read-only query", norm)
    return _mk("write", _type_of(e), text, f"{_type_of(e)} is a write", norm)


_SESSION_PARAMS = {
    "statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout", "search_path",
    "timezone", "application_name", "work_mem", "datestyle", "intervalstyle", "extra_float_digits",
    "client_encoding", "client_min_messages", "query_group", "enable_result_cache_for_session",
    "max_parallel_workers_per_gather", "jit",
}
_IDENT = r"[a-z_][a-z0-9_]*"
_SET_RE = re.compile(rf"set\s+(?:(?:session|local)\s+)?(?P<param>{_IDENT})\s*(?:=|\bto\b)\s*(?P<val>.+)")
_SET_TZ_RE = re.compile(r"set\s+(?:(?:session|local)\s+)?time\s+zone\s+(?P<val>.+)")
_SET_RO_RE = re.compile(
    r"set\s+(?:transaction|session\s+characteristics\s+as\s+transaction)\s+read\s+only")
_RESET_RE = re.compile(rf"reset\s+(?P<param>{_IDENT})")
_TXN_MODES = (
    r"(?:\s*,?\s*(?:isolation\s+level\s+(?:read\s+committed|read\s+uncommitted|repeatable\s+read"
    r"|serializable)|read\s+only|read\s+write|(?:not\s+)?deferrable))*"
)
_TXN_RE = re.compile(
    rf"(?P<w>begin|start\s+transaction|commit|end|abort|rollback)(?:\s+(?:work|transaction))?{_TXN_MODES}"
    r"(?:\s+and\s+(?:no\s+)?chain)?"
)
_SAVEPOINT_RE = re.compile(rf"(?P<w>savepoint|release(?:\s+savepoint)?|rollback(?:\s+(?:work|transaction))?"
                           rf"\s+to(?:\s+savepoint)?)\s+{_IDENT}")
_TABLE_RE = re.compile(r'table\s+(?:only\s+)?[\w."]+')


def _param_ok(p: str) -> bool:
    return p in _SESSION_PARAMS or (p.startswith("enable_") and re.fullmatch(r"[a-z0-9_]+", p) is not None)


def _session_stmt(raw: str) -> Stmt | None:
    """Allowlisted session/transaction statements are READ. Returns None when not matched."""
    t = _norm_text(raw).rstrip(";").strip()
    low = t.lower()
    if not low:
        return None
    m = _SET_RE.fullmatch(low)
    if m and _param_ok(m.group("param")) and "(" not in m.group("val"):
        return _mk("read", "SET", t, f"session setting {m.group('param')}")
    m = _SET_TZ_RE.fullmatch(low)
    if m and "(" not in m.group("val"):
        return _mk("read", "SET", t, "session setting timezone")
    if _SET_RO_RE.fullmatch(low):
        return _mk("read", "SET", t, "read-only transaction mode")
    m = _RESET_RE.fullmatch(low)
    if m and _param_ok(m.group("param")):
        return _mk("read", "RESET", t, f"session setting {m.group('param')}")
    m = _TXN_RE.fullmatch(low)
    if m:
        w = m.group("w").split()[0]
        typ = {"start": "BEGIN", "end": "COMMIT", "abort": "ROLLBACK"}.get(w, w.upper())
        return _mk("read", typ, t, "transaction control (changes nothing by itself)")
    m = _SAVEPOINT_RE.fullmatch(low)
    if m:
        w = m.group("w").split()[0]
        return _mk("read", {"rollback": "ROLLBACK"}.get(w, w.upper()), t, "savepoint control")
    if _TABLE_RE.fullmatch(low):
        return _mk("read", "TABLE", t, "TABLE t is SELECT * FROM t")
    return None


def _split_statements(sql: str, dialect: str) -> list[str]:
    """Split on top-level semicolons using the tokenizer; comment-only statements vanish."""
    toks = Dialect.get_or_raise(dialect).tokenize(sql)
    stmts: list[str] = []
    start = end = None
    for tok in toks:
        if tok.token_type == TokenType.SEMICOLON:
            if start is not None:
                stmts.append(sql[start:end + 1])
            start = end = None
            continue
        if start is None:
            start = tok.start
        end = tok.end
    if start is not None:
        stmts.append(sql[start:end + 1])
    return stmts


def _classify_sql(sql: str, dialect: str) -> list[Stmt]:
    if not sql.strip():
        return []
    try:
        pieces = _split_statements(sql, dialect)
    except Exception:
        return [_mk("write", "UNPARSEABLE", sql, "unparseable (fail closed)")]
    out: list[Stmt] = []
    for piece in pieces:
        quick = _session_stmt(piece)
        if quick:
            out.append(quick)
            continue
        try:
            parsed = sqlglot.parse(piece, read=dialect)
            found = False
            for e in parsed:
                if e is None or (_SEMI is not None and isinstance(e, _SEMI)):
                    continue  # empty statement or a comment after ';'
                out.append(_classify_expr(e, dialect))
                found = True
            if not found:
                out.append(_mk("write", "UNPARSEABLE", piece, "unparseable (fail closed)"))
        except Exception as ex:  # fail closed on any internal problem
            out.append(_mk("write", "UNPARSEABLE", piece, f"unparseable (fail closed): {ex}"))
    return out


def _meta_stmt(s: str) -> Stmt:
    if _META_READ.match(s):
        return _mk("read", "META", s, "psql meta-command (read-only)")
    return _mk("write", "META", s, "psql meta-command not on the read allowlist")


def classify(sql: str, dialect: str = "postgres") -> list[Stmt]:
    """Classify every statement in `sql`. Multi-statement input is split."""
    out: list[Stmt] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            out.extend(_classify_sql("\n".join(buf), dialect))
            buf.clear()

    for line in sql.splitlines():
        s = line.strip()
        if s.startswith("\\"):
            flush()
            out.append(_meta_stmt(s))
            continue
        if _META_GOTO.search(line):
            flush()
            out.append(_mk("write", "META", s, "psql \\g-family command executes buffer"))
            continue
        parts = re.split(r";[ \t]*(?=\\)", line)  # "select 1;\\dt" on one line
        if len(parts) > 1:
            for k, part in enumerate(parts):
                piece = part + ";" if k < len(parts) - 1 else part
                if piece.strip().startswith("\\"):
                    flush()
                    out.append(_meta_stmt(piece.strip()))
                else:
                    buf.append(piece)
            continue
        buf.append(line)
    flush()
    return out
