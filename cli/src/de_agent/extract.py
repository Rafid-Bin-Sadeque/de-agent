"""Extract SQL from shell commands that talk to a database (PLAN section 4.3).

Returns (source, sql) pairs. Anything we know a DB client is running but cannot read
becomes sql "<unreadable: ...>", which the classifier treats as a write (fail closed).
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from de_agent import pyscan
from de_agent.clients import SQL_CLIENTS

_NAMES = sorted(set(SQL_CLIENTS) | {"bq", "aws", "dea", "clickhouse"}, key=len, reverse=True)
_PY_RE = r"python[0-9.]*"
_CLIENT_RE = re.compile(r"(?<![\w-])(" + "|".join(re.escape(n) for n in _NAMES) + "|" + _PY_RE + r")(?![\w-])")
_PY_NAME = re.compile(r"^python[0-9.]*$")
_OPERATORS = set(";&|()")
_SHELLS = {"bash", "sh", "zsh", "dash", "ssh"}
# Text tools that never execute their arguments. xargs, find -exec, awk, sed, env, sudo ... are execution wrappers and
# are NOT here: a client anywhere after them is parsed as executed.
_HARMLESS = {
    "grep", "egrep", "fgrep", "rg", "echo", "printf", "cat", "which", "type", "man",
    "head", "tail", "less", "more", "wc", "sort", "uniq", "ls", "tee", "cut", "tr",
}
# Package managers and file tools: a client name after them is an argument, not a program being run.
_NON_EXEC = {
    "pip", "pip3", "pipx", "uv", "brew", "apt", "apt-get", "yum", "dnf", "apk", "npm", "yarn", "pnpm", "conda",
    "mamba", "poetry", "pdm", "hatch", "rye", "cargo", "gem", "git", "whereis", "mkdir", "rm", "cp", "mv", "touch", "chmod", "chown",
    "ln", "stat", "file", "du", "df", "pgrep", "pkill", "kill", "killall", "systemctl", "service", "docker-compose",
}
_NOARG_SHORT = set("AtXqxaebEHnsS1zZ0")
_MAX_FILE = 1_000_000
_PLACEHOLDER = "__DEA_HEREDOC_{}__"
_HEREDOC_RE = re.compile(r"<<(-?)[ \t]*(?:'([^']+)'|\"([^\"]+)\"|\\?(\w+))")


def _unreadable(why: str) -> str:
    return f"<unreadable: {why}>"


def _preprocess(s: str) -> tuple[str, list[str]]:
    """Quote-aware pass: join line continuations, turn newlines into ';', pull out heredoc bodies."""
    out: list[str] = []
    bodies: list[str] = []
    pending: list[tuple[str, bool]] = []
    q: str | None = None
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if q:
            out.append(c)
            if c == "\\" and q == '"' and i + 1 < n:
                out.append(s[i + 1])
                i += 2
                continue
            if c == q:
                q = None
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            if s[i + 1] == "\n":
                out.append(" ")
            else:
                out.append(c + s[i + 1])
            i += 2
            continue
        if c in "'\"":
            q = c
            out.append(c)
            i += 1
            continue
        if s.startswith("<<<", i):
            out.append("<<<")
            i += 3
            continue
        if s.startswith("<<", i):
            m = _HEREDOC_RE.match(s, i)
            if m:
                delim = m.group(2) or m.group(3) or m.group(4)
                pending.append((delim, bool(m.group(1))))
                out.append(f" << {_PLACEHOLDER.format(len(bodies) + len(pending) - 1)} ")
                i = m.end()
                continue
        if c == "\n":
            out.append(" ; ")
            i += 1
            for delim, strip in pending:
                lines: list[str] = []
                while i < n:
                    j = s.find("\n", i)
                    j = n if j == -1 else j
                    line = s[i:j]
                    i = min(j + 1, n)
                    if (line.lstrip("\t") if strip else line) == delim:
                        break
                    lines.append(line)
                bodies.append("\n".join(lines))
            pending = []
            continue
        out.append(c)
        i += 1
    for _ in pending:  # unterminated heredoc: body is empty
        bodies.append("")
    return "".join(out), bodies


def _tokenize(cmd: str) -> list[str]:
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def _split_simple(tokens: list[str]) -> list[tuple[bool, list[str]]]:
    cmds: list[tuple[bool, list[str]]] = []
    cur: list[str] = []
    piped = False
    next_piped = False
    for t in tokens:
        if t and all(ch in _OPERATORS for ch in t):
            if cur:
                cmds.append((piped, cur))
            cur = []
            piped = next_piped = t in ("|", "|&")
            if not next_piped:
                piped = False
            continue
        cur.append(t)
    if cur:
        cmds.append((piped, cur))
    return cmds


def _base(w: str) -> str:
    return os.path.basename(w)


def _read_file(path: str, cwd: str | None) -> str:
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        p = Path(cwd or os.getcwd()) / p
    try:
        if p.stat().st_size > _MAX_FILE:
            return _unreadable(f"file too large {path}")
        return p.read_text(errors="replace")
    except OSError:
        return _unreadable(f"file {path}")


_BARE_BRACED = re.compile(r"\s*\$\{[A-Za-z_]\w*\}\s*;?\s*")


def _has_subst(s: str) -> bool:
    if _BARE_BRACED.fullmatch(s):
        return False  # a bare shell variable: left as-is so the guard can say so (still denied, fail closed)
    return "$(" in s or "`" in s or "${" in s


def _psql(args: list[str], piped: bool, bodies: list[str], cwd: str | None) -> list[tuple[str, str]]:
    res: list[tuple[str, str]] = []
    stdin_src = False
    i = 0
    while i < len(args):
        a = args[i]
        val: str | None = None
        kind = None
        if a in ("-c", "--command"):
            kind, val, i = "c", (args[i + 1] if i + 1 < len(args) else None), i + 1
        elif a.startswith("--command="):
            kind, val = "c", a.split("=", 1)[1]
        elif a in ("-f", "--file"):
            kind, val, i = "f", (args[i + 1] if i + 1 < len(args) else None), i + 1
        elif a.startswith("--file="):
            kind, val = "f", a.split("=", 1)[1]
        elif re.fullmatch(r"-[A-Za-z0-9]*[cf]", a) and set(a[1:-1]) <= _NOARG_SHORT:
            kind, val, i = a[-1], (args[i + 1] if i + 1 < len(args) else None), i + 1
        elif a.startswith("-c") and len(a) > 2 and not a.startswith("--"):
            kind, val = "c", a[2:]
        elif a.startswith("-f") and len(a) > 2 and not a.startswith("--"):
            kind, val = "f", a[2:]
        elif a == "<<<" and i + 1 < len(args):
            res.append(("psql-herestring", args[i + 1]))
            stdin_src = True
            i += 1
        elif a == "<<" and i + 1 < len(args) and re.fullmatch(r"__DEA_HEREDOC_(\d+)__", args[i + 1]):
            k = int(re.fullmatch(r"__DEA_HEREDOC_(\d+)__", args[i + 1]).group(1))
            res.append(("psql-heredoc", bodies[k] if k < len(bodies) else ""))
            stdin_src = True
            i += 1
        elif a == "<" and i + 1 < len(args):
            res.append(("psql-stdin", _read_file(args[i + 1], cwd)))
            stdin_src = True
            i += 1
        if kind == "c":
            if val is None:
                res.append(("psql", _unreadable("missing -c argument")))
            else:
                res.append(("psql", _unreadable("shell substitution") if _has_subst(val) else val))
        elif kind == "f":
            if val is None or val == "-":
                pass  # stdin; covered by heredoc/pipe handling
            else:
                res.append(("psql-file", _unreadable("shell substitution") if _has_subst(val)
                            else _read_file(val, cwd)))
        i += 1
    if not res and piped and not stdin_src:
        res.append(("psql-stdin", _unreadable("SQL piped on stdin")))
    return res


def _flag_values(args: list[str], flag: str) -> list[str]:
    vals: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == flag and i + 1 < len(args):
            vals.append(args[i + 1])
            i += 1
        elif a.startswith(flag + "="):
            vals.append(a.split("=", 1)[1])
        i += 1
    return vals


def _multi_flag_values(args: list[str], flag: str) -> list[str]:
    vals: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == flag:
            j = i + 1
            while j < len(args) and not args[j].startswith("--"):
                vals.append(args[j])
                j += 1
            i = j
            continue
        if args[i].startswith(flag + "="):
            vals.append(args[i].split("=", 1)[1])
        i += 1
    return vals


def _aws(words: list[str]) -> list[tuple[str, str]]:
    res: list[tuple[str, str]] = []
    if "redshift-data" in words:
        k = words.index("redshift-data")
        sub = words[k + 1] if k + 1 < len(words) else ""
        if sub == "execute-statement":
            vals = _flag_values(words[k + 2:], "--sql")
            res += [("redshift-data", v) for v in vals] or [("redshift-data", _unreadable("no --sql"))]
        elif sub == "batch-execute-statement":
            vals = _multi_flag_values(words[k + 2:], "--sqls")
            res += [("redshift-data", v) for v in vals] or [("redshift-data", _unreadable("no --sqls"))]
    elif "athena" in words:
        k = words.index("athena")
        if k + 1 < len(words) and words[k + 1] == "start-query-execution":
            vals = _flag_values(words[k + 2:], "--query-string")
            res += [("athena", v) for v in vals] or [("athena", _unreadable("no --query-string"))]
    return res


def _bq(words: list[str]) -> list[tuple[str, str]]:
    if "query" not in words:
        return []
    k = words.index("query")
    rest = words[k + 1:]
    pos = [w for w in rest if not w.startswith("-")]
    if not pos:
        return [("bq", _unreadable("no query argument"))]
    return [("bq", pos[-1])]


_REDIRECT_OPS = {">", ">>", ">&", "&>", "&>>", "<", "<&", ">|", "<>"}


def _strip_redirects(args: list[str]) -> list[str]:
    """Drop shell redirections (`2>&1`, `>&2`, `> file`, `2>/dev/null`) and their operands: they are not arguments."""
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in _REDIRECT_OPS:
            i += 2
            continue
        if a.isdigit() and i + 1 < len(args) and args[i + 1] in _REDIRECT_OPS:
            i += 1  # the fd number of `2>&1`; the operator and its operand go on the next turn
            continue
        if re.match(r"^\d*(?:>>?|<)[^&\s].*$", a) or re.match(r"^\d*[<>]&\d*-?$", a):  # fused: 2>/dev/null, 2>&1
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def _gt(args: list[str]) -> list[tuple[str, str]]:
    """`dea query [--format X] [--limit N] <source> <sql>`: the CLI's own read path, logged and guarded like psql.

    Every other `dea` subcommand carries no SQL and is never classified; shell redirections are not arguments."""
    args = _strip_redirects(args)
    if not args or args[0] != "query":
        return []
    if any(a in ("--help", "-h") for a in args[1:]):
        return []  # help text, no statement
    pos: list[str] = []
    i = 1
    while i < len(args):
        a = args[i]
        if a in ("--format", "--limit"):
            i += 2
            continue
        if a.startswith("--"):
            i += 1
            continue
        pos.append(a)
        i += 1
    if len(pos) < 2:
        return [("dea", _unreadable("dea query needs <source> <sql>"))]
    sql = _unreadable("shell substitution") if _has_subst(pos[1]) else pos[1]
    return [(f"dea:{pos[0]}", sql)]


# ---- other SQL clients (mysql, snowsql, sqlcmd, clickhouse, sqlite3, duckdb, trino/presto, spark-sql, beeline)
# inline: flags whose value is SQL; files: flags whose value is a SQL file; cluster: letters that may precede a fused
# short flag (`-Ne "..."`); positional: SQL may follow the database argument (sqlite3, duckdb); valued: other flags that
# take a value (skipped when looking for positionals).
_SPECS: dict[str, dict] = {
    "mysql": {"inline": ("-e", "--execute"), "files": (), "cluster": "BNsrtvEgGinqwWZ"},
    "snowsql": {"inline": ("-q", "--query"), "files": ("-f", "--filename")},
    "sqlcmd": {"inline": ("-Q", "-q", "--query", "--initial-query"), "files": ("-i", "--input-file")},
    "clickhouse-client": {"inline": ("-q", "--query"), "files": ("--queries-file",)},
    "sqlite3": {"inline": ("-cmd",), "files": ("-init",), "positional": True,
                "valued": ("-separator", "-newline", "-nullvalue", "-vfs", "-mmap", "-maxsize", "-pagecache",
                           "-lookaside", "-heap", "-memtrace")},
    "duckdb": {"inline": ("-c", "-cmd", "-s"), "files": ("-init", "-f"), "positional": True,
               "valued": ("-separator", "-nullvalue", "-newline", "-storage-version")},
    "trino": {"inline": ("--execute",), "files": ("--file", "-f")},
    "presto": {"inline": ("--execute",), "files": ("--file", "-f")},
    "spark-sql": {"inline": ("-e",), "files": ("-f", "-i")},
    "beeline": {"inline": ("-e",), "files": ("-f",)},
}
_SPEC_ALIAS = {"mariadb": "mysql", "clickhouse-local": "clickhouse-client", "trino-cli": "trino", "presto-cli": "presto"}
# sqlite3 / duckdb dot-commands that read or print only
_DOT_SAFE = {".tables", ".schema", ".headers", ".header", ".mode", ".indexes", ".indices", ".databases", ".dump",
             ".help", ".timer", ".show", ".width", ".separator", ".print", ".quit", ".exit", ".nullvalue", ".bail",
             ".echo", ".changes", ".fullschema", ".stats", ".eqp", ".explain", ".columns", ".maxrows", ".maxwidth"}


def _dot_filter(sql: str, cwd: str | None, depth: int = 0) -> str:
    """sqlite3/duckdb dot-commands: `.read FILE` is inlined, read-only ones dropped, anything else is unreadable."""
    out: list[str] = []
    for line in sql.splitlines():
        t = line.strip()
        if not t.startswith("."):
            out.append(line)
            continue
        parts = t.split(None, 1)
        word = parts[0].lower()
        if word == ".read" and len(parts) > 1 and depth < 2 and not parts[1].startswith("|"):
            out.append(_dot_filter(_read_file(parts[1].strip("'\""), cwd), cwd, depth + 1))
        elif word not in _DOT_SAFE:
            out.append(_unreadable(f"dot-command {word}"))
    return "\n".join(out)


def _generic(client: str, args: list[str], piped: bool, bodies: list[str], cwd: str | None) -> list[tuple[str, str]]:
    spec = _SPECS[_SPEC_ALIAS.get(client, client)]
    inline, files = spec["inline"], spec["files"]
    cluster = spec.get("cluster", "")
    dots = client in ("sqlite3", "duckdb")
    res: list[tuple[str, str]] = []
    pos: list[str] = []
    stdin_src = False

    def add(kind: str, text: str) -> None:
        if _has_subst(text):
            text = _unreadable("shell substitution")
        elif dots:
            text = _dot_filter(text, cwd)
        res.append((client + kind, text))

    i = 0
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None
        if a in inline or a in files:
            if nxt is None:
                res.append((client, _unreadable(f"missing {a} argument")))
            elif a in inline:
                add("", nxt)
            elif nxt != "-":
                add("-file", _unreadable("shell substitution") if _has_subst(nxt) else _read_file(nxt, cwd))
            i += 2
            continue
        m = re.match(r"^(--[\w-]+)=(.*)$", a, re.S)
        if m and (m.group(1) in inline or m.group(1) in files):
            if m.group(1) in inline:
                add("", m.group(2))
            else:
                add("-file", _read_file(m.group(2), cwd))
            i += 1
            continue
        fused = [f for f in (*inline, *files) if len(f) == 2 and a.startswith(f) and len(a) > 2 and not a.startswith("--")]
        clus = re.fullmatch(r"-([A-Za-z]+)([eqQ])", a) if cluster else None
        if fused:
            f = fused[0]
            if f in inline:
                add("", a[2:])
            else:
                add("-file", _read_file(a[2:], cwd))
            i += 1
            continue
        if clus and set(clus.group(1)) <= set(cluster) and f"-{clus.group(2)}" in inline and nxt is not None:
            add("", nxt)
            i += 2
            continue
        if a == "<<<" and nxt is not None:
            add("-herestring", nxt)
            stdin_src = True
            i += 2
            continue
        if a == "<<" and nxt is not None and re.fullmatch(r"__DEA_HEREDOC_(\d+)__", nxt):
            k = int(re.fullmatch(r"__DEA_HEREDOC_(\d+)__", nxt).group(1))
            add("-heredoc", bodies[k] if k < len(bodies) else "")
            stdin_src = True
            i += 2
            continue
        if a == "<" and nxt is not None:
            add("-stdin", _read_file(nxt, cwd))
            stdin_src = True
            i += 2
            continue
        if spec.get("positional"):
            if a in spec.get("valued", ()):
                i += 2
                continue
            if not a.startswith("-"):
                pos.append(a)
        i += 1
    if spec.get("positional") and len(pos) > 1:
        for p in pos[1:]:
            add("", p)
    if not res and piped and not stdin_src:
        res.append((client + "-stdin", _unreadable("SQL piped on stdin")))
    return res


# ---- python code (heuristic; see pyscan)
_PY_VALUE_OPTS = {"-W", "-X", "-m", "-Q", "--check-hash-based-pycs"}


def _python_code(args: list[str], piped: bool, bodies: list[str], cwd: str | None) -> tuple[list[str], str | None]:
    """The Python source one `python ...` invocation will run, and a reason when it cannot be read (piped code)."""
    code: list[str] = []
    stdin_src = False
    script = None
    i = 0
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None
        if a == "-c" and nxt is not None:
            code.append(nxt)
            break
        if a.startswith("-c") and len(a) > 2 and not a.startswith("--"):
            code.append(a[2:])
            break
        if a == "-m":
            break  # a module: not scanned
        if a == "-" :
            stdin_src = True
            i += 1
            continue
        if a == "<<<" and nxt is not None:
            code.append(nxt)
            stdin_src = True
            i += 2
            continue
        if a == "<<" and nxt is not None and re.fullmatch(r"__DEA_HEREDOC_(\d+)__", nxt):
            k = int(re.fullmatch(r"__DEA_HEREDOC_(\d+)__", nxt).group(1))
            code.append(bodies[k] if k < len(bodies) else "")
            stdin_src = True
            i += 2
            continue
        if a == "<" and nxt is not None:
            code.append(_read_file(nxt, cwd))
            stdin_src = True
            i += 2
            continue
        if a in _PY_VALUE_OPTS:
            i += 2
            continue
        if a.startswith("-") or a in _REDIRECT_OPS:
            i += 1
            continue
        if script is None and not stdin_src:
            script = a
        i += 1
    if script is not None:
        sp = os.path.expanduser(script)
        sp = sp if os.path.isabs(sp) else os.path.join(cwd or os.getcwd(), sp)
        if os.path.isfile(sp):  # a missing file: python itself fails, nothing runs
            code.append(_read_file(script, cwd))
    if not code and piped and not stdin_src and not script and "-m" not in args:
        return code, "python code piped on stdin"
    if not code and stdin_src and piped:
        return code, "python code piped on stdin"
    return code, None


def _python(args: list[str], piped: bool, bodies: list[str], cwd: str | None, depth: int) -> list[tuple[str, str]]:
    nested = lambda text: extract_sql(text, cwd, depth + 1)  # noqa: E731
    res: list[tuple[str, str]] = []
    code, why = _python_code(args, piped, bodies, cwd)
    if why:
        return [("python", _unreadable(why))]
    for c in code:
        if c.startswith("<unreadable"):
            res.append(("python", c))
            continue
        if len(c) > _MAX_FILE:
            res.append(("python", _unreadable("python code too large")))
            continue
        if pyscan.has_driver(c) and _has_subst(c):
            res.append(("python", _unreadable("shell substitution in python code")))
            continue
        res += pyscan.analyze(c, nested)
    return res


_REDIRECT_OPS = {">", ">>", ">&", "&>", "&>>", "<&", ">|", "<>"}


def _runs(first: str, names: list[str]) -> bool:
    """False when the command is a package/file tool whose arguments merely mention a client (`pip install duckdb`)."""
    return first not in _NON_EXEC or ("run" in names[:4] or "exec" in names[:4])


def _handle(words: list[str], piped: bool, bodies: list[str], cwd: str | None, depth: int) -> list[tuple[str, str]]:
    names = [_base(w) for w in words]
    first = next((n for w, n in zip(words, names) if "=" not in w or w.startswith("-")), "")
    out: list[tuple[str, str]] = []
    matched = False
    if first not in _HARMLESS:
        for k, n in enumerate(names):
            if n == "psql" and _runs(first, names):
                out += _psql(words[k + 1:], piped, bodies, cwd)
                matched = True
                break
        for k, n in enumerate(names):
            c = _SPEC_ALIAS.get(n, n)
            if c in _SPECS and _runs(first, names):
                out += _generic(n, words[k + 1:], piped, bodies, cwd)
                matched = True
            elif n == "clickhouse" and k + 1 < len(names) and names[k + 1] in ("client", "local") and _runs(first, names):
                out += _generic("clickhouse-client", words[k + 2:], piped, bodies, cwd)
                matched = True
            elif _PY_NAME.match(n) and _runs(first, names):
                py = _python(words[k + 1:], piped, bodies, cwd, depth)
                matched = matched or bool(py)
                out += py
        if "bq" in names:
            out += _bq(words[names.index("bq") + 1:])
            matched = True
        elif "aws" in names:
            out += _aws(words[names.index("aws") + 1:])
            matched = matched or bool(out)
        if "dea" in names and names.index("dea") <= 3:
            out += _gt(words[names.index("dea") + 1:])
            matched = True
    if not matched and depth < 3:
        for w in words:
            if " " in w and _CLIENT_RE.search(w):
                out += extract_sql(w, cwd, depth + 1)
        if any(n in _SHELLS for n in names):
            for w in words:
                m = re.fullmatch(r"__DEA_HEREDOC_(\d+)__", w)
                if m and int(m.group(1)) < len(bodies):
                    out += extract_sql(bodies[int(m.group(1))], cwd, depth + 1)
    return out


def extract_sql(command: str, cwd: str | None = None, _depth: int = 0) -> list[tuple[str, str]]:
    """Return (source, sql) for every database call found in a shell command line."""
    if not _CLIENT_RE.search(command):
        return []
    try:
        pre, bodies = _preprocess(command)
        tokens = _tokenize(pre)
    except ValueError:
        return [("shell", _unreadable("shell parse error"))]
    out: list[tuple[str, str]] = []
    for piped, words in _split_simple(tokens):
        out += _handle(words, piped, bodies, cwd, _depth)
    seen: set[tuple[str, str]] = set()
    uniq = []
    for pair in out:
        if pair not in seen:
            seen.add(pair)
            uniq.append(pair)
    return uniq


def python_codes(command: str, cwd: str | None = None) -> list[str]:
    """Python source run by every `python ...` call in a shell command (for secret_guard). Unreadable code is skipped."""
    if not re.search(r"python", command):
        return []
    try:
        pre, bodies = _preprocess(command)
        tokens = _tokenize(pre)
    except ValueError:
        return []
    out: list[str] = []
    for piped, words in _split_simple(tokens):
        for k, w in enumerate(words):
            if _PY_NAME.match(_base(w)):
                code, _ = _python_code(words[k + 1:], piped, bodies, cwd)
                out += [c for c in code if not c.startswith("<unreadable")]
    return out
