"""Run read-only SQL and return list[dict] (M1a section 3).

Every query is classified (must be all-read) BEFORE it is wrapped, and always runs inside
`BEGIN READ ONLY; SET LOCAL statement_timeout = ...; SET LOCAL lock_timeout = ...; <sql>; COMMIT`. Collectors cannot write,
even by mistake. Backend A: psycopg (local only). Backend B: the `psql` CLI (default for ssh).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from typing import Any

from de_agent.collectors.base import CollectorError
from de_agent.config import ConfigError, ConnConfig, resolve_secret
from de_agent.sqlclass import classify

SSH_BASE = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
PSQL_FLAGS = ["-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1"]

_URI_PW = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^:/\s@]+:)[^@\s]+(@)", re.I)
_KV_PW = re.compile(r"(\bpassword\s*=\s*)[^\s'\"&]+", re.I)


def check_read_only(sql: str, dialect: str = "postgres") -> None:
    """Raise CollectorError unless every statement in `sql` is a read."""
    stmts = classify(sql, dialect)
    if not stmts:
        raise CollectorError("empty SQL")
    for s in stmts:
        if s.kind != "read":
            raise CollectorError(f"collector SQL refused: {s.label} is a write ({s.reason})")


LOCK_TIMEOUT_MS = 3000  # catalog size functions queue behind ACCESS EXCLUSIVE locks; fail fast instead of hanging


def _timeout_ms(timeout_s: float) -> int:
    return max(1, int(timeout_s * 1000))


def wrap_json(sql: str) -> str:
    """Wrap the query so the server returns one JSON array (identical shape for both backends)."""
    inner = sql.strip().rstrip(";").strip()
    # alias is not `q`: a result column named q (e.g. `left(query, 80) q`) would shadow the whole-row reference
    return f"select coalesce(json_agg(__dea_q), '[]'::json) from ({inner}) __dea_q"


def wrap_readonly(sql: str, timeout_s: float) -> str:
    """BEGIN READ ONLY; SET LOCAL statement_timeout; SET LOCAL lock_timeout; <json-wrapped sql>; COMMIT (added after the check)."""
    return (
        "BEGIN READ ONLY;\n"
        f"SET LOCAL statement_timeout = {_timeout_ms(timeout_s)};\n"
        f"SET LOCAL lock_timeout = {LOCK_TIMEOUT_MS};\n"
        f"{wrap_json(sql)};\n"
        "COMMIT;\n"
    )


def _err_line(ex: Exception) -> str:
    """The informative line of a driver error (not the `LINE 1:` echo or its `^` marker)."""
    lines = [ln.strip() for ln in str(ex).strip().splitlines() if ln.strip()]
    useful = [ln for ln in lines if not ln.startswith("LINE ") and set(ln) != {"^"}]
    return (useful or lines or [type(ex).__name__])[0]


def redact(msg: str, secrets: list[str | None] | None = None) -> str:
    for s in secrets or []:
        if s:
            msg = msg.replace(s, "***")
    msg = _URI_PW.sub(r"\1***\2", msg)
    return _KV_PW.sub(r"\1***", msg)


def build_psql(
    conn: ConnConfig, sql: str, exec_target: str, timeout_s: float, password: str | None
) -> tuple[list[str], dict[str, str], str]:
    """Return (argv, env, stdin). The password is only ever in env, never in argv."""
    script = wrap_readonly(sql, timeout_s)
    psql = [
        "psql", *PSQL_FLAGS, "-h", conn.host, "-p", str(conn.port), "-U", conn.user,
        "-d", conn.dbname, "-f", "-",
    ]
    env: dict[str, str] = {}
    if exec_target.startswith("ssh:"):
        host = exec_target[4:]
        remote = " ".join(shlex.quote(a) for a in psql)
        remote = f"PGSSLMODE={shlex.quote(conn.sslmode)} PGCONNECT_TIMEOUT=10 {remote}"
        return [*SSH_BASE, host, remote], env, script
    env["PGSSLMODE"] = conn.sslmode
    env["PGCONNECT_TIMEOUT"] = "10"
    if password is not None:
        env["PGPASSWORD"] = password
    return psql, env, script


def _run_psql(
    conn: ConnConfig, sql: str, exec_target: str, timeout_s: float, password: str | None
) -> list[dict[str, Any]]:
    argv, extra_env, script = build_psql(conn, sql, exec_target, timeout_s, password)
    env = dict(os.environ)
    env.update(extra_env)
    try:
        p = subprocess.run(
            argv, input=script, capture_output=True, text=True, env=env, timeout=timeout_s + 15,
        )
    except FileNotFoundError as ex:
        raise CollectorError(f"{os.path.basename(argv[0])} not found: {ex.filename}") from None
    except subprocess.TimeoutExpired:
        raise CollectorError(f"query timed out after {int(timeout_s)}s") from None
    if p.returncode != 0:
        tail = "\n".join(p.stderr.strip().splitlines()[-3:])
        raise CollectorError(redact(tail or f"psql exited {p.returncode}", [password]))
    return _parse_json(p.stdout)


def _parse_json(stdout: str) -> list[dict[str, Any]]:
    text = stdout.strip()
    if not text:
        raise CollectorError("query returned no output")
    try:  # json_agg emits one element per line, so parse the whole output
        rows = json.loads(text)
    except ValueError:
        raise CollectorError("query output was not JSON") from None
    if not isinstance(rows, list):
        raise CollectorError("query output was not a JSON array")
    return rows


def _run_psycopg(
    psycopg: Any, conn: ConnConfig, sql: str, timeout_s: float, password: str | None
) -> list[dict[str, Any]]:
    kwargs: dict[str, Any] = {
        "host": conn.host, "port": conn.port, "dbname": conn.dbname, "user": conn.user,
        "sslmode": conn.sslmode, "connect_timeout": 10, "autocommit": True,
    }
    if password is not None:
        kwargs["password"] = password
    try:
        with psycopg.connect(**kwargs) as c, c.cursor() as cur:
            cur.execute("BEGIN READ ONLY")
            try:
                cur.execute(f"SET LOCAL statement_timeout = {_timeout_ms(timeout_s)}")
                cur.execute(f"SET LOCAL lock_timeout = {LOCK_TIMEOUT_MS}")
                cur.execute(wrap_json(sql))
                row = cur.fetchone()
                cur.execute("COMMIT")
            except BaseException:
                cur.execute("ROLLBACK")
                raise
    except Exception as ex:
        raise CollectorError(redact(_err_line(ex), [password])) from None
    rows = row[0] if row else []
    if isinstance(rows, str):
        rows = json.loads(rows)
    return rows


def run_query(
    conn: ConnConfig, sql: str, exec_target: str = "local", timeout_s: float = 120
) -> list[dict[str, Any]]:
    check_read_only(sql)
    try:
        password = resolve_secret(conn.password)
    except ConfigError as ex:
        raise CollectorError(str(ex)) from None
    if exec_target == "local":
        try:
            import psycopg
        except ImportError:
            psycopg = None
        if psycopg is not None:
            return _run_psycopg(psycopg, conn, sql, timeout_s, password)
    return _run_psql(conn, sql, exec_target, timeout_s, password)


# ---- writes (dea apply only) and bounded reads (dea query)

TOTAL_COL = "__dea_total"
_TAG_RE = re.compile(r"^(?:INSERT \d+|UPDATE|DELETE|MERGE|COPY|SELECT|MOVE|FETCH) (\d+)$")
WRITE_PSQL_FLAGS = ["-X", "-A", "-t", "-v", "ON_ERROR_STOP=1"]  # no -q: command tags carry row counts


def build_psql_write(conn: ConnConfig, statements: list[str], exec_target: str, timeout_s: float,
                     password: str | None, transaction: bool) -> tuple[list[str], dict[str, str], str]:
    ms = _timeout_ms(timeout_s)
    body = "\n".join(statements)
    if transaction:
        script = f"BEGIN;\nSET LOCAL statement_timeout = {ms};\nSET LOCAL lock_timeout = {ms};\n{body}\nCOMMIT;\n"
    else:
        script = f"SET statement_timeout = {ms};\nSET lock_timeout = {ms};\n{body}\n"
    psql = ["psql", *WRITE_PSQL_FLAGS, "-h", conn.host, "-p", str(conn.port), "-U", conn.user, "-d", conn.dbname,
            "-f", "-"]
    env: dict[str, str] = {}
    if exec_target.startswith("ssh:"):
        remote = " ".join(shlex.quote(a) for a in psql)
        remote = f"PGSSLMODE={shlex.quote(conn.sslmode)} PGCONNECT_TIMEOUT=10 {remote}"
        return [*SSH_BASE, exec_target[4:], remote], env, script
    env["PGSSLMODE"] = conn.sslmode
    env["PGCONNECT_TIMEOUT"] = "10"
    if password is not None:
        env["PGPASSWORD"] = password
    return psql, env, script


def _write_psql(conn: ConnConfig, statements: list[str], exec_target: str, timeout_s: float,
                password: str | None, transaction: bool) -> list[int | None]:
    argv, extra, script = build_psql_write(conn, statements, exec_target, timeout_s, password, transaction)
    env = dict(os.environ)
    env.update(extra)
    try:
        p = subprocess.run(argv, input=script, capture_output=True, text=True, env=env, timeout=timeout_s + 15)
    except FileNotFoundError as ex:
        raise CollectorError(f"{os.path.basename(argv[0])} not found: {ex.filename}") from None
    except subprocess.TimeoutExpired:
        raise CollectorError(f"statement timed out after {int(timeout_s)}s") from None
    if p.returncode != 0:
        tail = "\n".join(p.stderr.strip().splitlines()[-3:])
        raise CollectorError(redact(tail or f"psql exited {p.returncode}", [password]))
    counts = [int(m.group(1)) for ln in p.stdout.splitlines() if (m := _TAG_RE.match(ln.strip()))]
    return counts if len(counts) == len(statements) else [None] * len(statements)


def _write_psycopg(psycopg: Any, conn: ConnConfig, statements: list[str], timeout_s: float,
                   password: str | None, transaction: bool) -> list[int | None]:
    kwargs: dict[str, Any] = {
        "host": conn.host, "port": conn.port, "dbname": conn.dbname, "user": conn.user,
        "sslmode": conn.sslmode, "connect_timeout": 10, "autocommit": True,
    }
    if password is not None:
        kwargs["password"] = password
    ms = _timeout_ms(timeout_s)
    counts: list[int | None] = []
    try:
        with psycopg.connect(**kwargs) as c, c.cursor() as cur:
            if transaction:
                cur.execute("BEGIN")
                cur.execute(f"SET LOCAL statement_timeout = {ms}")
                cur.execute(f"SET LOCAL lock_timeout = {ms}")
            else:
                cur.execute(f"SET statement_timeout = {ms}")
                cur.execute(f"SET lock_timeout = {ms}")
            try:
                for stmt in statements:
                    cur.execute(stmt)
                    counts.append(cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else None)
                if transaction:
                    cur.execute("COMMIT")
            except BaseException:
                if transaction:
                    cur.execute("ROLLBACK")
                raise
    except Exception as ex:
        raise CollectorError(redact(_err_line(ex), [password])) from None
    return counts


def run_write(conn: ConnConfig, statements: list[str], exec_target: str = "local", timeout_s: float = 300,
              transaction: bool = True) -> list[int | None]:
    """Run approved statements (the caller has verified plan approval). One row count per statement (None = unknown)."""
    try:
        password = resolve_secret(conn.password)
    except ConfigError as ex:
        raise CollectorError(str(ex)) from None
    if exec_target == "local":
        try:
            import psycopg
        except ImportError:
            psycopg = None
        if psycopg is not None:
            return _write_psycopg(psycopg, conn, statements, timeout_s, password, transaction)
    return _write_psql(conn, statements, exec_target, timeout_s, password, transaction)


def _run_raw_psycopg(psycopg: Any, conn: ConnConfig, sql: str, timeout_s: float, password: str | None,
                     limit: int) -> tuple[list[dict[str, Any]], int]:
    kwargs: dict[str, Any] = {
        "host": conn.host, "port": conn.port, "dbname": conn.dbname, "user": conn.user,
        "sslmode": conn.sslmode, "connect_timeout": 10, "autocommit": True,
    }
    if password is not None:
        kwargs["password"] = password
    try:
        with psycopg.connect(**kwargs) as c, c.cursor() as cur:
            cur.execute("BEGIN READ ONLY")
            try:
                cur.execute(f"SET LOCAL statement_timeout = {_timeout_ms(timeout_s)}")
                cur.execute(f"SET LOCAL lock_timeout = {LOCK_TIMEOUT_MS}")
                cur.execute(sql)
                cols = [d.name for d in (cur.description or [])]
                got = cur.fetchall() if cols else []
                cur.execute("COMMIT")
            except BaseException:
                cur.execute("ROLLBACK")
                raise
    except Exception as ex:
        raise CollectorError(redact(_err_line(ex), [password])) from None
    rows = [dict(zip(cols, r)) for r in got[:limit]]
    return rows, len(got)


def query_limited(conn: ConnConfig, sql: str, exec_target: str = "local", timeout_s: float = 120,
                  limit: int = 200) -> tuple[list[dict[str, Any]], int]:
    """Run ONE read statement; return (at most `limit` rows, total row count). Raises CollectorError on writes."""
    stmts = classify(sql)
    if len(stmts) != 1:
        raise CollectorError(f"dea query runs exactly one statement (got {len(stmts)})")
    if stmts[0].kind != "read":
        raise CollectorError(f"refused: {stmts[0].label} is a write ({stmts[0].reason})")
    inner = sql.strip().rstrip(";").strip()
    if stmts[0].type in ("SELECT", "VALUES"):
        rows = run_query(conn, f"select _s.*, count(*) over () as {TOTAL_COL} from ({inner}\n) _s limit {int(limit)}",
                         exec_target, timeout_s)
        total = int(rows[0][TOTAL_COL]) if rows else 0
        for r in rows:
            r.pop(TOTAL_COL, None)
        return rows, total
    # EXPLAIN / SHOW / TABLE / session statements cannot sit inside a subquery: read-only transaction via psycopg
    if exec_target != "local":
        raise CollectorError(f"{stmts[0].type} is only supported on local sources (use SELECT, or psql via the guard)")
    try:
        import psycopg
    except ImportError:
        raise CollectorError(f"{stmts[0].type} needs psycopg (pip install 'de-agent[pg]')") from None
    try:
        password = resolve_secret(conn.password)
    except ConfigError as ex:
        raise CollectorError(str(ex)) from None
    return _run_raw_psycopg(psycopg, conn, inner, timeout_s, password, limit)


# ---- Redshift (M3): same read-only discipline, no json_agg / lock_timeout, three transports -> one row shape
#
# Redshift has no json_agg, so rows come back through the cursor (psycopg), `psql --csv` (ssh / no psycopg; parsed with
# declared column types), or Data API records (typed fields). Every path normalizes to the same JSON-clean dicts.
# The query is classified with the redshift dialect and refused unless it is a read BEFORE anything is sent.

RS_DIALECT = "redshift"
RS_TYPES = ("int", "float", "bool", "ts", "str")
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(\.\d+)?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$")


def rs_statements(sql: str, timeout_s: float) -> list[str]:
    """BEGIN READ ONLY -> SET LOCAL statement_timeout -> query -> ROLLBACK (added after the read check)."""
    inner = sql.strip().rstrip(";").strip()
    return ["BEGIN READ ONLY", f"SET LOCAL statement_timeout TO {_timeout_ms(timeout_s)}", inner, "ROLLBACK"]


def _fmt_ts(day: str, clock: str, off: str | None) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M:%S")
    if off and off != "Z":
        sign = -1 if off[0] == "-" else 1
        digits = off[1:].replace(":", "")
        dt -= sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
    return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_value(v: Any) -> Any:
    """One JSON-clean value: integral numbers are ints, timestamps are `YYYY-MM-DDTHH:MM:SSZ` (UTC), the rest as is."""
    from datetime import date, datetime, timezone
    from decimal import Decimal

    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, Decimal, float)):
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            return None
        return int(f) if f == int(f) and abs(f) < 2**53 else round(f, 6)
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc)
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, str):
        m = _TS_RE.match(v)
        return _fmt_ts(m.group(1), m.group(2), m.group(4)) if m else v
    return str(v)


_NUM_RE = re.compile(r"^-?\d+(\.\d+)?([eE][+-]?\d+)?$")


def _convert(text: str | None, typ: str) -> Any:
    if text is None or text == "":
        return None
    if typ == "infer":  # ad-hoc `dea query` results: numbers and timestamps by their text
        if _NUM_RE.match(text):
            return normalize_value(float(text))
        return normalize_value(text)
    try:
        if typ == "int":
            return normalize_value(int(float(text))) if "." in text or "e" in text.lower() else int(text)
        if typ == "float":
            return normalize_value(float(text))
        if typ == "bool":
            return text.strip().lower() in ("t", "true", "1", "y", "yes")
    except ValueError:
        return text
    return normalize_value(text) if typ == "ts" else text


def parse_csv_rows(text: str, types: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """`psql --csv` output (header + rows; NULL = empty field) -> rows typed by the declared column types."""
    import csv
    import io

    types = types or {}
    bad = set(types.values()) - set(RS_TYPES) - {"infer"}
    if bad:
        raise CollectorError(f"unknown column type(s): {', '.join(sorted(bad))}")
    reader = csv.reader(io.StringIO(text.strip("\n")))
    try:
        header = next(reader)
    except StopIteration:
        return []
    default = "infer" if types.get("*") == "infer" else "str"
    return [{h: _convert(v, types.get(h, default)) for h, v in zip(header, row) if h != "*"} for row in reader if row]


_DATA_API_TYPES = {
    "int2": "int", "int4": "int", "int8": "int", "integer": "int", "bigint": "int", "smallint": "int",
    "float4": "float", "float8": "float", "float": "float", "real": "float", "double precision": "float",
    "numeric": "float", "decimal": "float", "bool": "bool", "boolean": "bool",
    "timestamp": "ts", "timestamptz": "ts", "date": "ts",
}


def rows_from_data_api(columns: list[dict[str, Any]], records: list[list[dict[str, Any]]],
                       types: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """GetStatementResult ColumnMetadata + Records -> rows (a declared type wins over the column's SQL type)."""
    types = types or {}
    names = [c.get("label") or c.get("name") for c in columns]
    kinds = [types.get(n) or _DATA_API_TYPES.get(str(c.get("typeName", "")).lower(), "str")
             for n, c in zip(names, columns)]
    out = []
    for rec in records:
        row: dict[str, Any] = {}
        for n, kind, field in zip(names, kinds, rec):
            if field.get("isNull"):
                row[n] = None
                continue
            val = next((field[k] for k in ("longValue", "doubleValue", "booleanValue", "stringValue") if k in field), None)
            row[n] = _convert(val, kind) if isinstance(val, str) else normalize_value(val)
        out.append(row)
    return out


def build_psql_redshift(conn: ConnConfig, sql: str, exec_target: str, timeout_s: float,
                        password: str | None) -> tuple[list[str], dict[str, str], str]:
    script = ";\n".join(rs_statements(sql, timeout_s)) + ";\n"
    psql = ["psql", "-X", "-q", "--csv", "-v", "ON_ERROR_STOP=1", "-h", conn.host, "-p", str(conn.port),
            "-U", conn.user, "-d", conn.dbname, "-f", "-"]
    env: dict[str, str] = {}
    if exec_target.startswith("ssh:"):
        remote = " ".join(shlex.quote(a) for a in psql)
        remote = f"PGSSLMODE={shlex.quote(conn.sslmode)} PGCONNECT_TIMEOUT=10 {remote}"
        return [*SSH_BASE, exec_target[4:], remote], env, script
    env["PGSSLMODE"] = conn.sslmode
    env["PGCONNECT_TIMEOUT"] = "10"
    if password is not None:
        env["PGPASSWORD"] = password
    return psql, env, script


def _rs_psql(conn: ConnConfig, sql: str, exec_target: str, timeout_s: float, password: str | None,
             types: dict[str, str] | None) -> list[dict[str, Any]]:
    argv, extra, script = build_psql_redshift(conn, sql, exec_target, timeout_s, password)
    env = dict(os.environ)
    env.update(extra)
    try:
        p = subprocess.run(argv, input=script, capture_output=True, text=True, env=env, timeout=timeout_s + 15)
    except FileNotFoundError as ex:
        raise CollectorError(f"{os.path.basename(argv[0])} not found: {ex.filename}") from None
    except subprocess.TimeoutExpired:
        raise CollectorError(f"query timed out after {int(timeout_s)}s") from None
    if p.returncode != 0:
        tail = "\n".join(p.stderr.strip().splitlines()[-3:])
        raise CollectorError(redact(tail or f"psql exited {p.returncode}", [password]))
    return parse_csv_rows(p.stdout, types)


def _rs_psycopg(psycopg: Any, conn: ConnConfig, sql: str, timeout_s: float, password: str | None,
                types: dict[str, str] | None) -> list[dict[str, Any]]:
    kwargs: dict[str, Any] = {
        "host": conn.host, "port": conn.port, "dbname": conn.dbname, "user": conn.user,
        "sslmode": conn.sslmode, "connect_timeout": 10, "autocommit": True, "prepare_threshold": None,
    }
    if password is not None:
        kwargs["password"] = password
    begin, set_timeout, query, rollback = rs_statements(sql, timeout_s)
    try:
        with psycopg.connect(**kwargs) as c, c.cursor() as cur:
            cur.execute(begin)
            try:
                cur.execute(set_timeout)
                cur.execute(query)
                cols = [d.name for d in (cur.description or [])]
                got = cur.fetchall() if cols else []
            finally:
                cur.execute(rollback)
    except Exception as ex:
        raise CollectorError(redact(_err_line(ex), [password])) from None
    return [{c: normalize_value(v) for c, v in zip(cols, r)} for r in got]


DATA_API_POLL_S = 0.5
_sleep = time.sleep  # both patched in tests
_clock = time.monotonic


def data_api_client(aws: dict[str, Any] | None) -> Any:
    """boto3 `redshift-data` client behind the read-only guard (raises CollectorError without boto3)."""
    from de_agent.collectors.aws import _aws_core as core

    try:
        session = core.make_session(aws or {}, sql_check=lambda s: check_read_only(s, RS_DIALECT))
    except core.AwsUnavailable as ex:
        raise CollectorError(str(ex)) from None
    return session.client("redshift-data")


def _rs_data_api(source: Any, sql: str, timeout_s: float, types: dict[str, str] | None) -> list[dict[str, Any]]:
    client = data_api_client(source.aws)  # raises the "pip install de-agent[aws]" CollectorError without boto3
    from botocore.exceptions import BotoCoreError, ClientError
    target = {k: v for k, v in (
        ("ClusterIdentifier", source.data_api.get("cluster_id")), ("WorkgroupName", source.data_api.get("workgroup")),
        ("Database", source.data_api.get("database")), ("DbUser", source.data_api.get("db_user")),
        ("SecretArn", _secret_arn(source.data_api.get("secret_arn")))) if v}
    ms = _timeout_ms(timeout_s)
    try:
        # BatchExecuteStatement = one implicit transaction; the session-level SET dies with the Data API session
        sid = client.batch_execute_statement(Sqls=[f"SET statement_timeout TO {ms}", sql.strip().rstrip(";")],
                                             **target)["Id"]
        deadline = _clock() + timeout_s + 5
        while True:
            d = client.describe_statement(Id=sid)
            if d["Status"] == "FINISHED":
                break
            if d["Status"] in ("FAILED", "ABORTED"):
                raise CollectorError(redact(str(d.get("Error") or d["Status"]).strip()[:300]))
            if _clock() > deadline:
                try:
                    client.cancel_statement(Id=sid)
                except (ClientError, BotoCoreError):
                    pass
                raise CollectorError(f"query timed out after {int(timeout_s)}s")
            _sleep(DATA_API_POLL_S)
        subs = d.get("SubStatements") or []
        rid = subs[-1]["Id"] if subs else sid
        cols: list[dict[str, Any]] = []
        records: list[list[dict[str, Any]]] = []
        token = None
        while True:
            page = client.get_statement_result(Id=rid, **({"NextToken": token} if token else {}))
            cols = cols or page.get("ColumnMetadata") or []
            records += page.get("Records") or []
            token = page.get("NextToken")
            if not token:
                break
    except (ClientError, BotoCoreError) as ex:
        raise CollectorError(_aws_error(ex)) from None
    return rows_from_data_api(cols, records, types)


def _secret_arn(ref: str | None) -> str | None:
    if not ref:
        return None
    if ref.startswith("env:"):
        try:
            return resolve_secret(ref)
        except ConfigError as ex:
            raise CollectorError(str(ex)) from None
    return ref


def _aws_error(ex: Exception) -> str:
    from de_agent.collectors.aws import _aws_core as core

    return core.describe_error(ex)


def run_redshift(source: Any, sql: str, types: dict[str, str] | None = None, timeout_s: float = 60) -> list[dict[str, Any]]:
    """Run ONE read query on a redshift source (psycopg / psql --csv / Data API); rows are JSON-clean dicts."""
    check_read_only(sql, RS_DIALECT)
    if len(classify(sql, RS_DIALECT)) != 1:
        raise CollectorError("collector queries are exactly one statement")
    if source.data_api:
        return _rs_data_api(source, sql, timeout_s, types)
    conn = source.conn
    try:
        password = resolve_secret(conn.password)
    except ConfigError as ex:
        raise CollectorError(str(ex)) from None
    if source.exec == "local":
        try:
            import psycopg
        except ImportError:
            psycopg = None
        if psycopg is not None:
            return _rs_psycopg(psycopg, conn, sql, timeout_s, password, types)
    return _rs_psql(conn, sql, source.exec, timeout_s, password, types)


def query_limited_redshift(source: Any, sql: str, timeout_s: float = 60, limit: int = 200) -> tuple[list[dict[str, Any]], int]:
    """`dea query` on a redshift source: ONE read statement, at most `limit` rows, plus the total row count."""
    stmts = classify(sql, RS_DIALECT)
    if len(stmts) != 1:
        raise CollectorError(f"dea query runs exactly one statement (got {len(stmts)})")
    if stmts[0].kind != "read":
        raise CollectorError(f"refused: {stmts[0].label} is a write ({stmts[0].reason})")
    if stmts[0].type not in ("SELECT", "VALUES"):
        raise CollectorError(f"{stmts[0].type} is not supported on redshift sources via dea query (use SELECT)")
    inner = sql.strip().rstrip(";").strip()
    rows = run_redshift(source, f"select _s.*, count(*) over () as {TOTAL_COL} from ({inner}\n) _s limit {int(limit)}",
                        {"*": "infer"}, timeout_s)
    total = int(rows[0][TOTAL_COL]) if rows else 0
    for r in rows:
        r.pop(TOTAL_COL, None)
    return rows, total
