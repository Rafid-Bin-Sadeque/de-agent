"""`dea pack check`: schema, expressions, SQL read-classification, argv read-verb check, http method rules.

The runtime calls `check_pack` before a pack runs and re-applies the per-statement / per-argv / per-request checks right before
each execution, so a pack that fails here never reaches a database, a subprocess or a socket.
"""
from __future__ import annotations

import re
from typing import Any

from de_agent.packs import argv as argv_rules
from de_agent.packs import template
from de_agent.packs.errors import PackError
from de_agent.packs.expr import compile_expr
from de_agent.packs.model import Pack, Section, parse_pack
from de_agent.sqlclass import classify

MSG_FIELD = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
SQLGLOT_DIALECT = {"postgres": "postgres", "redshift": "redshift", "mysql": "mysql", "snowflake": "snowflake",
                   "bigquery": "bigquery", "tsql": "tsql", "databricks": "databricks"}
SAMPLE = "x"


# ---- per-kind refusal functions (shared with the runtime) ---------------------------------------------------

def sql_refusal(sql: str, dialect: str, params: dict[str, Any]) -> str | None:
    """None when `sql` is exactly one read statement in `dialect`; else why not. `params` maps name -> spec or value."""
    text = sql

    def bind(m: re.Match[str]) -> str:
        name = m.group(1)
        spec = params.get(name)
        if spec is None:
            return m.group(0)
        typ = getattr(spec, "type", None)
        if typ == "list" or isinstance(spec, list):
            return "('x')"
        return "'x'"

    text = template.PLACEHOLDER.sub(lambda m: SAMPLE, text)
    text = template.BIND.sub(bind, text)
    try:
        stmts = classify(text, SQLGLOT_DIALECT.get(dialect, dialect))
    except Exception as ex:  # a parse failure is a refusal
        return f"cannot classify SQL ({type(ex).__name__})"
    if not stmts:
        return "empty SQL"
    if len(stmts) != 1:
        return f"a section runs exactly one statement (got {len(stmts)})"
    if stmts[0].kind != "read":
        return f"{stmts[0].label} is a write ({stmts[0].reason})"
    return None


def graphql_refusal(query: str) -> str | None:
    from de_agent.collectors._pack_http_core import graphql_refusal as core_refusal

    return core_refusal(query)


def path_refusal(path: str) -> str | None:
    from de_agent.collectors._pack_http_core import path_refusal as core_refusal

    return core_refusal(path)


# ---- pack check ---------------------------------------------------------------------------------------------

def _known_fields(sec: Section) -> set[str] | None:
    names: set[str] | None = None
    if sec.columns:
        names = set(sec.columns)
    elif sec.fields:
        names = set(sec.fields)
    if names is not None and sec.item_as:
        names.add(sec.item_as)
    return names


def _check_jmespath(expr: str, where: str, errs: list[str]) -> None:
    try:
        import jmespath

        jmespath.compile(expr)
    except ImportError:
        errs.append(f"{where}: jmespath is not installed (it is a core dependency: reinstall de-agent)")
    except Exception as ex:
        errs.append(f"{where}: invalid JMESPath ({type(ex).__name__}: {str(ex).splitlines()[0] if str(ex) else ''})")


def _check_section(pack: Pack, sec: Section, errs: list[str]) -> None:
    w = f"section {sec.name}"
    specs = pack.params

    def err(msg: str) -> None:
        errs.append(f"{w}: {msg}")

    if sec.for_each:
        spec = specs.get(sec.for_each)
        if spec is None or spec.type != "list":
            err(f"for_each {sec.for_each!r} must name a param of type list")
    if sec.item_as and not sec.for_each:
        err("item_as needs for_each")
    text = sec.sql or sec.get or sec.graphql or " ".join(sec.run or [])
    for name in sorted(template.used_placeholders(text)):
        if name == "item":
            if not sec.for_each:
                err("{{item}} needs for_each")
        elif template.is_builtin(name):
            if pack.kind == "sql":
                err(f"built-in {{{{{name}}}}} is not available in SQL (write the window in SQL)")
        elif name not in specs:
            err(f"unknown template variable {{{{{name}}}}} (declare it under params:)")
        elif pack.kind == "sql" and specs[name].type != "ident":
            err(f"{{{{{name}}}}} in SQL must be a param of type ident (use a bind parameter :{name} for values)")
        elif specs[name].type == "list":
            err(f"{{{{{name}}}}} is a list; use for_each")

    if pack.kind == "sql":
        if sec.sql is None:
            err("a sql pack section needs `sql:`")
        else:
            binds = set(template.BIND.findall(sec.sql))
            for b in sorted(binds):
                if b not in specs:
                    err(f"bind parameter :{b} is not declared under params:")
                elif specs[b].type == "ident":
                    err(f"param {b} is of type ident: use {{{{{b}}}}}, not :{b}")
            why = sql_refusal(sec.sql, pack.dialect or "postgres", specs)
            if why:
                err(f"sql is not a single read statement in {pack.dialect}: {why}")
        if sec.select or sec.fields:
            err("`select`/`fields` are for cli and http sections")
        if sec.for_each:
            err("for_each is for cli and http sections")
    elif pack.kind == "cli":
        if sec.run is None:
            err("a cli pack section needs `run: [argv...]`")
        else:
            sample = [template.PLACEHOLDER.sub(SAMPLE, a) for a in sec.run]
            why = argv_rules.refusal(sample)
            if why:
                err(f"run is not a recognised read-only command: {why}")
            if pack.requires.cli and sec.run and sec.run[0] != pack.requires.cli:
                err(f"run starts with {sec.run[0]!r} but requires.cli is {pack.requires.cli!r}")
    else:
        if sec.get is None and sec.graphql is None:
            err("an http pack section needs `get:` or `graphql:`")
        if sec.get is not None:
            why = path_refusal(template.PLACEHOLDER.sub(SAMPLE, sec.get))
            if why:
                err(f"get: {why}")
        if sec.graphql is not None:
            why = graphql_refusal(template.PLACEHOLDER.sub(SAMPLE, sec.graphql))
            if why:
                err(f"graphql: {why}")
        if sec.path is not None:
            if sec.graphql is None:
                err("`path` is for graphql sections")
            else:
                why = path_refusal(sec.path)
                if why:
                    err(f"path: {why}")
    if sec.select:
        _check_jmespath(sec.select, f"{w} select", errs)
    for out, jp in (sec.fields or {}).items():
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", out):
            err(f"fields: output name {out!r} must be a plain identifier")
        _check_jmespath(jp, f"{w} fields.{out}", errs)

    known = _known_fields(sec)
    if sec.columns and sec.fields:
        extra = [c for c in sec.columns if c not in sec.fields and c != sec.item_as]
        if extra:
            err(f"columns not produced by fields: {', '.join(extra)}")
    for k in sec.keys:
        if known is not None and k not in known:
            err(f"key {k!r} is not among the section's columns/fields")
    for v in sec.volatile:
        if known is not None and v not in known:
            err(f"volatile field {v!r} is not among the section's columns/fields")

    for i, sig in enumerate(sec.signals):
        sw = f"{w} signal {i + 1}"
        mode = "rows" if sig.rows is not None else "row"
        try:
            ex = compile_expr(sig.rows if sig.rows is not None else sig.when, mode)
        except PackError as e:
            errs.append(f"{sw}: {e}")
            continue
        for n in ex.names:
            if n.startswith("p_"):
                if n[2:] not in specs:
                    errs.append(f"{sw}: {n} refers to undeclared param {n[2:]!r}")
            elif mode == "row" and known is not None and n not in known:
                errs.append(f"{sw}: name {n!r} is not among the section's columns/fields")
        for n in MSG_FIELD.findall(sig.message):
            if mode == "rows":
                if n != "count" and not (n.startswith("p_") and n[2:] in specs):
                    errs.append(f"{sw}: message uses {{{n}}}; a `rows:` rule only has {{count}}")
            elif known is not None and n not in known and not (n.startswith("p_") and n[2:] in specs):
                errs.append(f"{sw}: message uses {{{n}}}, which is not a column/field")


def check_pack(pack: Pack) -> list[str]:
    errs: list[str] = []
    for name, spec in pack.params.items():
        if spec.default is not None:
            try:
                template.resolve_params({name: spec}, {})
            except PackError as e:
                errs.append(f"param {name}: {e}")
    if pack.kind == "sql" and pack.requires.cli:
        errs.append("requires.cli is for cli packs")
    if pack.kind != "sql" and pack.requires.extra not in (None, "aws", "terraform"):
        errs.append("requires.extra of a cli/http pack is only for CLI helper extras; use requires.cli")
    for sec in pack.sections:
        _check_section(pack, sec, errs)
    return errs


def check_text(text: str) -> tuple[Pack | None, list[str]]:
    """(pack, errors): errors is empty when the pack is valid."""
    try:
        pack = parse_pack(text)
    except PackError as e:
        return None, [str(e)]
    return pack, check_pack(pack)
