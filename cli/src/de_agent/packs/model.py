"""Pack schema (pydantic, strict: unknown keys are errors) and parsing."""
from __future__ import annotations

import re
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from de_agent.packs.errors import PackError

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
SECTION_RE = re.compile(r"^[a-z][a-z0-9_]*$")
KINDS = ("sql", "cli", "http")
DIALECTS = ("postgres", "redshift", "mysql", "snowflake", "bigquery", "tsql", "databricks")
EXTRAS = ("pg", "mysql", "snowflake", "bigquery", "databricks", "mssql", "aws", "terraform")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Requires(_Strict):
    extra: str | None = Field(None, description="pip extra that installs the driver (`de-agent[<extra>]`).")
    cli: str | None = Field(None, description="Executable that must be on PATH.")

    @field_validator("extra")
    @classmethod
    def _extra(cls, v: str | None) -> str | None:
        if v is not None and v not in EXTRAS:
            raise ValueError(f"unknown extra {v!r}; known: {', '.join(EXTRAS)}")
        return v


class ParamSpec(_Strict):
    type: Literal["str", "ident", "int", "bool", "list"] = "str"
    default: Any = None
    required: bool = False
    description: str = ""


class Signal(_Strict):
    rows: str | None = Field(None, description="Aggregate rule over the section (`count > 0`).")
    when: str | None = Field(None, description="Per-row rule (`error_message contains 'quota'`).")
    message: str
    severity: Literal["info", "warn", "error"] = "warn"

    @model_validator(mode="after")
    def _one(self) -> Signal:
        if (self.rows is None) == (self.when is None):
            raise ValueError("a signal needs exactly one of `rows:` and `when:`")
        return self


class Section(_Strict):
    name: str
    title: str | None = None
    sql: str | None = None
    run: list[str] | None = None
    get: str | None = None
    graphql: str | None = None
    path: str | None = Field(None, description="http graphql: endpoint path (default /graphql).")
    select: str | None = None
    fields: dict[str, str] | None = None
    key: list[str] | str | None = None
    columns: list[str] | None = None
    signals: list[Signal] = Field(default_factory=list)
    render: Literal["table", "list"] = "table"
    volatile: list[str] = Field(default_factory=list)
    limit: int = Field(200, ge=1, le=1000)
    for_each: str | None = None
    item_as: str | None = Field(None, description="Row field that receives the for_each value (default: the value is not added).")

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not SECTION_RE.match(v):
            raise ValueError("section name must match [a-z][a-z0-9_]*")
        if v == "meta":
            raise ValueError("`meta` is reserved")
        return v

    @model_validator(mode="after")
    def _shape(self) -> Section:
        present = [k for k in ("sql", "run", "get", "graphql") if getattr(self, k) is not None]
        if len(present) != 1:
            raise ValueError("a section needs exactly one of sql / run / get / graphql")
        if isinstance(self.key, str):
            self.key = [self.key]
        return self

    @property
    def keys(self) -> list[str]:
        return list(self.key or [])


class Pack(_Strict):
    pack: str
    version: int = Field(1, ge=1)
    kind: Literal["sql", "cli", "http"]
    dialect: str | None = None
    requires: Requires = Field(default_factory=Requires)
    validated: Literal["fixtures", "live"] = "fixtures"
    description: str = ""
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    sections: list[Section] = Field(min_length=1)

    @field_validator("pack")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("pack name must match [a-z0-9][a-z0-9_-]*")
        return v

    @model_validator(mode="after")
    def _kind(self) -> Pack:
        if self.kind == "sql":
            if self.dialect not in DIALECTS:
                raise ValueError(f"sql packs need `dialect:` one of {', '.join(DIALECTS)}")
        elif self.dialect is not None:
            raise ValueError("`dialect:` is for sql packs only")
        names = [s.name for s in self.sections]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise ValueError(f"duplicate section names: {', '.join(dup)}")
        for pname in self.params:
            if not re.match(r"^[a-z][a-z0-9_]*$", pname):
                raise ValueError(f"param name {pname!r} must match [a-z][a-z0-9_]*")
        return self


MAX_PACK_BYTES = 256 * 1024


def parse_pack(text: str) -> Pack:
    if len(text.encode("utf-8", "replace")) > MAX_PACK_BYTES:
        raise PackError(f"a pack file is limited to {MAX_PACK_BYTES // 1024} KB")
    try:
        for ev in yaml.parse(text):  # anchors/aliases allow exponential expansion ("billion laughs"): not needed in a pack
            if isinstance(ev, yaml.AliasEvent) or getattr(ev, "anchor", None):
                raise PackError("YAML anchors and aliases are not allowed in packs")
        raw = yaml.safe_load(text)
    except yaml.YAMLError as ex:
        raise PackError(f"invalid YAML: {str(ex).splitlines()[0] if str(ex) else 'parse error'}") from None
    if not isinstance(raw, dict):
        raise PackError("a pack file must be a YAML mapping")
    try:
        return Pack.model_validate(raw)
    except ValidationError as ex:
        msgs = []
        for e in ex.errors():
            loc = ".".join(str(x) for x in e["loc"])
            msgs.append(f"{loc}: {e['msg'].removeprefix('Value error, ')}" if loc else e["msg"].removeprefix("Value error, "))
        raise PackError("; ".join(msgs)) from None
