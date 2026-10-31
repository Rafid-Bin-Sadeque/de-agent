"""`type: sql` source: runs a SQL pack's sections through SQLAlchemy 2.x, read-only.

Enforcement, in order, for every statement: (1) `sqlclass` classifies it in the pack's dialect and anything but one read is a
pack error, BEFORE any connection is made; (2) where the database supports it the statement runs inside a read-only transaction
(`BEGIN READ ONLY` / `START TRANSACTION READ ONLY`, a read-intent connection for SQL Server); (3) a per-statement timeout is set
where the database has one. Local execution only: the driver connects from this machine.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from de_agent import sqlexec  # module, not names: sqlexec -> collectors -> this module is a cycle
from de_agent.collectors.base import CollectorError, RunContext
from de_agent.collectors.pack_base import PackCollector, validate_for_config
from de_agent.config import ConfigError, resolve_secret
from de_agent.packs import template
from de_agent.packs.check import sql_refusal
from de_agent.packs.errors import PackError
from de_agent.packs.model import Pack, Section

# URL scheme -> pack dialect family (a source must match its pack)
SCHEME_DIALECT = {
    "postgresql": "postgres", "postgres": "postgres", "redshift": "redshift", "mysql": "mysql", "mariadb": "mysql",
    "snowflake": "snowflake", "bigquery": "bigquery", "mssql": "tsql", "databricks": "databricks",
}
# pack dialect -> (read-only begin, per-statement timeout statement(s) with {ms}/{s}, end)
FAMILIES: dict[str, dict[str, Any]] = {
    "postgres": {"begin": "BEGIN READ ONLY", "timeout": ["SET LOCAL statement_timeout = {ms}", "SET LOCAL lock_timeout = 3000"], "end": "ROLLBACK"},
    "redshift": {"begin": "BEGIN READ ONLY", "timeout": ["SET LOCAL statement_timeout TO {ms}"], "end": "ROLLBACK"},
    "mysql": {"begin": "START TRANSACTION READ ONLY", "timeout": ["SET SESSION max_execution_time = {ms}"], "end": "ROLLBACK"},
    "snowflake": {"begin": None, "timeout": ["ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {s}"], "end": None},
    "bigquery": {"begin": None, "timeout": [], "end": None},
    "tsql": {"begin": None, "timeout": [], "end": None},
    "databricks": {"begin": None, "timeout": ["SET STATEMENT_TIMEOUT = {s}"], "end": None},
}
MAX_BYTES_BILLED = 10 * 1024**3
DRIVER_HINT = {"postgres": "pg", "redshift": "pg", "mysql": "mysql", "snowflake": "snowflake", "bigquery": "bigquery",
               "tsql": "mssql", "databricks": "databricks"}


def url_scheme(url: str) -> str:
    return url.split("://", 1)[0].split("+", 1)[0].lower()


def install_hint(pack: Pack) -> str:
    extra = pack.requires.extra or DRIVER_HINT.get(pack.dialect or "", "pg")
    return f"pip install 'de-agent[{extra}]'"


def check_driver_url(url: str, pack: Pack) -> None:
    fam = SCHEME_DIALECT.get(url_scheme(url))
    if fam is None:
        raise ValueError(f"unsupported database url scheme {url_scheme(url)!r}; supported: {', '.join(sorted(SCHEME_DIALECT))}")
    if fam != pack.dialect:
        raise ValueError(f"pack {pack.pack} is for {pack.dialect} but the url is {url_scheme(url)}")


def build_url(url: str, password: str | None) -> Any:
    """SQLAlchemy URL object with the resolved password injected (the secret never sits in a string we log)."""
    from sqlalchemy.engine import make_url

    u = make_url(url)
    if password is not None:
        u = u.set(password=password)
    fam = SCHEME_DIALECT.get(url_scheme(url))
    if fam == "tsql" and "ApplicationIntent" not in u.query:
        u = u.update_query_dict({"ApplicationIntent": "ReadOnly"})  # read-intent connection where the server supports it
    return u


def create_read_engine(url_obj: Any, family: str, timeout_s: float, source_options: dict[str, Any]) -> Any:
    from sqlalchemy import create_engine, event
    from sqlalchemy.pool import NullPool

    kwargs: dict[str, Any] = {"poolclass": NullPool}
    if family == "bigquery":
        kwargs["maximum_bytes_billed"] = int(source_options.get("max_bytes_billed", MAX_BYTES_BILLED))
    if family == "postgres":
        kwargs["connect_args"] = {"connect_timeout": 10}
    if family == "mysql":
        kwargs["connect_args"] = {"connect_timeout": 10, "read_timeout": int(max(1, timeout_s)),
                                  "write_timeout": int(max(1, timeout_s))}
    engine = create_engine(url_obj, **kwargs)
    if family == "tsql":
        @event.listens_for(engine, "connect")
        def _set_query_timeout(dbapi_conn: Any, _rec: Any) -> None:  # pyodbc: Connection.timeout is the QUERY timeout
            try:
                dbapi_conn.timeout = int(max(1, timeout_s))
            except Exception:  # not every driver has it
                pass
    return engine


class SqlSession:
    """One connection for all sections of a source; each statement is its own read-only transaction."""

    def __init__(self, source: Any, pack: Pack, timeout_s: float) -> None:
        self.family = pack.dialect or "postgres"
        self.timeout_s = timeout_s
        self.conn: Any = None
        self.engine: Any = None
        self.password: str | None = None
        hint = install_hint(pack)
        try:
            self.password = resolve_secret(source.conn.password)
        except ConfigError as ex:
            raise CollectorError(str(ex)) from None
        try:
            import sqlalchemy  # noqa: F401
        except ImportError:
            raise CollectorError(f"SQLAlchemy is not installed: {hint}") from None
        try:
            self.engine = create_read_engine(build_url(source.conn.url, self.password), self.family, timeout_s, source.options)
            self.conn = self.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        except ImportError as ex:
            raise CollectorError(f"database driver for {url_scheme(source.conn.url)} is not installed ({ex.name or 'import error'}): {hint}") from None
        except Exception as ex:
            if type(ex).__name__ == "NoSuchModuleError":
                raise CollectorError(f"no SQLAlchemy dialect for {url_scheme(source.conn.url)}: {hint}") from None
            raise CollectorError(sqlexec.redact(_first_line(ex), [self.password])) from None
        fam = FAMILIES.get(self.family, FAMILIES["postgres"])
        if fam["begin"] is None:  # session-level timeout once
            self._optional(fam["timeout"])

    def _optional(self, stmts: list[str]) -> None:
        for st in stmts:
            try:
                self.conn.exec_driver_sql(st.format(ms=max(1, int(self.timeout_s * 1000)), s=max(1, int(self.timeout_s))))
            except Exception:  # a driver without the setting still runs the read
                pass

    def query(self, sql: str, params: dict[str, Any], specs: dict[str, Any], cap: int) -> list[dict[str, Any]]:
        from sqlalchemy import bindparam, text

        fam = FAMILIES.get(self.family, FAMILIES["postgres"])
        binds = {n: params.get(n) for n in template.BIND.findall(sql) if n in specs}
        stmt = text(sql)
        expanding = [bindparam(n, expanding=True) for n, v in binds.items() if isinstance(v, list)]
        if expanding:
            stmt = stmt.bindparams(*expanding)
        try:
            if fam["begin"]:
                self.conn.exec_driver_sql(fam["begin"])
                self._optional(fam["timeout"])
            try:
                res = self.conn.execute(stmt, binds)
                got = res.mappings().fetchmany(cap + 1) if res.returns_rows else []
            finally:
                if fam["end"]:
                    try:
                        self.conn.exec_driver_sql(fam["end"])
                    except Exception:
                        pass
        except Exception as ex:
            raise CollectorError(sqlexec.redact(_first_line(ex), [self.password])) from None
        return [{k: sqlexec.normalize_value(v) for k, v in dict(r).items()} for r in got]

    def close(self) -> None:
        for obj in (self.conn, self.engine):
            try:
                if obj is not None:
                    obj.close() if obj is self.conn else obj.dispose()
            except Exception:
                pass


def _first_line(ex: Exception) -> str:
    lines = [ln.strip() for ln in str(ex).strip().splitlines() if ln.strip()]
    return re.sub(r"\s+", " ", (lines[0] if lines else type(ex).__name__))[:300]


class SqlPackCollector(PackCollector):
    type = "sql"

    def validate_source(self, source: Any) -> None:
        pack = validate_for_config(source, self.type)
        if source.exec != "local":
            raise ValueError("sql pack sources run locally (the driver connects from this machine); exec: local")
        if source.conn is None:
            raise ValueError("sql sources need `conn: {url, password}`")
        if pack is not None:
            check_driver_url(source.conn.url, pack)

    def open(self, source: Any, ctx: RunContext, pack: Pack, params: dict[str, Any]) -> Any:
        if ctx.pack_fn is not None:
            return None
        return SqlSession(source, pack, max(1.0, ctx.remaining()))

    def close(self, state: Any) -> None:
        if state is not None:
            state.close()

    def run_section(self, state: Any, source: Any, ctx: RunContext, pack: Pack, sec: Section, item: str | None,
                    params: dict[str, Any], now: datetime) -> list[Any]:
        left = ctx.remaining()
        if left <= 0:
            raise CollectorError("source timeout budget exhausted")
        sql = template.render(sec.sql or "", params, None, now)
        why = sql_refusal(sql, pack.dialect or "postgres", pack.params)  # BEFORE any connection or statement
        if why:
            raise PackError(f"refused, not a single read statement: {why}")
        if ctx.pack_fn is not None:
            return list(ctx.pack_fn(sec, item, sql))
        return state.query(sql, params, pack.params, sec.limit)

    def ping_kind(self, source: Any, ctx: RunContext, pack: Pack) -> str:
        if ctx.pack_fn is not None:
            return f"pack {pack.pack} ok"
        sess = SqlSession(source, pack, max(1.0, ctx.remaining()))
        try:
            rows = sess.query("select 1 as ok", {}, {}, 1)
        finally:
            sess.close()
        return f"pack {pack.pack}, {url_scheme(source.conn.url)} answered ({len(rows)} row)"

