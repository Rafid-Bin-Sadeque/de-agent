"""de-agent.yaml: models, loading and secret references (M1a section 2).

The config holds secret REFERENCES only. A literal value in a secret field is a load error.
`resolve_secret` is called at run time only; the value is never logged, rendered or snapshotted.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    ValidationError,
    field_validator,
    model_validator,
)

NAME_RE = re.compile(r"^[a-z0-9_-]+$")
SSH_RE = re.compile(r"^ssh:[A-Za-z0-9_][A-Za-z0-9_.@-]*$")
ENV_RE = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$")
SECRET_FIELD_SUFFIXES = ("_secret", "_key")
SECRET_FIELDS = {"password", "token"}
LITERAL_MSG = "secret literal in config; use env:/pgpass/file:"


class ConfigError(Exception):
    """Invalid or unusable configuration."""


def is_secret_field(name: str) -> bool:
    return name in SECRET_FIELDS or name.endswith(SECRET_FIELD_SUFFIXES)


def validate_secret_ref(ref: Any, field: str = "secret") -> str:
    """Return the ref when it is a valid reference; raise ValueError otherwise."""
    if not isinstance(ref, str):
        raise ValueError(LITERAL_MSG)
    if ref in ("pgpass", "none") or ENV_RE.match(ref):
        return ref
    if ref.startswith("file:"):
        path = ref[5:]
        if not os.path.isabs(path):
            raise ValueError(f"{field}: file: ref must be an absolute path")
        p = Path(path)
        if p.exists():
            mode = stat.S_IMODE(p.stat().st_mode)
            if mode & 0o077:
                raise ValueError(
                    f"{field}: secret file {path} has mode {oct(mode)}; it must be 0600 or stricter"
                )
        return ref
    raise ValueError(LITERAL_MSG)


def _dotenv_lookup(name: str) -> str | None:
    """`name` from a `.env` file in the hub directory, if there is one. dea's own read: never printed, never given to the agent."""
    try:
        from de_agent.hubpath import find_hub

        hub = find_hub()
        text = (hub / ".env").read_text(encoding="utf-8") if hub else ""
    except Exception:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, val = line.partition("=")
        if not sep or key.strip() != name:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            return val[1:-1]
        return val.split(" #")[0].rstrip()
    return None


def resolve_secret(ref: str) -> str | None:
    """Resolve a reference to its value at run time. `pgpass`/`none` resolve to None (libpq decides)."""
    if ref in ("pgpass", "none"):
        return None
    if ref.startswith("env:"):
        name = ref[4:]
        val = os.environ.get(name)
        if val is None:
            val = _dotenv_lookup(name)
        if val is None:
            raise ConfigError(f"environment variable {name} is not set (and not in the hub's .env)")
        return val
    if ref.startswith("file:"):
        path = Path(ref[5:])
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                raise ConfigError(f"secret file {path} must be mode 0600 or stricter")
            return path.read_text().strip()
        except OSError as ex:
            raise ConfigError(f"cannot read secret file {path}: {ex.strerror}") from None
    raise ConfigError(LITERAL_MSG)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HubConfig(_Strict):
    name: str = Field("data platform", description="Platform name, shown in the generated map.")
    map_budget_tokens: int = Field(1500, gt=0, description="Token budget for the generated map.md (the file loaded every session).")
    snapshot_max_age_hours: float = Field(24, gt=0, description="A snapshot older than this is flagged as stale at session start.")
    retention: int = Field(30, ge=1, description="Number of dated snapshots kept per source.")


class GuardConfig(_Strict):
    mode: str = Field("strict", description="`strict` denies a database write outside an approved plan; `ask` prompts instead of denying.")
    cli_allow: list[str] = Field(default_factory=list, description="Exact command prefixes the CLI write guard lets through, e.g. `dbt run --select staging` (word-for-word prefix of the command, after wrappers). A human edits this; agents cannot.")

    @field_validator("cli_allow")
    @classmethod
    def _cli_allow(cls, v: list[str]) -> list[str]:
        import shlex

        for entry in v:
            try:
                words = shlex.split(entry)
            except ValueError:
                raise ValueError(f"cli_allow entry {entry!r} is not a valid command prefix") from None
            if len(words) < 2:
                raise ValueError(f"cli_allow entry {entry!r} must be the CLI and a verb, e.g. `dbt run`")
            if re.search(r"[;&|<>`$()\n\\]", entry):
                raise ValueError(f"cli_allow entry {entry!r} must be one plain command prefix (no shell operators)")
        return v

    @field_validator("mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v not in ("strict", "ask"):
            raise ValueError("guard.mode must be strict or ask")
        return v


class ConnConfig(_Strict):
    host: str = Field("localhost", description="Database host (as seen from where the collector runs).")
    port: int = Field(5432, description="Database port.")
    dbname: str = Field(description="Database name.")
    user: str = Field(description="Role to connect as. Use a read-only role.")
    password: str = Field("pgpass", description="Secret reference: `pgpass`, `none`, `env:NAME` or `file:/abs/path` (mode 0600). A literal is a load error.")
    sslmode: str = Field("prefer", description="libpq sslmode: disable, allow, prefer, require, verify-ca or verify-full.")

    @field_validator("password")
    @classmethod
    def _password(cls, v: str) -> str:
        return validate_secret_ref(v, "password")

    @field_validator("sslmode")
    @classmethod
    def _ssl(cls, v: str) -> str:
        if v not in ("disable", "allow", "prefer", "require", "verify-ca", "verify-full"):
            raise ValueError(f"unsupported sslmode {v!r}")
        return v


class PackConn(_Strict):
    """Connection for `type: sql` (pack) sources: a SQLAlchemy URL WITHOUT a password, plus a secret reference."""

    url: str = Field(description="SQLAlchemy URL without a password, e.g. `mysql+pymysql://reader@db.example.internal:3306/shop` (driver from the pack's extra).")
    password: str = Field("none", description="Secret reference for the URL's password: `none`, `env:NAME` or `file:/abs/path` (mode 0600). A literal is a load error; so is a password inside the URL.")

    @field_validator("password")
    @classmethod
    def _password(cls, v: str) -> str:
        return validate_secret_ref(v, "password")

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not re.match(r"^[a-z][a-z0-9+.-]*://", v):
            raise ValueError("url must look like driver://user@host/db")
        if re.match(r"^[a-z][a-z0-9+.-]*://[^/@?]*:[^/@?]+@", v) or re.search(r"[?&](password|pwd|token|secret)=", v, re.I):
            raise ValueError("a password or token inside the url is not allowed: put it in `password:` as a secret reference")
        return v


def _conn_kind(v: Any) -> str:
    return "pack" if (isinstance(v, PackConn) or (isinstance(v, dict) and "url" in v)) else "db"


AnyConn = Annotated[
    Annotated[ConnConfig, Tag("db")] | Annotated[PackConn, Tag("pack")], Discriminator(_conn_kind)]

DEFAULT_TIMEOUT = 120
REDSHIFT_TIMEOUT_DEFAULT = 600
PACK_TYPES = ("sql", "cli", "http")


class SourceConfig(_Strict):
    name: str = Field(description="Source name, `[a-z0-9_-]+`; used in file names and `dea query <source>`.")
    type: str = Field(description="Collector type: postgres, redshift, airflow, dbt or aws (see docs/collectors.md), or sql, cli or http to run a pack (see docs/packs.md).")
    exec: str = Field("local", description="`local`, or `ssh:<host-alias>` to run the collector on another host over ssh.")
    timeout_seconds: int | None = Field(None, ge=1, description="Per-collector timeout. Unset: 600 for redshift, 120 otherwise.")
    conn: AnyConn | None = Field(None, description="Database connection: host/dbname/user for postgres and redshift sources; `{url, password}` for `type: sql` pack sources.")
    pack: str | None = Field(None, description="`type: sql|cli|http` only: the pack to run (`dea pack list`). Its params go in `options:`.")
    base_url: str | None = Field(None, description="`type: http` only: base URL of the API, as the machine that runs the request sees it.")
    auth: str | None = Field(None, description="`type: http` only: `none`, `bearer`, `basic` or `header`.")
    user: str | None = Field(None, description="`type: http` with `auth: basic`: the user name (not a secret).")
    token: str | None = Field(None, description="`type: http` with `auth: bearer|header`: secret reference.")
    password: str | None = Field(None, description="`type: http` with `auth: basic`: secret reference.")
    header_name: str | None = Field(None, description="`type: http` with `auth: header`: header that carries the token, e.g. `X-Api-Key`.")
    data_api: dict[str, Any] | None = Field(None, description="redshift only: Data API target `{cluster_id|workgroup, database, db_user|secret_arn}`.")
    aws: dict[str, Any] | None = Field(None, description="aws and redshift: `{profile, region}` or `{instance_role: true}`.")
    services: dict[str, Any] = Field(default_factory=dict, description="aws only: which services to read (dms, glue, s3, athena, firehose, cloudwatch) and their options.")
    writable: bool = Field(False, description="postgres only: the role may write. Used by `dea apply` for approved plans, never snapshotted.")
    options: dict[str, Any] = Field(default_factory=dict, description="Collector-specific options (airflow, dbt, aws; see docs/collectors.md), or the pack's params for sql, cli and http sources.")

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("source name must match [a-z0-9_-]+")
        return v

    @field_validator("type")
    @classmethod
    def _type(cls, v: str) -> str:
        from de_agent.collectors import registered_types

        types = registered_types()
        if v not in types:
            raise ValueError(f"unknown source type {v!r}; registered types: {', '.join(types)}. "
                             "For a system with a pack use type: sql|cli|http plus pack: <name> (see `dea pack list`)")
        return v

    @field_validator("token", "password")
    @classmethod
    def _secret(cls, v: str | None, info: Any) -> str | None:
        return None if v is None else validate_secret_ref(v, info.field_name)

    @field_validator("exec")
    @classmethod
    def _exec(cls, v: str) -> str:
        if v != "local" and not SSH_RE.match(v):
            raise ValueError("exec must be 'local' or 'ssh:<host-alias>'")
        return v

    @model_validator(mode="after")
    def _ssh_secret(self) -> SourceConfig:
        if isinstance(self.conn, ConnConfig) and self.exec.startswith("ssh:") and self.conn.password != "pgpass":
            raise ValueError(
                "exec ssh:<host> requires password: pgpass (resolved on the remote host); "
                f"got {self.conn.password.split(':')[0]!r}"
            )
        return self

    @model_validator(mode="after")
    def _conn_matches_type(self) -> SourceConfig:
        if self.type in PACK_TYPES:
            if self.conn is not None and not isinstance(self.conn, PackConn):
                raise ValueError("sql sources take `conn: {url, password}`")
        elif isinstance(self.conn, PackConn):
            raise ValueError(f"{self.type} sources take conn: {{host, port, dbname, user, password}}, not a url")
        if self.type not in PACK_TYPES and any(
                getattr(self, k) is not None for k in ("pack", "base_url", "auth", "user", "token", "password", "header_name")):
            raise ValueError("pack, base_url, auth, user, token, password and header_name are for type sql|cli|http sources")
        return self

    @model_validator(mode="after")
    def _writable(self) -> SourceConfig:
        if self.writable and self.type != "postgres":
            raise ValueError("writable: true is only supported on postgres sources")
        return self

    @model_validator(mode="after")
    def _default_timeout(self) -> SourceConfig:
        if self.timeout_seconds is None:
            self.timeout_seconds = REDSHIFT_TIMEOUT_DEFAULT if self.type == "redshift" else DEFAULT_TIMEOUT
        return self

    @model_validator(mode="after")
    def _options(self) -> SourceConfig:
        from de_agent.collectors import get_collector

        collector = get_collector(self.type)
        validate = getattr(collector, "validate_options", None)
        if validate:
            validate(self.options)
        validate_source = getattr(collector, "validate_source", None)
        if validate_source:
            validate_source(self)
        return self


SCAN_TYPES = ("airflow", "dbt", "sql", "python", "terraform")
PYTHON_GLOBS = ["**/*.py", "**/*.ipynb"]


class ScanConfig(_Strict):
    """One static scan (M1c). Paths are relative to the hub. Scanned code is parsed, never imported or run."""

    name: str = Field(description="Scan name, `[a-z0-9_-]+`.")
    type: str = Field(description="Scanner type: airflow, dbt, sql, python (scripts, notebooks, pyspark) or terraform.")
    path: str = Field(description="Directory to scan, relative to the hub. Parsed, never imported or run.")
    project: str | None = Field(None, description="Project name the catalog entries belong to. Default: the scan name.")
    dag_folders: list[str] = Field(default_factory=lambda: ["dags"], description="airflow: DAG folders under `path`.")
    util_packages: list[str] = Field(default_factory=list, description="airflow: shared packages whose functions are resolved to their callers.")
    dataset_tables: list[str] = Field(default_factory=list, description="airflow: Dataset URI patterns, e.g. `warehouse://{schema}/{table}` (must contain `{table}`).")
    target_dir: str = Field("target", description="dbt: directory holding `manifest.json`, relative to `path`.")
    globs: list[str] = Field(default_factory=lambda: ["**/*.sql"], description="sql and python: file globs under `path` (python default: `**/*.py`, `**/*.ipynb`).")
    dialect: str = Field("postgres", description="airflow and python (SQL strings), sql and terraform: sqlglot dialect used to parse SQL.")

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("scan name must match [a-z0-9_-]+")
        return v

    @field_validator("type")
    @classmethod
    def _type(cls, v: str) -> str:
        if v not in SCAN_TYPES:
            raise ValueError(f"unknown scan type {v!r}; supported: {', '.join(SCAN_TYPES)}")
        return v

    @field_validator("dataset_tables")
    @classmethod
    def _patterns(cls, v: list[str]) -> list[str]:
        for pat in v:
            if "{table}" not in pat:
                raise ValueError(f"dataset_tables pattern {pat!r} must contain {{table}} (optional: {{schema}}, {{db}})")
        return v

    @model_validator(mode="after")
    def _python_globs(self) -> ScanConfig:
        if self.type == "python" and "globs" not in self.model_fields_set:
            self.globs = list(PYTHON_GLOBS)
        return self

    @property
    def project_name(self) -> str:
        return self.project or self.name


class EdgesConfig(_Strict):
    ignore_tables: list[str] = Field(default_factory=list, description="Bookkeeping tables (watermarks, load state) that `dea impact` does not traverse through, e.g. `etl.load_state`. Their own edges stay in `dea edges`.")

    @field_validator("ignore_tables")
    @classmethod
    def _ignore(cls, v: list[str]) -> list[str]:
        out = []
        for t in v:
            if not isinstance(t, str) or not t.strip():
                raise ValueError("ignore_tables entries must be non-empty table names")
            out.append(t.strip().lower())
        return out


class Config(_Strict):
    version: int = Field(1, description="Config format version. Only 1 exists.")
    hub: HubConfig = Field(default_factory=HubConfig, description="Hub-wide settings.")
    sources: list[SourceConfig] = Field(default_factory=list, description="Live systems read by `dea snapshot` (read-only collectors).")
    scans: list[ScanConfig] = Field(default_factory=list, description="Code scanned by `dea scan` into the catalog.")
    guard: GuardConfig = Field(default_factory=GuardConfig, description="Write-guard behaviour.")
    edges: EdgesConfig = Field(default_factory=EdgesConfig, description="Lineage graph options.")

    @field_validator("version")
    @classmethod
    def _version(cls, v: int) -> int:
        if v != 1:
            raise ValueError("unsupported config version (expected 1)")
        return v

    @model_validator(mode="after")
    def _unique(self) -> Config:
        names = [s.name for s in self.sources]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise ValueError(f"duplicate source names: {', '.join(dup)}")
        scan_names = [s.name for s in self.scans]
        dup = sorted({n for n in scan_names if scan_names.count(n) > 1})
        if dup:
            raise ValueError(f"duplicate scan names: {', '.join(dup)}")
        return self

    def source(self, name: str) -> SourceConfig:
        for s in self.sources:
            if s.name == name:
                return s
        raise ConfigError(f"no such source: {name}")


def _scan_literal_secrets(node: Any, path: str = "") -> str | None:
    """Reject literals in ANY secret-named key, even in sections the models don't know about."""
    if isinstance(node, dict):
        for k, v in node.items():
            here = f"{path}.{k}" if path else str(k)
            if isinstance(k, str) and is_secret_field(k):
                try:
                    validate_secret_ref(v, here)
                except ValueError as ex:
                    return f"{here}: {ex}"
            else:
                found = _scan_literal_secrets(v, here)
                if found:
                    return found
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found = _scan_literal_secrets(v, f"{path}[{i}]")
            if found:
                return found
    return None


def parse_config(text: str) -> Config:
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as ex:
        raise ConfigError(f"invalid YAML: {ex}") from None
    if not isinstance(raw, dict):
        raise ConfigError("de-agent.yaml must be a mapping")
    bad = _scan_literal_secrets(raw)
    if bad:
        raise ConfigError(bad)
    try:
        return Config.model_validate(raw)
    except ValidationError as ex:
        msgs = []
        for e in ex.errors():
            loc = ".".join(str(x) for x in e["loc"])
            msgs.append(f"{loc}: {e['msg'].removeprefix('Value error, ')}")
        raise ConfigError("; ".join(msgs)) from None


def load_config(path: Path) -> Config:
    from de_agent.packs.loader import set_hub_hint

    set_hub_hint(Path(path).resolve().parent)  # hub-local packs/ resolve relative to the config being loaded
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as ex:
        raise ConfigError(f"cannot read {path}: {ex.strerror}") from None
    return parse_config(text)
