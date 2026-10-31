"""Shared machinery of the generic pack collectors (`type: sql`, `type: cli`, `type: http`).

A pack collector loads the source's pack, refuses to run when `pack check` fails, runs each section through the kind-specific
`run_section` (which performs the read-only enforcement right before executing), shapes the rows, evaluates the pack's signal
rules and returns the usual `{"data", "errors"}`. The result is self-contained: `data["meta"]` carries the pack's identity, keys,
rendering hints and the evaluated signals, so render / diff / find work without the pack file.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from de_agent.collectors.base import CollectorError, RunContext
from de_agent.packs import template
from de_agent.packs.check import MSG_FIELD, check_pack
from de_agent.packs.errors import PackError
from de_agent.packs.expr import compile_expr
from de_agent.packs.loader import load_pack
from de_agent.packs.model import Pack, Section

MAX_SIGNALS_PER_RULE = 10
MAX_JSON_BYTES = 5 * 1024 * 1024


def load_for_source(source: Any, kind: str) -> Pack:
    if not source.pack:
        raise ValueError(f"{kind} sources need `pack: <name>` (see `dea pack list`)")
    try:
        pack = load_pack(source.pack)
    except PackError as ex:
        raise ValueError(str(ex)) from None
    if pack.kind != kind:
        raise ValueError(f"pack {pack.pack!r} is kind {pack.kind}; a type: {kind} source needs a {kind} pack")
    try:
        template.resolve_params(pack.params, source.options)
    except PackError as ex:
        raise ValueError(str(ex)) from None
    return pack


def validate_for_config(source: Any, kind: str) -> Pack | None:
    """Config-load checks: the pack exists, matches the source type and the options fit its params. A pack that does not even
    parse returns None here (it must not break every `dea` command); `collect`, `dea doctor` and `dea pack check` report it."""
    if not source.pack:
        raise ValueError(f"{kind} sources need `pack: <name>` (see `dea pack list`)")
    from de_agent.packs.loader import find_pack

    try:
        ref = find_pack(source.pack)
    except PackError as ex:
        raise ValueError(str(ex)) from None
    try:
        pack = ref.parse()
    except PackError:
        return None
    if pack.kind != kind:
        raise ValueError(f"pack {pack.pack!r} is kind {pack.kind}; a type: {kind} source needs a {kind} pack")
    try:
        template.resolve_params(pack.params, source.options)
    except PackError as ex:
        raise ValueError(str(ex)) from None
    return pack


def apply_select(doc: Any, sec: Section) -> list[Any]:
    """JMESPath `select` then `fields` over a parsed JSON document -> a list of rows."""
    import jmespath

    val = jmespath.search(sec.select, doc) if sec.select else doc
    items = val if isinstance(val, list) else ([] if val is None else [val])
    if sec.fields:
        compiled = {k: jmespath.compile(v) for k, v in sec.fields.items()}
        items = [{k: c.search(it) for k, c in compiled.items()} for it in items]
    return items


def _clean(v: Any) -> Any:
    from de_agent.sqlexec import normalize_value

    if isinstance(v, (dict, list)) or (isinstance(v, int) and not isinstance(v, bool)):
        return v  # JSON containers and integers (ids above 2**53 must not become floats) pass through
    return normalize_value(v)


def shape_rows(items: list[Any], sec: Section, extra: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    rows = [{**it, **(extra or {})} if isinstance(it, dict) else {"value": it, **(extra or {})} for it in items]
    rows = [{k: _clean(v) for k, v in r.items()} for r in rows]
    if sec.columns:
        rows = [{c: r.get(c) for c in sec.columns} for r in rows]
    return rows


def _format(message: str, env: dict[str, Any]) -> str:
    def sub(m: re.Match[str]) -> str:
        v = env.get(m.group(1))
        if v is None:
            return "-"
        if isinstance(v, float):
            return f"{v:.2f}".rstrip("0").rstrip(".")
        return str(v)

    return MSG_FIELD.sub(sub, message)


def eval_signals(sec: Section, rows: list[dict[str, Any]], params: dict[str, Any], now: datetime,
                 errors: list[str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    penv = {f"p_{k}": v for k, v in params.items()}
    for sig in sec.signals:
        try:
            if sig.rows is not None:
                env = {"count": len(rows), **penv}
                if compile_expr(sig.rows, "rows").truthy(env, now):
                    out.append({"section": sec.name, "severity": sig.severity, "text": _format(sig.message, env)})
                continue
            expr = compile_expr(sig.when, "row")
            hits = [r for r in rows if expr.truthy({**penv, **r}, now)]
        except PackError as ex:
            errors.append(f"{sec.name}: signal rule skipped: {ex}")
            continue
        for r in hits[:MAX_SIGNALS_PER_RULE]:
            out.append({"section": sec.name, "severity": sig.severity, "text": _format(sig.message, {**penv, **r})})
        if len(hits) > MAX_SIGNALS_PER_RULE:
            out.append({"section": sec.name, "severity": sig.severity,
                        "text": f"{sec.name}: {len(hits) - MAX_SIGNALS_PER_RULE} more rows matched the same rule"})
    return out


class PackCollector:
    """Base of SqlPackCollector / CliPackCollector / HttpPackCollector."""

    type = ""
    schema_version = 1
    list_keys: dict[str, list[str]] = {}
    volatile_fields: set[str] = set()

    # diff reads these per result: the pack decides the keys and which fields are noise
    def list_keys_for(self, result: dict[str, Any]) -> dict[str, list[str]]:
        return (result.get("data") or {}).get("meta", {}).get("list_keys", {})

    def volatile_for(self, result: dict[str, Any]) -> set[str]:
        return set((result.get("data") or {}).get("meta", {}).get("volatile", []))

    @staticmethod
    def validate_options(options: dict[str, Any]) -> None:
        if not isinstance(options, dict):
            raise ValueError("options must be a mapping")

    def validate_source(self, source: Any) -> None:  # kind-specific subclasses extend this
        validate_for_config(source, self.type)

    # ---- to be provided by the kind --------------------------------------------------------------------
    def open(self, source: Any, ctx: RunContext, pack: Pack, params: dict[str, Any]) -> Any:  # noqa: ARG002
        return None

    def run_section(self, state: Any, source: Any, ctx: RunContext, pack: Pack, sec: Section, item: str | None,
                    params: dict[str, Any], now: datetime) -> list[Any]:
        raise NotImplementedError

    def close(self, state: Any) -> None:
        return None

    def ping(self, source: Any, ctx: RunContext) -> str:  # dea doctor
        pack = load_for_source(source, self.type)
        errs = check_pack(pack)
        if errs:
            raise CollectorError(f"pack {pack.pack} fails `dea pack check`: {errs[0]}")
        return self.ping_kind(source, ctx, pack)

    def ping_kind(self, source: Any, ctx: RunContext, pack: Pack) -> str:
        return f"pack {pack.pack} ok"

    # ---- common flow ------------------------------------------------------------------------------------
    def collect(self, source: Any, ctx: RunContext) -> dict[str, Any]:
        try:
            pack = load_for_source(source, self.type)
            params = template.resolve_params(pack.params, source.options)
        except (ValueError, PackError) as ex:
            return {"data": {}, "errors": [str(ex)]}
        problems = check_pack(pack)
        if problems:  # a pack that fails `pack check` never runs
            return {"data": {}, "errors": [f"pack {pack.pack} failed `dea pack check` and was not run: {p}" for p in problems[:5]]}
        now = ctx.now or datetime.now(timezone.utc)
        data: dict[str, Any] = {}
        errors: list[str] = []
        meta: dict[str, Any] = {
            "pack": pack.pack, "pack_version": pack.version, "kind": pack.kind, "validated": pack.validated,
            "list_keys": {s.name: s.keys for s in pack.sections if s.keys},
            "volatile": sorted({v for s in pack.sections for v in s.volatile}),
            "sections": {s.name: {"render": s.render, "title": s.title, "columns": s.columns or None} for s in pack.sections},
            "signals": [], "truncated": {},
        }
        state = None
        try:
            state = self.open(source, ctx, pack, params)
        except (CollectorError, PackError) as ex:
            return {"data": {}, "errors": [str(ex)]}
        try:
            for sec in pack.sections:
                items: list[str | None] = [None]
                if sec.for_each:
                    items = list(params.get(sec.for_each) or [])
                rows: list[dict[str, Any]] = []
                failed = 0
                for item in items:
                    tag = f"{sec.name}@{item}" if item is not None else sec.name
                    try:
                        extra = {sec.item_as: item} if item is not None and sec.item_as else None
                        part = shape_rows(self.run_section(state, source, ctx, pack, sec, item, params, now), sec, extra)
                    except (CollectorError, PackError) as ex:
                        errors.append(f"{tag}: {ex}")
                        failed += 1
                        continue
                    rows += part
                if items and failed == len(items):
                    continue  # nothing usable: the section is absent from data, the error is recorded
                meta["signals"] += eval_signals(sec, rows, params, now, errors)
                if len(rows) > sec.limit:
                    meta["truncated"][sec.name] = len(rows)
                data[sec.name] = rows[: sec.limit]
        finally:
            self.close(state)
        data["meta"] = meta
        return {"data": data, "errors": errors}
