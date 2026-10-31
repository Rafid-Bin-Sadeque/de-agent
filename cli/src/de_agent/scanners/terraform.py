"""`terraform` scan (v0.2 section 2): resources catalogued from .tf files, data resources turned into lineage edges.

Parsing uses `python-hcl2` when it is installed (extra `terraform`); otherwise a small built-in reader handles the same
shapes (blocks, `key = value`, lists, objects, heredocs, `jsonencode(..)`). Both produce the same normalised attribute
tree, which is all the handlers below read. Nothing is evaluated: a value that is a variable or a reference resolves only
when it points at a literal default or at a resource attribute in the scan; otherwise the edge is not drawn and the name
goes to the `unresolved` list.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from de_agent.scanners.common import analyze_sql, rel_to

try:  # optional extra
    import hcl2  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - exercised through the fallback tests
    hcl2 = None

SKIP_PARTS = {".terraform", ".git", "node_modules", ".venv", "venv", "__pycache__"}
BLOCK_KINDS = {"resource", "data", "variable", "locals", "module", "provider", "output", "terraform"}
MAX_STR = 20000
MAX_DEPTH = 5


def parser_name() -> str:
    return "hcl2" if hcl2 is not None else "regex"


# =========================== built-in reader ===========================

def _skip_string(s: str, i: int) -> int:
    """s[i] == '"'; index just after the closing quote (interpolations `${..}` may nest quotes)."""
    n = len(s)
    i += 1
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        if c == "$" and s[i + 1:i + 2] == "{":
            i = _skip_interp(s, i + 2)
            continue
        i += 1
    return n


def _skip_interp(s: str, i: int) -> int:
    depth, n = 1, len(s)
    while i < n and depth:
        c = s[i]
        if c == '"':
            i = _skip_string(s, i)
            continue
        depth += (c == "{") - (c == "}")
        i += 1
    return i


_HEREDOC_RE = re.compile(r"<<(-?)([A-Za-z_]\w*)[ \t]*\r?\n")


def _skip_heredoc(s: str, m: re.Match[str]) -> tuple[int, str]:
    """(index after the closing delimiter line, body text)."""
    delim, indent = m.group(2), m.group(1) == "-"
    i = m.end()
    body: list[str] = []
    n = len(s)
    while i < n:
        j = s.find("\n", i)
        j = n if j == -1 else j
        line = s[i:j]
        i = min(j + 1, n)
        if (line.strip() if indent else line.rstrip("\r")) == delim:
            return i, "\n".join(body)
        body.append(line)
    return n, "\n".join(body)


def _skip_comment(s: str, i: int) -> int | None:
    if s.startswith(("#", "//"), i):
        j = s.find("\n", i)
        return len(s) if j == -1 else j
    if s.startswith("/*", i):
        j = s.find("*/", i)
        return len(s) if j == -1 else j + 2
    return None


def _block_end(s: str, open_idx: int) -> int:
    """Index just after the brace matching s[open_idx] == '{' (strings, comments and heredocs skipped)."""
    depth, i, n = 0, open_idx, len(s)
    while i < n:
        c = s[i]
        if c == '"':
            i = _skip_string(s, i)
            continue
        cm = _skip_comment(s, i)
        if cm is not None:
            i = cm
            continue
        if c == "<" and s.startswith("<<", i):
            m = _HEREDOC_RE.match(s, i)
            if m:
                i, _ = _skip_heredoc(s, m)
                continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _expr_end(s: str, i: int, stop_commas: bool) -> tuple[int, str | None]:
    """End of one expression starting at i; (index, heredoc body or None). Stops at a newline (or comma) at depth 0."""
    depth, n = 0, len(s)
    m = _HEREDOC_RE.match(s, i) if s.startswith("<<", i) else None
    if m:
        return _skip_heredoc(s, m)
    while i < n:
        c = s[i]
        if c == '"':
            i = _skip_string(s, i)
            continue
        cm = _skip_comment(s, i)
        if cm is not None and depth == 0:
            return i, None
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return i, None
            depth -= 1
        elif depth == 0 and (c == "\n" or (stop_commas and c == ",")):
            return i, None
        i += 1
    return n, None


class _Heredoc(str):
    """The body of a `<<EOT ... EOT` value: a plain string, never an expression."""


def _items(s: str) -> list[tuple[str, str, Any, int]]:
    """Top-level items of a block body: ("attr", name, raw, offset) | ("block", name, (labels, body), offset)."""
    out: list[tuple[str, str, Any, int]] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in " \t\r\n,":
            i += 1
            continue
        cm = _skip_comment(s, i)
        if cm is not None:
            i = cm
            continue
        start = i
        if c == '"':
            j = _skip_string(s, i)
            name = s[i + 1:j - 1]
        else:
            m = re.compile(r"[A-Za-z_][\w.-]*").match(s, i)
            if not m:
                j2 = s.find("\n", i)
                i = n if j2 == -1 else j2
                continue
            j, name = m.end(), m.group(0)
        k = j
        while k < n and s[k] in " \t":
            k += 1
        if k < n and s[k] in "=:" and s[k:k + 2] != "==":
            k += 1
            while k < n and s[k] in " \t":
                k += 1
            end, heredoc = _expr_end(s, k, stop_commas=True)
            out.append(("attr", name, _Heredoc(heredoc) if heredoc is not None else s[k:end].strip(), start))
            i = end
            continue
        labels: list[str] = []
        while k < n:
            while k < n and s[k] in " \t":
                k += 1
            if k < n and s[k] == '"':
                e = _skip_string(s, k)
                labels.append(s[k + 1:e - 1])
                k = e
                continue
            m = re.compile(r"[A-Za-z_][\w-]*").match(s, k)
            if m:
                labels.append(m.group(0))
                k = m.end()
                continue
            break
        if k < n and s[k] == "{":
            e = _block_end(s, k)
            out.append(("block", name, (labels, s[k + 1:e - 1]), start))
            i = e
            continue
        j2 = s.find("\n", k)
        i = n if j2 == -1 else j2
    return out


def _unquote(raw: str) -> str:
    return (raw[1:-1].replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n")) if len(raw) >= 2 else raw


def _split_commas(s: str) -> list[str]:
    parts: list[str] = []
    depth, i, start, n = 0, 0, 0, len(s)
    while i < n:
        c = s[i]
        if c == '"':
            i = _skip_string(s, i)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append(s[start:i])
            start = i + 1
        i += 1
    tail = s[start:]
    if tail.strip():
        parts.append(tail)
    return [p.strip() for p in parts if p.strip()]


def _value(raw: Any, depth: int = 0) -> Any:
    """Raw expression text -> plain value: str | list | dict. Unevaluable expressions become `${expr}`."""
    if not isinstance(raw, str):
        return raw
    if isinstance(raw, _Heredoc):
        return str(raw)[:MAX_STR]
    raw = raw.strip()
    if depth > MAX_DEPTH:
        return "${...}"
    if raw.startswith('"') and _skip_string(raw, 0) == len(raw):
        return _unquote(raw)
    if raw.startswith("[") and raw.endswith("]"):
        return [_value(p, depth + 1) for p in _split_commas(raw[1:-1])]
    if raw.startswith("{") and raw.endswith("}"):
        return _body(raw[1:-1], depth + 1)
    m = re.match(r"^(jsonencode|tolist|toset|tomap)\((.*)\)$", raw, re.S)
    if m:
        return _value(m.group(2), depth + 1)
    if re.fullmatch(r"-?\d+(?:\.\d+)?|true|false|null", raw):
        return raw
    return "${" + raw + "}"


def _body(s: str, depth: int = 0) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for kind, name, payload, _off in _items(s):
        if kind == "attr":
            out[name] = _value(payload, depth)
        else:
            cur = out.get(name)
            if not isinstance(cur, list):
                cur = out[name] = []
            cur.append(_body(payload[1], depth + 1))
    return out


# =========================== normalised attribute tree ===========================

def _norm(v: Any, depth: int = 0) -> Any:
    """hcl2 output -> the same shape the built-in reader produces (quotes removed, block markers dropped)."""
    if depth > MAX_DEPTH:
        return "${...}"
    if isinstance(v, str):
        if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
            v = _unquote(v)
        hd = re.match(r"^<<-?([A-Za-z_]\w*)[ \t]*\r?\n(.*?)\r?\n?[ \t]*\1\s*$", v, re.S)
        if hd:
            return hd.group(2)[:MAX_STR]
        m = re.match(r"^\$\{(?:jsonencode|tolist|toset|tomap)\((.*)\)\}$", v, re.S)
        if m:
            return "${" + m.group(1) + "}" if not m.group(1).startswith(("{", "[")) else "${jsonencode(" + m.group(1) + ")}"
        return v[:MAX_STR]
    if isinstance(v, dict):
        return {_unquote(k) if isinstance(k, str) and k.startswith('"') else k: _norm(x, depth + 1)
                for k, x in v.items() if k != "__is_block__"}
    if isinstance(v, list):
        return [_norm(x, depth + 1) for x in v]
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    return str(v)


def attrs_for(block_text: str, body: str, errors: list[str], where: str) -> dict[str, Any]:
    """Attributes of one block; hcl2 when installed (falls back per block), the built-in reader otherwise."""
    if hcl2 is not None:
        try:
            tree = hcl2.loads(block_text)
            for entries in tree.values():
                for ent in entries:
                    if isinstance(ent, dict):
                        node: Any = ent
                        while isinstance(node, dict) and len(node) == 1 and all(
                                isinstance(k, str) and (k.startswith('"') or k == "locals") for k in node):
                            node = next(iter(node.values()))
                        return _norm(node) if isinstance(node, dict) else {}
        except Exception as ex:
            errors.append(f"{where}: hcl2 could not parse ({type(ex).__name__}); used the built-in reader")
    return _body(body)


# =========================== per-file ===========================

def scan_terraform_text(text: str, rel: str, dialect: str) -> dict[str, Any]:
    """Resources, data sources, variables and locals of ONE .tf file (a pure function of the text)."""
    out: dict[str, Any] = {"rel": rel, "parser": parser_name(), "resources": [], "variables": {}, "locals": {}, "errors": []}
    try:
        items = _items(text)
    except RecursionError:
        out["errors"].append(f"{rel}: too deeply nested")
        return out
    for kind, name, payload, off in items:
        if kind != "block" or name not in BLOCK_KINDS:
            continue
        labels, body = payload
        line = text.count("\n", 0, off) + 1
        brace = text.find("{", off)
        full = text[off:brace] + "{" + body + "}"
        where = f"{rel}:{line}"
        if name in ("resource", "data") and len(labels) >= 2:
            attrs = attrs_for(full, body, out["errors"], where)
            out["resources"].append({"kind": name, "type": labels[0], "name": labels[1], "file": rel, "line": line,
                                     "attrs": _prune(attrs)})
        elif name == "variable" and labels:
            d = attrs_for(full, body, out["errors"], where).get("default")
            if isinstance(d, str):
                out["variables"][labels[0]] = d
        elif name == "locals":
            for k, v in (_body(body) if hcl2 is None else _locals_hcl2(full, body, out["errors"], where)).items():
                if isinstance(v, str):
                    out["locals"][k] = v
    return out


def _locals_hcl2(full: str, body: str, errors: list[str], where: str) -> dict[str, Any]:
    try:
        tree = hcl2.loads(full)
        loc = tree.get("locals", [{}])[0]
        return _norm(loc) if isinstance(loc, dict) else {}
    except Exception as ex:
        errors.append(f"{where}: hcl2 could not parse locals ({type(ex).__name__})")
        return _body(body)


def _prune(v: Any, depth: int = 0) -> Any:
    if depth > MAX_DEPTH:
        return None
    if isinstance(v, dict):
        return {k: _prune(x, depth + 1) for k, x in list(v.items())[:80]}
    if isinstance(v, list):
        return [_prune(x, depth + 1) for x in v[:80]]
    return v


# =========================== accessors ===========================

def walk(attrs: Any, path: str) -> list[Any]:
    """Every value at a dotted path, across repeated blocks: walk(a, "s3_target.path")."""
    cur = [attrs]
    for key in path.split("."):
        nxt: list[Any] = []
        for c in cur:
            if isinstance(c, list):
                for el in c:
                    if isinstance(el, dict) and key in el:
                        nxt.append(el[key])
            elif isinstance(c, dict) and key in c:
                nxt.append(c[key])
        cur = nxt
    flat: list[Any] = []
    for c in cur:
        flat += c if isinstance(c, list) and c and not isinstance(c[0], dict) else [c]
    return flat


def first(attrs: Any, path: str) -> Any:
    vals = walk(attrs, path)
    return vals[0] if vals else None


# =========================== assemble: handlers ===========================

REF_RE = re.compile(r"^\$\{\s*([^{}]*?)\s*\}$")
ADDR_RE = re.compile(r"^(?!var\.|local\.|data\.|module\.|each\.|count\.|path\.|self\.)([a-z][a-z0-9_]*)\.([A-Za-z0-9_-]+)(?:\.|\[|$)")
NAME_ATTR = {"aws_s3_bucket": "bucket", "google_storage_bucket": "name", "aws_kinesis_stream": "name",
             "google_pubsub_topic": "name", "google_pubsub_subscription": "name",
             "aws_kinesis_firehose_delivery_stream": "name", "google_bigquery_dataset": "dataset_id",
             "snowflake_database": "name", "snowflake_schema": "name"}


class Ctx:
    def __init__(self, resources: list[dict[str, Any]], variables: dict[str, str], locals_: dict[str, str]) -> None:
        self.by_addr = {f"{r['type']}.{r['name']}": r for r in resources if r["kind"] == "resource"}
        self.variables, self.locals = variables, locals_
        self.unresolved: list[dict[str, str]] = []

    def _ref(self, ref: str, depth: int) -> str | None:
        got = None
        if ref.startswith("var."):
            got = self.variables.get(ref[4:].split(".")[0])
        elif ref.startswith("local."):
            got = self.locals.get(ref[6:].split(".")[0])
        else:
            am = ADDR_RE.match(ref)
            r = self.by_addr.get(f"{am.group(1)}.{am.group(2)}") if am else None
            key = NAME_ATTR.get(r["type"]) if r else None
            if r and key and isinstance(r["attrs"].get(key), str):
                got = r["attrs"][key]
        if got is not None and "${" in got:
            return self.text(got, "", False, depth + 1)
        return got

    def text(self, v: Any, owner: str = "", note: bool = True, depth: int = 0) -> str | None:
        """A string value resolved to a literal (variable defaults, locals and resource names are substituted), or None
        (noted as unresolved when `note`)."""
        if v is None or isinstance(v, (dict, list)):
            return None
        s = str(v)
        if "${" not in s:
            return s
        ok = depth < 4

        def one(m: re.Match[str]) -> str:
            nonlocal ok
            got = self._ref(m.group(1).strip(), depth) if ok else None
            if got is None:
                ok = False
                return ""
            return got

        out = re.sub(r"\$\{([^{}]*)\}", one, s)
        if ok:
            return out
        if note and owner:
            self.unresolved.append({"kind": "terraform-ref", "name": s[:80], "referenced_by": owner})
        return None

    def address(self, v: Any) -> dict[str, Any] | None:
        """The resource a reference like `aws_dms_endpoint.src.endpoint_arn` points at."""
        if not isinstance(v, str):
            return None
        m = REF_RE.match(v)
        am = ADDR_RE.match(m.group(1)) if m else None
        return self.by_addr.get(f"{am.group(1)}.{am.group(2)}") if am else None


def _bucket_from(ctx: Ctx, v: Any, owner: str) -> str | None:
    """Bucket name from a bucket name, an `arn:aws:s3:::bucket[/prefix]` or a reference to an aws_s3_bucket."""
    s = ctx.text(v, owner)
    if s is None:
        return None
    s = re.sub(r"^arn:aws[\w-]*:s3:::", "", s)
    return s.split("/")[0].lower() or None


def _s3(bucket: str, prefix: str = "") -> str:
    from de_agent.scanners.edges import s3_node

    return s3_node(bucket, prefix)


def _sql_flow(sql: str, dialect: str) -> tuple[list[str], list[str]]:
    res = analyze_sql(sql, dialect)
    return res["reads"], res["writes"]


def _dms_rules(raw: Any) -> list[dict[str, str]]:
    """Selection rules (schema, table, action) from a table_mappings value: jsonencode(..), a heredoc or a JSON string."""
    text = raw if isinstance(raw, str) else json.dumps(raw)
    rules: list[dict[str, str]] = []
    cur: dict[str, str] | None = None
    for m in re.finditer(r'"(rule-type|rule-action|schema-name|table-name)"\s*[=:]\s*\\?"([^"\\]*)\\?"', text):
        if m.group(1) == "rule-type":
            cur = {"type": m.group(2)}
            rules.append(cur)
        elif cur is not None:
            cur[{"rule-action": "action", "schema-name": "schema", "table-name": "table"}[m.group(1)]] = m.group(2)
    return [r for r in rules if r.get("type") == "selection" and r.get("action", "include") == "include"]


def h_dms_task(res: dict[str, Any], ctx: Ctx, owner: str, dialect: str) -> dict[str, Any]:
    a = res["attrs"]
    out: dict[str, Any] = {"pairs": [], "id": None}
    tid = ctx.text(a.get("replication_task_id"), owner, False) or res["name"]
    out["id"] = f"dms:{tid}"
    tgt = ctx.address(a.get("target_endpoint_arn"))
    if tgt is None:
        ctx.unresolved.append({"kind": "terraform-ref", "name": f"target_endpoint_arn of {owner}", "referenced_by": owner})
        return out
    tattrs = tgt["attrs"]
    bucket = ctx.text(first(tattrs, "s3_settings.bucket_name"), owner)
    folder = (ctx.text(first(tattrs, "s3_settings.bucket_folder"), owner, False) or "").strip("/")
    for r in _dms_rules(a.get("table_mappings")):
        sch, tbl = r.get("schema", "%"), r.get("table", "%")
        skey = f"{sch}.{tbl}".lower()
        pattern = "%" in sch or "%" in tbl
        if bucket:
            tkey = _s3(bucket, "/".join(x for x in (folder, sch, tbl) if x) + "/")
        elif tattrs.get("engine_name"):
            tkey = skey
        else:
            continue
        out["pairs"].append({"source": skey, "target": tkey, "pattern": pattern})
    return out


def h_firehose(res: dict[str, Any], ctx: Ctx, owner: str, dialect: str) -> dict[str, Any]:
    a = res["attrs"]
    out: dict[str, Any] = {"reads": [], "writes": [], "id": f"firehose:{ctx.text(a.get('name'), owner, False) or res['name']}"}
    arn = first(a, "extended_s3_configuration.bucket_arn") or first(a, "s3_configuration.bucket_arn")
    if arn is not None:
        b = _bucket_from(ctx, arn, owner)
        prefix = ctx.text(first(a, "extended_s3_configuration.prefix") or first(a, "s3_configuration.prefix"), owner, False) or ""
        if b:
            out["writes"].append(_s3(b, prefix))
    src = first(a, "kinesis_source_configuration.kinesis_stream_arn")
    if src is not None:
        r = ctx.address(src)
        name = ctx.text(r["attrs"].get("name"), owner) if r else ctx.text(src, owner)
        if name:
            out["reads"].append(f"kinesis://{name.split('/')[-1].lower()}")
    return out


def h_glue_table(res: dict[str, Any], ctx: Ctx, owner: str, dialect: str) -> dict[str, Any]:
    a = res["attrs"]
    db, name = ctx.text(a.get("database_name"), owner), ctx.text(a.get("name"), owner)
    out: dict[str, Any] = {"reads": [], "writes": [], "id": f"glue:{db}.{name}".lower() if db and name else None}
    loc = ctx.text(first(a, "storage_descriptor.location"), owner)
    m = re.match(r"^s3://([^/]+)/?(.*)$", loc or "")
    if m:
        out["reads"].append(_s3(m.group(1), m.group(2)))
    if db and name:
        out["writes"].append(f"{db}.{name}".lower())
    return out


def h_glue_crawler(res: dict[str, Any], ctx: Ctx, owner: str, dialect: str) -> dict[str, Any]:
    out: dict[str, Any] = {"reads": [], "writes": [], "id": f"glue-crawler:{ctx.text(res['attrs'].get('name'), owner, False) or res['name']}"}
    for p in walk(res["attrs"], "s3_target.path"):
        loc = ctx.text(p, owner)
        m = re.match(r"^s3://([^/]+)/?(.*)$", loc or "")
        if m:
            out["reads"].append(_s3(m.group(1), m.group(2)))
    return out


_OUT_KEY = re.compile(r"output|target|dest|sink|write", re.I)
_IN_KEY = re.compile(r"input|source|src|read", re.I)


def h_glue_job(res: dict[str, Any], ctx: Ctx, owner: str, dialect: str) -> dict[str, Any]:
    """`default_arguments` that hold an s3:// path become edges; the direction comes from the argument's name
    (output/target/dest -> writes, input/source -> reads, otherwise reads and marked unclear)."""
    a = res["attrs"]
    out: dict[str, Any] = {"reads": [], "writes": [], "unclear": [],
                           "id": f"glue-job:{ctx.text(a.get('name'), owner, False) or res['name']}"}
    args = first(a, "default_arguments")
    for k, v in (args.items() if isinstance(args, dict) else []):
        if not str(v).startswith(("s3://", "s3a://")):
            continue
        s = ctx.text(v, owner)
        m = re.match(r"^s3a?://([^/]+)/?(.*)$", s or "")
        if not m:
            continue
        node = _s3(m.group(1), m.group(2))
        if _OUT_KEY.search(k):
            out["writes"].append(node)
        elif _IN_KEY.search(k):
            out["reads"].append(node)
        else:
            out["reads"].append(node)
            out["unclear"].append(node)
    return out


def h_mwaa(res: dict[str, Any], ctx: Ctx, owner: str, dialect: str) -> dict[str, Any]:
    a = res["attrs"]
    out: dict[str, Any] = {"reads": [], "writes": [], "id": f"mwaa:{ctx.text(a.get('name'), owner, False) or res['name']}"}
    b = _bucket_from(ctx, a.get("source_bucket_arn"), owner) if a.get("source_bucket_arn") is not None else None
    if b:
        out["reads"].append(_s3(b, ctx.text(a.get("dag_s3_path"), owner, False) or ""))
    return out


def h_bq_table(res: dict[str, Any], ctx: Ctx, owner: str, dialect: str) -> dict[str, Any]:
    a = res["attrs"]
    ds, tid = ctx.text(a.get("dataset_id"), owner), ctx.text(a.get("table_id"), owner)
    out: dict[str, Any] = {"reads": [], "writes": [], "id": f"bq:{ds}.{tid}".lower() if ds and tid else None}
    if ds and tid:
        out["writes"].append(f"{ds}.{tid}".lower())
    q = ctx.text(first(a, "view.query"), owner, False)
    if q:
        out["reads"] += _sql_flow(q, "bigquery")[0]
    return out


def _sf_name(ctx: Ctx, a: dict[str, Any], owner: str) -> str | None:
    sch, name = ctx.text(a.get("schema"), owner, False) or ctx.text(a.get("schema_name"), owner, False), ctx.text(a.get("name"), owner)
    return f"{sch}.{name}".lower() if sch and name else None


def h_sf_object(res: dict[str, Any], ctx: Ctx, owner: str, dialect: str) -> dict[str, Any]:
    a = res["attrs"]
    t = res["type"]
    key = _sf_name(ctx, a, owner)
    out: dict[str, Any] = {"reads": [], "writes": [], "id": f"sf:{key}" if key else None}
    if t in ("snowflake_table", "snowflake_table_legacy") and key:
        out["writes"].append(key)
    stmt = ctx.text(a.get("statement") or a.get("sql_statement") or a.get("copy_statement"), owner, False)
    if stmt:
        r, w = _sql_flow(stmt, "snowflake")
        out["reads"] += r
        out["writes"] += w
        if t in ("snowflake_view", "snowflake_materialized_view") and key:
            out["writes"].append(key)
    return out


def h_db_sql(res: dict[str, Any], ctx: Ctx, owner: str, dialect: str) -> dict[str, Any]:
    a = res["attrs"]
    out: dict[str, Any] = {"reads": [], "writes": [], "id": f"databricks:{res['type']}.{res['name']}"}
    q = ctx.text(a.get("query") or a.get("statement"), owner, False)
    if q:
        r, w = _sql_flow(q, "databricks")
        out["reads"] += r
        out["writes"] += w
    return out


def h_pubsub_sub(res: dict[str, Any], ctx: Ctx, owner: str, dialect: str) -> dict[str, Any]:
    a = res["attrs"]
    sub = ctx.text(a.get("name"), owner, False) or res["name"]
    out: dict[str, Any] = {"reads": [], "writes": [], "id": f"pubsub:{sub}"}
    topic = a.get("topic")
    r = ctx.address(topic)
    tname = ctx.text(r["attrs"].get("name"), owner) if r else ctx.text(topic, owner)
    if tname:
        out["reads"].append(f"pubsub://topic/{tname.split('/')[-1].lower()}")
    bq = ctx.text(first(a, "bigquery_config.table"), owner, False)
    if bq:
        out["writes"].append(norm_bq(bq))
    gcs = first(a, "cloud_storage_config.bucket")
    if gcs is not None:
        b = ctx.text(gcs, owner)
        if b:
            out["writes"].append(f"gs://{b.lower()}/")
    return out


def norm_bq(t: str) -> str:
    parts = re.split(r"[.:]", t.strip("`").lower())
    return ".".join(parts[-2:])


HANDLERS = {
    "aws_dms_replication_task": h_dms_task,
    "aws_kinesis_firehose_delivery_stream": h_firehose,
    "aws_glue_catalog_table": h_glue_table,
    "aws_glue_crawler": h_glue_crawler,
    "aws_glue_job": h_glue_job,
    "aws_mwaa_environment": h_mwaa,
    "google_bigquery_table": h_bq_table,
    "snowflake_table": h_sf_object, "snowflake_table_legacy": h_sf_object, "snowflake_view": h_sf_object,
    "snowflake_materialized_view": h_sf_object, "snowflake_task": h_sf_object, "snowflake_pipe": h_sf_object,
    "databricks_sql_query": h_db_sql, "databricks_query": h_db_sql,
    "google_pubsub_subscription": h_pubsub_sub,
}
# data-relevant types that are catalogued as nodes even when no edge names them
NODE_TYPES = {
    "aws_dms_endpoint", "aws_dms_replication_instance", "aws_glue_catalog_database", "aws_kinesis_stream",
    "aws_s3_bucket", "google_storage_bucket", "google_bigquery_dataset", "google_pubsub_topic",
    "google_composer_environment", "snowflake_database", "snowflake_schema", "databricks_job", "databricks_pipeline",
    "aws_redshift_cluster", "aws_redshift_serverless_workgroup", "aws_db_instance", "aws_rds_cluster",
}
DATA_TYPES = set(HANDLERS) | NODE_TYPES


def node_key_of(res: dict[str, Any], ctx: Ctx) -> str | None:
    """The graph key of a catalogued node resource (a bucket, topic, stream...), when its name is literal."""
    t, a = res["type"], res["attrs"]
    owner = f"{t}.{res['name']}"
    if t == "aws_s3_bucket":
        b = ctx.text(a.get("bucket"), owner, False)
        return _s3(b) if b else None
    if t == "google_storage_bucket":
        b = ctx.text(a.get("name"), owner, False)
        return f"gs://{b.lower()}/" if b else None
    if t == "aws_kinesis_stream":
        n = ctx.text(a.get("name"), owner, False)
        return f"kinesis://{n.lower()}" if n else None
    if t == "google_pubsub_topic":
        n = ctx.text(a.get("name"), owner, False)
        return f"pubsub://topic/{n.lower()}" if n else None
    return None


def assemble(scan: Any, per_file: dict[str, dict[str, Any]], revision: str) -> dict[str, Any]:
    resources: list[dict[str, Any]] = []
    variables: dict[str, str] = {}
    locals_: dict[str, str] = {}
    errors: list[str] = []
    parsers: set[str] = set()
    for rel in sorted(per_file):
        pf = per_file[rel]
        resources += pf["resources"]
        variables.update(pf["variables"])
        locals_.update(pf["locals"])
        errors += pf["errors"]
        parsers.add(pf.get("parser", "regex"))
    resources.sort(key=lambda r: (r["file"], r["line"], r["type"], r["name"]))
    ctx = Ctx(resources, variables, locals_)
    nodes: list[dict[str, Any]] = []
    types: dict[str, int] = {}
    for r in resources:
        if r["kind"] != "resource":
            continue
        types[r["type"]] = types.get(r["type"], 0) + 1
        owner = f"{r['type']}.{r['name']}"
        node: dict[str, Any] = {"address": owner, "type": r["type"], "name": r["name"], "file": r["file"], "line": r["line"],
                                "data": r["type"] in DATA_TYPES, "reads": [], "writes": [], "pairs": [], "unclear": [], "id": None,
                                "key": None}
        h = HANDLERS.get(r["type"])
        if h:
            got = h(r, ctx, owner, scan.dialect)
            for k in ("reads", "writes", "pairs", "unclear"):
                node[k] = sorted(dict.fromkeys(got.get(k, []))) if k != "pairs" else sorted(
                    got.get(k, []), key=lambda p: (p["source"], p["target"]))
            node["id"] = got.get("id")
        elif r["type"] in NODE_TYPES:
            node["key"] = node_key_of(r, ctx)
        if node["id"] is None:
            node["id"] = f"tf:{owner}"
        nodes.append(node)
    seen = set()
    unresolved = [u for u in ctx.unresolved if not (repr(u) in seen or seen.add(repr(u)))]
    return {"scan": scan.name, "type": "terraform", "project": scan.project_name, "revision": revision, "nodes": nodes,
            "unresolved_refs": sorted(unresolved, key=lambda u: (u["name"], u["referenced_by"])),
            "stats": {"files": len(per_file), "resources": len(nodes), "data_nodes": sum(1 for n in nodes if n["data"]),
                      "edges": sum(len(n["reads"]) + len(n["writes"]) + 2 * len(n["pairs"]) for n in nodes),
                      "unresolved_refs": len(unresolved), "parse_errors": len(errors),
                      "parser": ",".join(sorted(parsers)) or "regex"},
            "errors": errors, "types": dict(sorted(types.items()))}


def list_files(scan: Any, root: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for p in sorted(root.rglob("*.tf")):
        if p.is_file() and not (set(p.relative_to(root).parts) & SKIP_PARTS):
            out[rel_to(root, p)] = p
    return out
