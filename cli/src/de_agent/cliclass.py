"""Classify cloud / data CLI command lines as read, write or unknown (v0.2 section 3). Stdlib only, Python 3.9 compatible.

The hook `scripts/cli_guard.py` and the pack runner (`type: cli`) share this module, so one table decides what a "read-only
command" means. `classify_command` takes a shell command line, follows wrappers (`env`, `sudo`, `bash -c`, `xargs`,
`find -exec`, `timeout`, `nice`, `uv run`, `poetry run`, `docker exec`, `docker compose exec`, `kubectl exec --`, `ssh`, ...)
and returns one `Verdict` per guarded CLI invocation found. Kinds:

  read     the command only reads
  write    a known mutating command (denied outside `dea apply`)
  unknown  a verb of a known CLI that is not on its read list (denied, fail closed)
  sql      carries SQL (`aws redshift-data`, `bq query`, `snow sql`, `psql` inside `kubectl exec`): `sql_guard` classifies it
  allow    matched an entry of `guard.cli_allow` (exact word prefix)

It is a pattern matcher over command text, not a sandbox: variables used as the program name, `eval` of built strings, base64 and
scripts it cannot read are out of scope (docs/SAFETY.md).
"""
from __future__ import annotations

import ast
import os
import re
import shlex
from dataclasses import dataclass

from de_agent.clients import DB_CLIENTS, GUARDED_CLIS

MAX_DEPTH = 4
MAX_SCRIPT = 200_000
PLACEHOLDER = re.compile(r"__DEA_HEREDOC_(\d+)__")
UNPARSEABLE = "de-agent: cannot parse this command line to check the CLI calls in it; split it into simple commands"
PIPED_SHELL = ("de-agent: shell code piped on stdin cannot be checked for CLI writes; write the commands out, or put them "
               "in a plan as a shell step")
UNKNOWN_MSG = "not a recognised read-only command; put it in a plan as a shell step"


@dataclass(frozen=True)
class Verdict:
    kind: str   # read | write | unknown | sql | allow | unparseable
    cli: str
    verb: str
    reason: str = ""

    @property
    def denied(self) -> bool:
        return self.kind in ("write", "unknown", "unparseable")

    def message(self) -> str:
        label = f"{self.cli} {self.verb}".strip()
        if self.kind == "write":
            return (f"de-agent: `{label}` is a write; CLI writes run only as an approved plan shell step "
                    f"(put it in a plan; `dea apply` runs it after human approval){': ' + self.reason if self.reason else ''}")
        if self.kind == "unknown":
            return f"de-agent: `{label}`: {UNKNOWN_MSG}{' (' + self.reason + ')' if self.reason else ''}"
        return self.reason or UNPARSEABLE


def _r(cli: str, verb: str = "") -> Verdict:
    return Verdict("read", cli, verb)


def _w(cli: str, verb: str, why: str = "") -> Verdict:
    return Verdict("write", cli, verb, why)


def _u(cli: str, verb: str, why: str = "") -> Verdict:
    return Verdict("unknown", cli, verb, why)


def _s(cli: str, verb: str) -> Verdict:
    return Verdict("sql", cli, verb)


# ---------------------------------------------------------------- argument helpers

def scan(args: list[str], valued: frozenset[str] | set[str] = frozenset()) -> tuple[list[str], list[str]]:
    """(positionals, flags). A flag listed in `valued` (as `--name` or `-n`) consumes the next word unless written `--name=value`."""
    pos: list[str] = []
    flags: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos.extend(args[i + 1:])
            break
        if a.startswith("--") or (a.startswith("-") and len(a) > 1 and not a[1:].isdigit()):
            flags.append(a)
            if "=" not in a and a in valued and i + 1 < len(args):
                i += 1
        else:
            pos.append(a)
        i += 1
    return pos, flags


def _has_help(args: list[str]) -> bool:
    return "--help" in args


def _prefix(t: str, prefixes: tuple[str, ...]) -> bool:
    return t.startswith(prefixes)


def _base(w: str) -> str:
    return os.path.basename(w)


def _path_verb(pos: list[str], reads: set[str], writes: set[str], rp: tuple[str, ...], wp: tuple[str, ...],
               start: int = 1, limit: int = 8) -> tuple[str, str] | None:
    """First token of the command path (after the service group) that is a known read or write verb."""
    for i in range(start, min(len(pos), limit)):
        t = pos[i]
        if t in writes or (wp and t.startswith(wp)):
            return "write", t
        if t in reads or (rp and t.startswith(rp)):
            return "read", t
    return None


def _label(parts: list[str], n: int = 4) -> str:
    return " ".join(parts[:n])


# ---------------------------------------------------------------- aws

_AWS_VALUED = {"--profile", "--region", "--output", "--endpoint-url", "--query", "--ca-bundle", "--cli-read-timeout",
               "--cli-connect-timeout", "--color", "--cli-binary-format", "--cli-input-json", "--cli-input-yaml",
               "--cli-auth-type"}
_S3_VALUED = _AWS_VALUED | {"--exclude", "--include", "--acl", "--storage-class", "--sse", "--sse-kms-key-id", "--sse-c",
                            "--sse-c-key", "--sse-c-copy-source", "--sse-c-copy-source-key", "--content-type", "--cache-control",
                            "--expires", "--metadata", "--metadata-directive", "--grants", "--website-redirect",
                            "--page-size", "--request-payer", "--source-region", "--expected-size", "--content-disposition",
                            "--content-encoding", "--content-language", "--copy-props", "--checksum-algorithm",
                            "--checksum-mode", "--bucket", "--key"}
_AWS_READ_PREFIX = ("describe-", "list-", "get-", "head-", "batch-get-", "lookup-")
_AWS_WRITE_PREFIX = ("start-", "stop-", "create-", "delete-", "update-", "put-", "modify-", "run-", "terminate-", "reboot-")
_AWS_EXTRA_READ = {("logs", "tail"), ("logs", "filter-log-events"), ("dynamodb", "query"), ("dynamodb", "scan"),
                   ("s3", "presign"), ("cloudformation", "validate-template"), ("sts", "decode-authorization-message")}


def _aws(args: list[str]) -> Verdict:
    pos, flags = scan(args, _S3_VALUED)
    if not pos:
        return _r("aws")
    svc = pos[0]
    if svc in ("help", "version"):
        return _r("aws", svc)
    if len(pos) == 1 or _has_help(args) or pos[1] == "help":
        return _r("aws", svc)
    op = pos[1]
    if svc == "configure":
        return _r("aws", f"configure {op}") if op in ("list", "get", "list-profiles") else _u("aws", f"configure {op}")
    if svc == "redshift-data" and op in ("execute-statement", "batch-execute-statement"):
        return _s("aws", f"{svc} {op}")
    if svc == "athena" and op == "start-query-execution":
        return _s("aws", f"{svc} {op}")
    if svc == "s3":
        return _aws_s3(op, pos)
    label = f"{svc} {op}"
    if op == "wait" or op.startswith(_AWS_READ_PREFIX) or (svc, op) in _AWS_EXTRA_READ:
        return _r("aws", label)
    if op.startswith(_AWS_WRITE_PREFIX) or op == "invoke":
        return _w("aws", label)
    return _u("aws", label)


def _aws_s3(op: str, pos: list[str]) -> Verdict:
    label = f"s3 {op}"
    if op in ("ls", "presign"):
        return _r("aws", label)
    if op in ("cp", "sync"):
        paths = pos[2:]
        if len(paths) < 2:
            return _u("aws", label, "needs a source and a destination")
        src, dst = paths[0], paths[1]
        if dst.startswith("s3://"):
            return _w("aws", label, "destination is s3://")
        if src.startswith("s3://") or src == "-":
            return _r("aws", label) if src.startswith("s3://") else _u("aws", label, "local to local")
        return _u("aws", label, "neither side is s3://")
    if op in ("mv", "rm", "mb", "rb", "website"):
        return _w("aws", label)
    return _u("aws", label)


# ---------------------------------------------------------------- gcloud / gsutil / bq

_GCLOUD_VALUED = {"--project", "--account", "--billing-project", "--configuration", "--format", "--filter", "--flags-file",
                  "--impersonate-service-account", "--verbosity", "--zone", "--region", "--location", "--limit", "--sort-by",
                  "--page-size"}
_GCLOUD_READS = {"list", "describe", "read", "tail", "ls", "cat", "du", "hash", "show"}
_GCLOUD_WRITES = {"create", "delete", "update", "deploy", "run", "submit", "import", "start", "stop", "restart", "resume",
                  "suspend", "pause", "cancel", "enable", "disable", "reset", "resize", "trigger", "publish", "execute",
                  "copy", "move", "rename", "attach", "detach", "apply", "replace", "restore", "undelete", "upgrade", "promote",
                  "rollback", "patch", "cp", "mv", "rm", "rsync", "ssh", "scp", "login", "revoke", "add", "remove", "call", "pull", "invoke",
                  "export", "clone", "ack", "bind"}
_GCLOUD_RP = ("list-", "describe-", "get-", "show-")
_GCLOUD_WP = ("set-", "add-", "remove-", "delete-", "create-", "update-", "enable-", "disable-")


_SCHEME = re.compile(r"^(?!file:)[A-Za-z][A-Za-z0-9+.-]*://")


def _gs(p: str) -> bool:
    return p.startswith("gs://")


def _remote(p: str) -> bool:
    return bool(_SCHEME.match(p))


def _gcloud(args: list[str]) -> Verdict:
    pos, flags = scan(args, _GCLOUD_VALUED)
    while pos and pos[0] in ("alpha", "beta", "preview"):
        pos = pos[1:]
    if not pos or _has_help(args):
        return _r("gcloud")
    if pos[0] in ("version", "info", "help", "topic", "feedback") or (pos[0] == "config" and len(pos) > 1 and pos[1] in (
            "list", "get", "get-value", "configurations")):
        return _r("gcloud", _label(pos, 2))
    if pos[0] == "auth" and len(pos) > 1 and pos[1] == "list":
        return _r("gcloud", "auth list")
    if pos[0] == "storage" and len(pos) > 1:
        sub = pos[1]
        if sub in ("ls", "cat", "du", "hash"):
            return _r("gcloud", f"storage {sub}")
        if sub in ("cp", "rsync"):
            return _w("gcloud", f"storage {sub}", "destination is not local") if len(pos) > 2 and _remote(pos[-1]) else (
                _r("gcloud", f"storage {sub}") if len(pos) > 3 and _gs(pos[2]) else _u("gcloud", f"storage {sub}", "neither side is gs://"))
        if sub in ("mv", "rm"):
            return _w("gcloud", f"storage {sub}")
    hit = _path_verb(pos, _GCLOUD_READS, _GCLOUD_WRITES, _GCLOUD_RP, _GCLOUD_WP)
    label = _label(pos)
    if hit is None:
        return _u("gcloud", label)
    return _r("gcloud", label) if hit[0] == "read" else _w("gcloud", label)


_GSUTIL_VALUED = {"-o", "-h", "-u", "-i"}
_GSUTIL_SUBVERB_GROUPS = {"acl", "iam", "notification", "label", "lifecycle", "cors", "defacl", "versioning", "web", "retention",
                          "logging", "requesterpays", "kms", "hmac", "autoclass", "bucketpolicyonly", "ubla", "pap", "rpo", "ls"}


def _gsutil(args: list[str]) -> Verdict:
    pos, flags = scan(args, _GSUTIL_VALUED)
    if not pos or _has_help(args):
        return _r("gsutil")
    v = pos[0]
    if v in ("ls", "cat", "du", "stat", "hash", "version", "help", "lsattr"):
        return _r("gsutil", v)
    if v in ("cp", "rsync"):
        if len(pos) < 3:
            return _u("gsutil", v, "needs a source and a destination")
        return _w("gsutil", v, "destination is not local") if _remote(pos[-1]) else (
            _r("gsutil", v) if _gs(pos[1]) else _u("gsutil", v, "neither side is gs://"))
    if v in ("mv", "rm", "mb", "rb", "setmeta", "rewrite", "compose", "update", "signurl"):
        return _w("gsutil", v)
    if v in _GSUTIL_SUBVERB_GROUPS:
        if len(pos) > 1 and pos[1] in ("get", "list"):
            return _r("gsutil", f"{v} {pos[1]}")
        return _w("gsutil", v + (f" {pos[1]}" if len(pos) > 1 else ""))
    return _u("gsutil", v)


_BQ_VALUED = {"--project_id", "--dataset_id", "--location", "--format", "--api", "--api_version", "--job_id", "--max_rows",
              "--bigquery_discovery_api_key", "--discovery_file", "--bq_discovery_api_key", "--service_account",
              "--service_account_credential_file", "--credential_file", "--proxy_address", "--destination_table", "--schedule",
              "--target_dataset", "--destination_schema", "--destination_kms_key", "--flagfile"}
_BQ_WRITE_FLAGS = {"--destination_table", "--append_table", "--replace", "--schedule", "--target_dataset", "--destination_schema",
                   "--destination_kms_key"}


def _bq(args: list[str]) -> Verdict:
    pos, flags = scan(args, _BQ_VALUED)
    if not pos or _has_help(args):
        return _r("bq")
    v = pos[0]
    names = {f.split("=", 1)[0] for f in flags}
    if "--flagfile" in names:
        return _w("bq", v, "--flagfile can carry any flag")
    if v == "query":
        return _w("bq", v, "writes to a table or schedules the query") if names & _BQ_WRITE_FLAGS else _s("bq", v)
    if v in ("ls", "show", "head", "version", "help", "wait", "info"):
        return _r("bq", v)
    if v in ("mk", "rm", "cp", "load", "insert", "update", "extract", "cancel", "partition", "undelete", "truncate", "add-iam-policy-binding",
             "remove-iam-policy-binding", "set-iam-policy"):
        return _w("bq", v)
    return _u("bq", v)


# ---------------------------------------------------------------- databricks

_DBX_VALUED = {"-p", "--profile", "-o", "--output", "--log-level", "--log-file", "--log-format", "-t", "--target",
               "--progress-format"}
_DBX_WRITE_SUB = {"create", "delete", "update", "run-now", "submit", "reset", "cancel", "start", "stop", "restart", "terminate",
                  "edit", "resize", "pin", "unpin", "import", "put", "set", "patch", "deploy", "destroy", "run", "trigger",
                  "permanent-delete", "repair", "install", "uninstall", "set-permissions", "update-permissions", "execute",
                  "mkdirs", "mkdir", "move", "rename", "migrate", "publish", "unpublish", "enable", "disable", "grant", "revoke"}


def _dbx_dbfs(p: str) -> bool:
    return p.startswith(("dbfs:", "/Volumes", "/Workspace"))


def _databricks(args: list[str]) -> Verdict:
    pos, flags = scan(args, _DBX_VALUED)
    if not pos or _has_help(args):
        return _r("databricks")
    grp = pos[0]
    if grp in ("version", "help", "completion"):
        return _r("databricks", grp)
    if grp == "sql":
        return _s("databricks", "sql")
    if len(pos) < 2:
        return _r("databricks", grp)
    sub = pos[1]
    label = f"{grp} {sub}"
    if grp == "fs":
        if sub in ("ls", "cat"):
            return _r("databricks", label)
        if sub == "cp":
            return _w("databricks", label, "destination is dbfs") if len(pos) > 3 and _dbx_dbfs(pos[-1]) else _r("databricks", label)
        return _w("databricks", label)
    if grp == "api":
        return _r("databricks", label) if sub in ("get", "head") else _w("databricks", label)
    if grp == "auth":
        return _r("databricks", label) if sub in ("profiles", "describe", "env") else _u("databricks", label)
    if grp == "bundle":
        if sub in ("validate", "summary", "schema", "generate", "init", "open"):
            return _r("databricks", label) if sub in ("validate", "summary", "schema") else _u("databricks", label)
        return _w("databricks", label) if sub in ("deploy", "run", "destroy") else _u("databricks", label)
    if sub.startswith(("list", "get", "describe", "export", "show", "search")) or sub in ("me", "ls", "events", "export-dir", "wait"):
        return _r("databricks", label)
    if sub in _DBX_WRITE_SUB or sub.startswith(("cancel", "create-", "delete-", "update-", "set-")):
        return _w("databricks", label)
    return _u("databricks", label)


def _dbsqlcli(args: list[str]) -> Verdict:
    return _s("dbsqlcli", "")


# ---------------------------------------------------------------- kubectl / helm

_KUBECTL_VALUED = {"-n", "--namespace", "--context", "--cluster", "--user", "--kubeconfig", "-s", "--server", "--token", "--as",
                   "--as-group", "--request-timeout", "--cache-dir", "--certificate-authority", "--client-certificate",
                   "--client-key", "-v", "--v", "--tls-server-name", "--username", "--password", "--profile", "--profile-output",
                   "--as-uid"}
_KUBECTL_EXEC_VALUED = _KUBECTL_VALUED | {"-c", "--container", "-f", "--filename", "--pod-running-timeout"}
_KUBECTL_READ = {"get", "describe", "logs", "top", "explain", "version", "api-resources", "api-versions", "cluster-info", "diff",
                 "events", "wait", "completion", "options", "help"}
_KUBECTL_WRITE = {"apply", "create", "delete", "edit", "patch", "replace", "scale", "cordon", "uncordon", "drain", "taint", "label",
                  "annotate", "set", "port-forward", "expose", "run", "autoscale", "certificate", "debug", "attach", "proxy",
                  "kustomize"}


def _kubectl(args: list[str]) -> Verdict:
    pos, flags = scan(args, _KUBECTL_VALUED)
    if not pos or _has_help(args):
        return _r("kubectl")
    v = pos[0]
    if v in _KUBECTL_READ:
        return _r("kubectl", v)
    if v == "config":
        sub = pos[1] if len(pos) > 1 else ""
        return _r("kubectl", f"config {sub}") if sub in ("view", "get-contexts", "current-context", "get-clusters", "get-users") or not sub else _u(
            "kubectl", f"config {sub}")
    if v == "auth":
        sub = pos[1] if len(pos) > 1 else ""
        return _r("kubectl", f"auth {sub}") if sub in ("can-i", "whoami") else _u("kubectl", f"auth {sub}")
    if v == "rollout":
        sub = pos[1] if len(pos) > 1 else ""
        if sub in ("status", "history"):
            return _r("kubectl", f"rollout {sub}")
        return _w("kubectl", f"rollout {sub}") if sub in ("restart", "undo", "pause", "resume") else _u("kubectl", f"rollout {sub}")
    if v == "cp":
        dests = pos[1:]
        if len(dests) >= 2 and ":" in dests[-1] and not re.match(r"^[A-Za-z]:[\\/]", dests[-1]):
            return _w("kubectl", "cp", "copies into a pod")
        return _r("kubectl", "cp") if len(dests) >= 2 else _u("kubectl", "cp")
    if v in _KUBECTL_WRITE:
        return _w("kubectl", v)
    if v == "exec":
        return _u("kubectl", "exec", "unreachable: handled by the wrapper logic")
    return _u("kubectl", v)


_HELM_VALUED = {"-n", "--namespace", "--kube-context", "--kubeconfig", "--burst-limit", "--kube-apiserver", "--kube-token",
                "--kube-as-user", "--kube-as-group", "--kube-ca-file", "--registry-config", "--repository-cache",
                "--repository-config"}


def _helm(args: list[str]) -> Verdict:
    pos, flags = scan(args, _HELM_VALUED)
    if not pos or _has_help(args):
        return _r("helm")
    v = pos[0]
    sub = pos[1] if len(pos) > 1 else ""
    if v in ("list", "ls", "status", "get", "history", "show", "inspect", "template", "version", "lint", "search", "env", "help",
             "completion", "verify"):
        return _r("helm", v)
    if v == "repo":
        return _r("helm", "repo list") if sub in ("list", "") else _u("helm", f"repo {sub}")
    if v in ("install", "upgrade", "uninstall", "delete", "rollback"):
        return _w("helm", v)
    return _u("helm", v)


# ---------------------------------------------------------------- terraform / pulumi

_TF_VALUED: set[str] = set()


def _terraform(args: list[str], name: str = "terraform") -> Verdict:
    pos = [a for a in args if not a.startswith("-chdir=")]
    pos, flags = scan(pos)
    if not pos:
        return _r(name)
    if any(f in ("-help", "-h", "--help", "-version", "--version") for f in flags) and len(pos) <= 1:
        return _r(name, pos[0])
    v = pos[0]
    sub = pos[1] if len(pos) > 1 else ""
    if v in ("plan", "show", "validate", "output", "providers", "version", "init", "graph", "get", "help", "metadata", "console"):
        if v == "console":
            return _u(name, v, "interactive")
        if v == "providers" and sub in ("lock", "mirror"):
            return _u(name, f"providers {sub}")
        return _r(name, v)
    if v == "fmt":
        return _r(name, "fmt -check") if any(f in ("-check", "--check") for f in flags) else _w(name, "fmt", "rewrites files")
    if v == "state":
        if sub in ("list", "show", "pull", ""):
            return _r(name, f"state {sub}".strip())
        return _w(name, f"state {sub}") if sub in ("rm", "mv", "push", "replace-provider") else _u(name, f"state {sub}")
    if v == "workspace":
        return _r(name, f"workspace {sub}") if sub in ("list", "show") else _w(name, f"workspace {sub}".strip())
    if v in ("apply", "destroy", "import", "taint", "untaint", "force-unlock", "refresh", "login", "logout", "test"):
        return _w(name, v)
    return _u(name, v)


_PULUMI_VALUED = {"--cwd", "-C", "-s", "--stack", "--color", "--tracing", "--profiling", "-v", "--config-file"}


def _pulumi(args: list[str]) -> Verdict:
    pos, flags = scan(args, _PULUMI_VALUED)
    if not pos or _has_help(args):
        return _r("pulumi")
    v = pos[0]
    sub = pos[1] if len(pos) > 1 else ""
    if v in ("preview", "whoami", "version", "about", "logs", "help", "completion", "gen-completion"):
        return _r("pulumi", v)
    if v == "stack":
        if sub in ("", "ls", "output", "history", "graph", "export", "tag"):
            if sub == "tag" and len(pos) > 2 and pos[2] != "ls" and pos[2] != "get":
                return _w("pulumi", f"stack tag {pos[2]}")
            return _r("pulumi", f"stack {sub}".strip())
        return _w("pulumi", f"stack {sub}")
    if v == "config":
        if sub in ("", "get"):
            return _r("pulumi", f"config {sub}".strip())
        return _w("pulumi", f"config {sub}")
    if v in ("up", "destroy", "refresh", "import", "cancel", "login", "logout", "new", "state", "watch", "convert", "policy"):
        return _w("pulumi", v + (f" {sub}" if sub else ""))
    return _u("pulumi", v)


# ---------------------------------------------------------------- az / snow

_AZ_VALUED = {"--subscription", "-o", "--output", "--query", "-g", "--resource-group", "--name", "-n", "--location", "-l"}
_AZ_READS = {"list", "show", "query", "exists", "wait", "list-locations", "download", "download-batch"}
_AZ_WRITES = {"create", "delete", "update", "set", "start", "stop", "restart", "run", "invoke", "deploy", "add", "remove",
              "import", "scale", "swap", "purge", "restore", "assign", "cancel", "upload", "upload-batch", "login",
              "logout", "move", "regenerate-key", "rotate", "sync", "copy", "attach", "detach", "enable", "disable"}


def _az(args: list[str]) -> Verdict:
    pos, flags = scan(args, _AZ_VALUED)
    if not pos or _has_help(args):
        return _r("az")
    if pos[0] in ("version", "help", "find", "feedback", "interactive") and len(pos) <= 2:
        return _r("az", pos[0])
    if pos[0] == "rest":
        m = None
        for k, f in enumerate(args):
            if f in ("--method", "-m") and k + 1 < len(args):
                m = args[k + 1].lower()
            elif f.startswith("--method="):
                m = f.split("=", 1)[1].lower()
        return _r("az", "rest") if m in (None, "get", "head") else _w("az", f"rest --method {m}")
    hit = _path_verb(pos, _AZ_READS, _AZ_WRITES, ("list-", "show-", "get-", "check-", "download"), ("upload", "create-", "delete-",
                                                                                                  "update-", "set-", "add-", "remove-"))
    label = _label(pos)
    if hit is None:
        return _u("az", label)
    return _r("az", label) if hit[0] == "read" else _w("az", label)


_SNOW_VALUED = {"-c", "--connection", "--format", "--config-file", "--database", "--schema", "--role", "--warehouse"}


def _snow(args: list[str]) -> Verdict:
    pos, flags = scan(args, _SNOW_VALUED)
    if not pos or _has_help(args):
        return _r("snow")
    v = pos[0]
    if v == "sql":
        return _s("snow", "sql")
    if v == "object" and len(pos) > 1 and pos[1] in ("list", "describe"):
        return _r("snow", f"object {pos[1]}")
    if v in ("help", "--version"):
        return _r("snow", v)
    return _w("snow", _label(pos, 2), "snow: only `sql` and `object list|describe` are read")


# ---------------------------------------------------------------- dbt / airflow / dagster / prefect / kafka

_DBT_VALUED = {"--log-level", "--log-path", "--profiles-dir", "--project-dir", "--target", "-t", "--profile", "--vars",
               "--threads", "--log-format", "--state", "--defer-state"}


def _dbt(args: list[str]) -> Verdict:
    pos, flags = scan(args, _DBT_VALUED)
    if not pos or _has_help(args):
        return _r("dbt")
    v = pos[0]
    sub = pos[1] if len(pos) > 1 else ""
    if v in ("parse", "compile", "ls", "list", "debug", "deps", "test", "show", "version", "help"):
        return _r("dbt", v)
    if v == "docs":
        return _r("dbt", f"docs {sub}".strip()) if sub in ("generate", "serve", "") else _u("dbt", f"docs {sub}")
    if v == "source":
        return _r("dbt", "source freshness") if sub in ("freshness", "") else _u("dbt", f"source {sub}")
    if v in ("run", "build", "seed", "snapshot", "run-operation", "retry", "clone"):
        return _w("dbt", v)
    return _u("dbt", v)


_AF_READ = {
    "dags": {"list", "show", "state", "list-runs", "list-jobs", "report", "details", "next-execution", "list-import-errors", "status"},
    "tasks": {"list", "state", "states-for-dag-run", "failed-deps", "render"},
    "config": {"get-value", "list"},
    "connections": {"list", "get"},
    "variables": {"list", "get"},
    "pools": {"list", "get"},
    "jobs": {"check"},
    "providers": {"list", "get", "behaviours", "links", "widgets", "hooks", "triggers", "auth"},
    "plugins": {""},
    "assets": {"list", "details"},
    "roles": {"list"},
}
_AF_WRITE = {
    "dags": {"trigger", "pause", "unpause", "delete", "backfill", "reserialize", "test"},
    "tasks": {"clear", "run", "test"},
    "connections": {"add", "delete", "import", "export", "create-default-connections", "test"},
    "variables": {"set", "delete", "import", "export"},
    "pools": {"set", "delete", "import", "export"},
    "roles": {"create", "delete", "add-perms", "del-perms", "export", "import"},
}
_AF_VALUED = {"-o", "--output"}


def _airflow(args: list[str]) -> Verdict:
    pos, flags = scan(args, _AF_VALUED)
    if not pos or _has_help(args):
        return _r("airflow")
    g = pos[0]
    sub = pos[1] if len(pos) > 1 else ""
    if g == "info" and any(f.split("=", 1)[0] == "--file-io" for f in flags):
        return _w("airflow", "info --file-io", "uploads the environment info")
    if g in ("version", "info", "cheat-sheet", "help"):
        return _r("airflow", g)
    if g in ("db", "users", "kerberos", "rotate-fernet-key", "scheduler", "webserver", "standalone", "triggerer", "celery",
             "kubernetes", "api-server", "dag-processor", "internal-api", "sync-perm", "task", "migrate", "upgradecheck"):
        return _w("airflow", _label(pos, 2), "")
    label = f"{g} {sub}".strip()
    if sub in _AF_WRITE.get(g, ()):
        return _w("airflow", label)
    if sub in _AF_READ.get(g, ()):
        return _r("airflow", label)
    if g == "config" and not sub:
        return _r("airflow", g)
    return _u("airflow", label)


_DAGSTER_READ = {"list", "info", "print", "preview", "logs", "version", "show", "list-versions", "export", "validate"}
_DAGSTER_WRITE = {"execute", "launch", "backfill", "materialize", "wipe", "delete", "start", "stop", "restart", "migrate", "reindex",
                  "cursor", "scaffold_config", "import", "run", "up", "reload", "set", "trigger"}


def _dagster(args: list[str]) -> Verdict:
    pos, flags = scan(args)
    if not pos or _has_help(args):
        return _r("dagster")
    if pos[0] in ("version", "help"):
        return _r("dagster", pos[0])
    hit = _path_verb(pos, _DAGSTER_READ, _DAGSTER_WRITE, ("list-",), ("delete-", "set-"), start=1, limit=4)
    label = _label(pos, 3)
    if hit is None:
        return _u("dagster", label)
    return _r("dagster", label) if hit[0] == "read" else _w("dagster", label)


_PREFECT_READ = {"ls", "list", "inspect", "view", "get", "logs", "preview", "version", "status", "validate", "show"}
_PREFECT_WRITE = {"run", "execute", "cancel", "delete", "create", "apply", "build", "pause", "resume", "set", "unset", "set-schedule",
                  "clear-schedule", "register", "start", "stop", "reset", "retry", "restart", "use", "rename", "login", "logout",
                  "migrate", "upgrade", "downgrade", "revision", "push", "pull", "deploy", "trigger", "update", "provision-infra",
                  "enable", "disable", "sync"}


def _prefect(args: list[str]) -> Verdict:
    pos, flags = scan(args)
    if not pos or _has_help(args):
        return _r("prefect")
    if pos[0] in ("version", "help"):
        return _r("prefect", pos[0])
    hit = _path_verb(pos, _PREFECT_READ, _PREFECT_WRITE, (), (), start=1, limit=5)
    label = _label(pos, 3)
    if hit is None:
        return _u("prefect", label)
    return _r("prefect", label) if hit[0] == "read" else _w("prefect", label)


def _kafka(name: str, args: list[str]) -> Verdict:
    flags = {a.split("=", 1)[0] for a in args if a.startswith("--")}
    if flags & {"--create", "--delete", "--alter", "--delete-config", "--reassignment-json-file", "--execute", "--add-config"}:
        verb = next(f for f in ("--create", "--delete", "--alter", "--delete-config", "--execute", "--add-config",
                                "--reassignment-json-file") if f in flags)
        return _w(name, verb)
    if flags & {"--list", "--describe", "--help", "--version"}:
        return _r(name, next(iter(sorted(flags & {"--list", "--describe", "--help", "--version"}))))
    return _u(name, "", "no --list or --describe")


# ---------------------------------------------------------------- program table

def _guarded(name: str, args: list[str]) -> Verdict | None:
    n = name[:-3] if name.endswith(".sh") else name
    if n == "aws":
        return _aws(args)
    if n == "gcloud":
        return _gcloud(args)
    if n == "gsutil":
        return _gsutil(args)
    if n == "bq":
        return _bq(args)
    if n == "databricks":
        return _databricks(args)
    if n == "dbsqlcli":
        return _dbsqlcli(args)
    if n == "kubectl":
        return _kubectl(args)
    if n == "helm":
        return _helm(args)
    if n in ("terraform", "tofu"):
        return _terraform(args, n)
    if n == "pulumi":
        return _pulumi(args)
    if n == "az":
        return _az(args)
    if n == "snow":
        return _snow(args)
    if n == "dbt":
        return _dbt(args)
    if n == "airflow":
        return _airflow(args)
    if n == "dagster":
        return _dagster(args)
    if n == "prefect":
        return _prefect(args)
    if n in ("kafka-topics", "kafka-configs"):
        return _kafka(n, args)
    return None


def classify_cli(name: str, args: list[str]) -> Verdict | None:
    """Verdict for one guarded CLI's arguments, no wrappers followed (None: not a guarded CLI). Pack argv checks build on this."""
    return _guarded(name, args)


# ---------------------------------------------------------------- wrappers

_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_CONTROL = {"do", "then", "else", "elif", "if", "while", "until", "!", "{", "}", "function"}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "ash"}
_SIMPLE_PREFIX = {"nohup", "exec", "builtin", "setsid", "unbuffer", "chronic", "caffeinate", "ionice", "chrt", "taskset", "stdbuf",
                  "nice", "time", "command", "sudo", "doas", "env", "timeout", "watch", "flock", "su", "xargs", "find", "parallel",
                  "pkexec", "runuser"}


def _drop_options(args: list[str], valued: set[str]) -> list[str]:
    """Words after leading options (an option in `valued` consumes its value; a lone `--` ends options)."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return args[i + 1:]
        if a.startswith("-") and len(a) > 1:
            if a in valued and "=" not in a:
                i += 2
                continue
            i += 1
            continue
        break
    return args[i:]


def _unwrap_env(args: list[str]) -> list[str] | tuple[str, str] | None:
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return args[i + 1:]
        if a in ("-u", "--unset", "-C", "--chdir", "-P"):
            i += 2
            continue
        if a in ("-S", "--split-string") and i + 1 < len(args):
            return ("string", args[i + 1] + " " + " ".join(shlex.quote(x) for x in args[i + 2:]))
        if a.startswith("--split-string="):
            return ("string", a.split("=", 1)[1] + " " + " ".join(shlex.quote(x) for x in args[i + 1:]))
        if a.startswith("-S") and len(a) > 2:
            return ("string", a[2:] + " " + " ".join(shlex.quote(x) for x in args[i + 1:]))
        if a.startswith("-") or _ASSIGN.match(a):
            i += 1
            continue
        break
    return args[i:]


def _unwrap_sudo(args: list[str]) -> list[str]:
    valued = {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-r", "-t", "-U", "-T", "--user", "--group", "--host", "--prompt",
              "--chdir", "--role", "--type", "--other-user"}
    rest = _drop_options(args, valued)
    while rest and _ASSIGN.match(rest[0]):
        rest = rest[1:]
    return rest


def _unwrap_timeout(args: list[str]) -> list[str]:
    rest = _drop_options(args, {"-s", "--signal", "-k", "--kill-after"})
    return rest[1:] if rest else []  # the duration


def _unwrap_nice(args: list[str]) -> list[str]:
    return _drop_options(args, {"-n", "--adjustment"})


def _unwrap_stdbuf(args: list[str]) -> list[str]:
    return _drop_options(args, {"-i", "-o", "-e", "--input", "--output", "--error"})


def _unwrap_watch(args: list[str]) -> list[str]:
    return _drop_options(args, {"-n", "--interval", "-w", "--width"})


def _unwrap_flock(args: list[str]) -> list[str]:
    if "-c" in args or "--command" in args:
        k = args.index("-c") if "-c" in args else args.index("--command")
        return [] if k + 1 >= len(args) else ["bash", "-c", args[k + 1]]
    rest = _drop_options(args, {"-w", "--timeout", "-E", "--conflict-exit-code"})
    return rest[1:] if rest else []


_XARGS_LONG_VALUED = {"--arg-file", "--delimiter", "--max-args", "--max-chars", "--max-procs", "--process-slot-var"}


def _unwrap_xargs(args: list[str]) -> list[str]:
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return args[i + 1:]
        if a.startswith("--"):
            i += 2 if (a in _XARGS_LONG_VALUED) else 1
            continue
        if a.startswith("-") and len(a) > 1:
            consumed_next = False
            for j, ch in enumerate(a[1:], 1):
                if ch in "aEdnPsIL":  # takes a value: fused (`-I{}`, `-n1`) or the next word
                    consumed_next = j + 1 == len(a)
                    break
                if ch in "li":  # optional fused value only
                    break
            i += 2 if consumed_next else 1
            continue
        break
    return args[i:]


_PARALLEL_VALUED = {"-j", "--jobs", "-P", "-n", "--max-args", "-N", "-L", "-a", "--arg-file", "--arg-file-sep", "--arg-sep", "-S",
                    "--sshlogin", "--sshloginfile", "--slf", "--joblog", "--results", "--res", "--colsep", "-C", "--halt",
                    "--retries", "--timeout", "--delay", "--header", "--tagstring", "--workdir", "--wd", "--tmpdir", "--tmp",
                    "-I", "--rpl"}


def _unwrap_parallel(args: list[str]) -> list[str]:
    rest = _drop_options(args, _PARALLEL_VALUED)
    out = []
    for a in rest:
        if a in (":::", "::::", ":::+"):
            break
        out.append(a)
    return out


def _unwrap_find(args: list[str]) -> list[list[str]]:
    """Commands of every -exec / -execdir / -ok / -okdir in a find argument list."""
    cmds: list[list[str]] = []
    i = 0
    while i < len(args):
        if args[i] in ("-exec", "-execdir", "-ok", "-okdir"):
            j = i + 1
            cur: list[str] = []
            while j < len(args) and args[j] not in (";", "+", "\\;"):
                cur.append(args[j])
                j += 1
            if cur:
                cmds.append(cur)
            i = j
        i += 1
    return cmds


_UV_VALUED = {
    "--with", "--with-requirements", "--with-editable", "--python", "-p", "--project", "--directory", "--env-file", "--extra",
    "--group", "--no-group", "--only-group", "--package", "--index", "--index-url", "-i", "--default-index", "--extra-index-url",
    "-f", "--find-links", "--index-strategy", "--keyring-provider", "--resolution", "--prerelease", "--exclude-newer",
    "--exclude-newer-package", "--refresh-package", "--reinstall-package", "--upgrade-package", "-P", "--no-binary-package",
    "--no-build-package", "--link-mode", "--config-setting", "-C", "--cache-dir", "--color", "--config-file", "--constraint", "-c",
    "--override", "--build-constraint", "--python-platform", "--fork-strategy", "--python-preference", "--python-fetch",
    "--allow-insecure-host", "--from", "--script", "-s", "--no-extra", "--torch-backend", "--lfs"}


def _after_run(a: list[str], valued: set[str] | frozenset[str]) -> list[str] | None:
    """Words after the first `run` word, past the runner's own options (global ones before it are skipped by position)."""
    for k, w in enumerate(a):
        if w == "run":
            return _drop_options(a[k + 1:], valued)
    return None


def _hatch(a: list[str]) -> list[str] | None:
    r = _after_run(a, set())
    if r:
        m = re.match(r"^[A-Za-z0-9_.-]+:(.+)$", r[0])
        if m:
            r = [m.group(1), *r[1:]]
    return r


_RUNNER_PREFIX = {
    "uv": lambda a: _after_run(a, _UV_VALUED),
    "uvx": lambda a: _drop_options(a, _UV_VALUED),
    "poetry": lambda a: _after_run(a, set()),
    "pipenv": lambda a: _after_run(a, set()),
    "pdm": lambda a: _after_run(a, {"--venv"}),
    "hatch": _hatch,
    "rye": lambda a: _after_run(a, set()),
    "pipx": lambda a: _drop_options(a[1:], {"--spec", "--python", "--pip-args"}) if a and a[0] == "run" else None,
    "conda": lambda a: _drop_options(a[1:], {"-n", "--name", "-p", "--prefix", "--cwd"}) if a and a[0] == "run" else None,
    "mamba": lambda a: _drop_options(a[1:], {"-n", "--name", "-p", "--prefix", "--cwd"}) if a and a[0] == "run" else None,
    "micromamba": lambda a: _drop_options(a[1:], {"-n", "--name", "-p", "--prefix", "--cwd"}) if a and a[0] == "run" else None,
}


_DOCKER_GLOBAL_VALUED = {"-H", "--host", "-c", "--context", "-l", "--log-level", "--config", "--tlscacert", "--tlscert", "--tlskey"}
_COMPOSE_GLOBAL_VALUED = {"-f", "--file", "-p", "--project-name", "--profile", "--env-file", "--project-directory", "--parallel",
                          "--ansi", "-H", "--host", "-c", "--context", "--progress"}
_DOCKER_EXEC_VALUED = {"-e", "--env", "--env-file", "-u", "--user", "-w", "--workdir", "--detach-keys", "--index"}
_DOCKER_RUN_VALUED = {
    "-a", "--attach", "--add-host", "--blkio-weight", "--cap-add", "--cap-drop", "--cgroup-parent", "--cidfile", "--cpu-period",
    "--cpu-quota", "--cpu-shares", "--cpus", "-c", "--cpuset-cpus", "--cpuset-mems", "--detach-keys", "--device", "--dns",
    "--dns-option", "--dns-search", "-e", "--env", "--env-file", "--expose", "--gpus", "--group-add", "--health-cmd",
    "--health-interval", "--health-retries", "--health-start-period", "--health-timeout", "-h", "--hostname", "--ip", "--ip6",
    "--ipc", "--isolation", "-l", "--label", "--label-file", "--link", "--log-driver", "--log-opt", "--mac-address", "-m",
    "--memory", "--memory-reservation", "--memory-swap", "--mount", "--name", "--network", "--net", "--network-alias",
    "--oom-score-adj", "--pid", "--pids-limit", "--platform", "-p", "--publish", "--pull", "--restart", "--runtime",
    "--security-opt", "--shm-size", "--stop-signal", "--stop-timeout", "--storage-opt", "--sysctl", "--tmpfs", "-u", "--user",
    "--userns", "--uts", "-v", "--volume", "--volume-driver", "--volumes-from", "-w", "--workdir", "--entrypoint"}
_COMPOSE_RUN_VALUED = {"-e", "--env", "-v", "--volume", "-p", "--publish", "-u", "--user", "-w", "--workdir", "--name",
                       "--entrypoint", "-l", "--label", "--cap-add", "--cap-drop", "--use-aliases", "--pull"}


_IMAGE_CLI = {"aws-cli": "aws", "terraform": "terraform", "opentofu": "tofu", "tofu": "tofu", "kubectl": "kubectl", "helm": "helm",
              "airflow": "airflow", "pulumi": "pulumi", "google-cloud-cli": "gcloud", "cloud-sdk": "gcloud", "azure-cli": "az",
              "databricks-cli": "databricks", "snowflake-cli": "snow", "dbt": "dbt", "dagster": "dagster", "prefect": "prefect"}
NOARGS = "\0noargs"


def _image_cli(image: str) -> str | None:
    """The guarded CLI a container image's entrypoint is (registry, tag and digest stripped), else None."""
    n = image.split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0].lower()
    if n.startswith("dbt-"):
        return "dbt"
    if n.endswith("-kubectl"):
        return "kubectl"
    return _IMAGE_CLI.get(n)


def _entry_cmd(args: list[str], valued: set[str], positionals_before_cmd: int, image: bool = False) -> list[str] | None:
    """Words of the inner command after options and `positionals_before_cmd` positionals (container, image or service).
    With `image`, the first positional is an image (or compose service): one whose entrypoint is a guarded CLI runs that CLI."""
    i = 0
    seen = 0
    first: str | None = None
    entry: str | None = None
    while i < len(args):
        a = args[i]
        if a == "--":
            if seen >= positionals_before_cmd:
                i += 1
                break
            i += 1
            continue
        if a.startswith("-") and len(a) > 1:
            name = a.split("=", 1)[0]
            if name in ("--entrypoint",):
                entry = a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < len(args) else None)
            if "=" not in a and name in valued:
                i += 2
            else:
                i += 1
            continue
        if first is None:
            first = a
        seen += 1
        i += 1
        if seen == positionals_before_cmd:
            break
    inner = args[i:]
    if entry:
        inner = [entry, *inner]
    elif image and first:
        cli = _image_cli(first)
        if cli and not (inner and _base(inner[0]) == cli):
            inner = [cli, *inner] if inner else [NOARGS, cli]
    return inner or None


def _container_inner(name: str, args: list[str]) -> list[str] | None:
    """For docker / podman / docker compose: the inner command of `exec` / `run`, or None for other verbs."""
    if name in ("docker-compose",):
        args = ["compose", *args]
    if args and args[0] == "compose":
        rest = _drop_options(args[1:], _COMPOSE_GLOBAL_VALUED)
        if not rest or rest[0] not in ("exec", "run"):
            return None
        valued = _DOCKER_EXEC_VALUED if rest[0] == "exec" else _COMPOSE_RUN_VALUED
        return _entry_cmd(rest[1:], valued, 1, rest[0] == "run")
    rest = _drop_options(args, _DOCKER_GLOBAL_VALUED)
    if rest and rest[0] == "container":
        rest = rest[1:]
    if not rest or rest[0] not in ("exec", "run", "create"):
        return None
    valued = _DOCKER_EXEC_VALUED if rest[0] == "exec" else _DOCKER_RUN_VALUED
    return _entry_cmd(rest[1:], valued, 1, rest[0] != "exec")


def _kubectl_exec_inner(args: list[str]) -> list[str] | None:
    pos_args = args
    if "--" in pos_args:
        return pos_args[pos_args.index("--") + 1:]
    pos, _ = scan(pos_args, _KUBECTL_EXEC_VALUED)
    return pos[1:] or None  # deprecated form: kubectl exec POD CMD


_SSH_VALUED = set("bcDEeFIiJLlmOopQRSWw")
_SSH_CMD_OPT = re.compile(r"\s*(?:proxycommand|localcommand|remotecommand)\s*[=\s]\s*(.*)$", re.I | re.S)


def _ssh_parse(args: list[str]) -> tuple[list[str] | None, list[str]]:
    """(remote command words or None, commands carried by -o ProxyCommand / LocalCommand / RemoteCommand)."""
    opt_cmds: list[str] = []
    state = {"i": 0}

    def option(a: str) -> None:
        i = state["i"]
        for k, ch in enumerate(a[1:], 1):
            if ch in _SSH_VALUED:
                val = a[k + 1:] if k + 1 < len(a) else (args[i + 1] if i + 1 < len(args) else "")
                state["i"] += 0 if k + 1 < len(a) else 1
                m = _SSH_CMD_OPT.match(val) if ch == "o" else None
                if m:
                    opt_cmds.append(m.group(1))
                return

    host_seen = False
    while state["i"] < len(args):
        a = args[state["i"]]
        if a == "--":
            state["i"] += 1
            if host_seen:
                break
            host_seen = True  # the next word is the host
            state["i"] += 1
            continue
        if a.startswith("-") and len(a) > 1:
            option(a)
            state["i"] += 1
            continue
        if host_seen:
            break
        host_seen = True
        state["i"] += 1
    rest = args[state["i"]:] if host_seen else []
    return (rest or None), opt_cmds


def _shell_files(args: list[str]) -> list[str]:
    """Non-option words of a shell invocation (the script first), past valued options such as `-o pipefail`."""
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-o", "-O", "+o", "+O", "--rcfile", "--init-file"):
            i += 2
            continue
        if a == "--":
            out.extend(args[i + 1:])
            break
        if a.startswith(("-", "+")) or a == "<":
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def _shell_c(args: list[str]) -> str | None:
    """The string after -c in `bash -lc 'cmd'` style options."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-o", "-O", "+o", "+O", "--rcfile", "--init-file"):
            i += 2
            continue
        if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", a):
            return args[i + 1] if i + 1 < len(args) else ""
        if a.startswith("-") or a.startswith("+"):
            i += 1
            continue
        return None
    return None


# ---------------------------------------------------------------- small wrapper helpers

_REDIR_WORDS = {">", ">>", ">&", "&>", "&>>", "<", "<&", ">|", "<>", "<<<", "<<"}
_WRITE_REDIR = {">", ">>", ">|", "&>", "&>>", "<>"}
WRITE_RUN = ("de-agent: this command line writes a script and then runs it; write the file in one command and run it in a later "
             "one, or put the commands in a plan as a shell step")
_TMUX_SHELL = {"new", "new-session", "neww", "new-window", "split-window", "splitw", "run", "run-shell", "respawn-pane",
               "respawnp", "respawn-window", "respawnw", "display-popup", "popup", "pipe-pane", "pipep", "if-shell", "if",
               "send-keys", "send"}


def _drop_clusters(args: list[str], letters: str, long_valued: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Words after leading options; in a short cluster the first letter in `letters` takes a value (fused, or the next word)."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return args[i + 1:]
        if a.startswith("--"):
            i += 2 if (a in long_valued) else 1
            continue
        if a.startswith("-") and len(a) > 1:
            k = next((j for j, ch in enumerate(a[1:], 1) if ch in letters), None)
            i += 2 if (k is not None and k + 1 == len(a)) else 1
            continue
        break
    return args[i:]


def _su_command(args: list[str]) -> str | None:
    """The command of `su -c X`, `su -lc X`, `su --command=X`, `runuser -c X`."""
    for k, a in enumerate(args):
        if a.startswith("--command="):
            return a.split("=", 1)[1]
        if a == "--command":
            return args[k + 1] if k + 1 < len(args) else ""
        if a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
            j = a.index("c", 1)
            return a[j + 1:] if j + 1 < len(a) else (args[k + 1] if k + 1 < len(args) else "")
    return None


def _script_cmd(args: list[str]) -> list[str] | str:
    """`script`: util-linux `-c CMD` (a string), else the macOS form `script [opts] file cmd...` (a word list)."""
    for k, a in enumerate(args):
        if a.startswith("--command="):
            return a.split("=", 1)[1]
        if a == "--command":
            return args[k + 1] if k + 1 < len(args) else ""
        if a.startswith("-") and not a.startswith("--") and a.endswith("c") and k + 1 < len(args):
            return args[k + 1]
    return _drop_options(args, {"-t", "-T", "-I", "-O", "-B", "-E", "-m", "-F", "-o"})[1:]


def _cron_lines(text: str) -> list[str]:
    """Command part of each crontab line (the five time fields or `@word` dropped)."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or _ASSIGN.match(line):
            continue
        parts = line.split(None, 1 if line.startswith("@") else 5)
        out.append(parts[-1] if len(parts) > (1 if line.startswith("@") else 5) else "")
    return out


# ---------------------------------------------------------------- the walker

def _subst(word: str) -> list[str]:
    """Command texts inside `$( ... )`, backticks and process substitutions of one shell word."""
    out: list[str] = []
    i, n = 0, len(word)
    while i < n:
        if word.startswith("$(", i) or word.startswith("<(", i) or word.startswith(">(", i):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += (word[j] == "(") - (word[j] == ")")
                j += 1
            out.append(word[i + 2:j - 1])
            i = j
            continue
        if word[i] == "`":
            j = word.find("`", i + 1)
            j = n if j == -1 else j
            out.append(word[i + 1:j])
            i = j + 1
            continue
        i += 1
    return out


def _sql_client(exe: str) -> bool:
    return exe in DB_CLIENTS or exe in ("dea", "dbsqlcli")


class Walker:
    def __init__(self, cwd: str | None, allow: list[list[str]], bodies: list[str] | None = None) -> None:
        self.cwd = cwd
        self.allow = allow
        self.bodies = bodies or []
        self.out: list[Verdict] = []
        self.written: set[str] = set()

    # ---- command text
    def command(self, text: str, depth: int) -> None:
        from de_agent.extract import _preprocess, _split_simple, _tokenize

        if depth > MAX_DEPTH:
            if GUARD_NAME_RE.search(text):
                self.out.append(Verdict("unparseable", "shell", "", "de-agent: command is nested too deeply to check"))
            return
        if not text.strip():
            return
        try:
            pre, bodies = _preprocess(text)
            tokens = _tokenize(pre)
        except ValueError:
            if GUARD_NAME_RE.search(text):  # a command we cannot tokenise that mentions a guarded CLI: fail closed
                self.out.append(Verdict("unparseable", "shell", ""))
            return
        for inner in _subst(text):  # `$( ... )`, backticks and process substitutions run their own commands
            self.command(inner, depth + 1)
        saved = self.bodies
        self.bodies = bodies
        try:
            for piped, words in _split_simple(tokens):
                self.words(words, depth, piped)
        finally:
            self.bodies = saved

    # ---- one simple command
    def words(self, words: list[str], depth: int, piped: bool = False) -> None:
        args = list(words)
        self.note_writes(args)
        i = 0
        while i < len(args):
            a = args[i]
            if a == "function":  # `function f { ... }`: the body is parsed as it stands
                i += 2
            elif a == "coproc":
                i += 2 if i + 2 < len(args) and args[i + 2] == "{" else 1
            elif a in _REDIR_WORDS:  # leading redirect: `>/dev/null aws s3 rm ...`
                i += 2
            elif a.isdigit() and i + 1 < len(args) and args[i + 1] in _REDIR_WORDS:
                i += 1
            elif _ASSIGN.match(a) or a in _CONTROL:
                i += 1
            else:
                break
        args = args[i:]
        if not args:
            return
        if DOLLAR_PROG_RE.match(args[0]):
            self.out.append(Verdict("unparseable", "shell", "", "de-agent: a program name built from a variable after a guarded CLI "
                                    "name (`aws${IFS}...`) cannot be checked; write the command out"))
            return
        # `find ... -exec` continuation segments (the `;` terminator split the command)
        if args[0] in ("-exec", "-execdir", "-ok", "-okdir"):
            for cmd in _unwrap_find(args):
                self.words(cmd, depth + 1)
            return
        name = _base(args[0])
        rest = args[1:]
        self.program(name, rest, args, depth, piped)

    def inner(self, cmd: list[str] | None, depth: int) -> None:
        if cmd:
            self.words(cmd, depth + 1)

    # ---- files written by the command line itself, and scripts run directly
    def norm(self, path: str) -> str:
        return os.path.normpath(os.path.join(self.cwd or os.getcwd(), os.path.expanduser(path)))

    def note_writes(self, args: list[str]) -> None:
        for k, a in enumerate(args):
            if a in _WRITE_REDIR and k + 1 < len(args):
                self.written.add(self.norm(args[k + 1]))
            elif a.startswith("of=") and len(a) > 3:
                self.written.add(self.norm(a[3:]))
            elif _base(a) == "tee":
                self.written.update(self.norm(w) for w in args[k + 1:] if not w.startswith("-") and w not in _REDIR_WORDS)
            elif _base(a) in ("cp", "mv", "install", "ln"):
                tail = [w for w in args[k + 1:] if not w.startswith("-") and w not in _REDIR_WORDS]
                if len(tail) >= 2:
                    self.written.add(self.norm(tail[-1]))

    def direct(self, path: str, depth: int) -> None:
        """`./s.sh`, `bin/wrap`: a shell script under the working directory is scanned like `bash s.sh`."""
        p = self.norm(path)
        if p in self.written:
            self.out.append(Verdict("unparseable", "shell", "", WRITE_RUN))
            return
        try:
            base = os.path.realpath(self.cwd or os.getcwd())
            real = os.path.realpath(p)
            if os.path.commonpath([base, real]) != base or not os.path.isfile(real):
                return
            with open(real, "rb") as f:
                head = f.read(256)
        except (OSError, ValueError):
            return
        if b"\0" in head:
            return
        first = head.split(b"\n", 1)[0].decode("utf-8", "replace")
        if first.startswith("#!"):
            if re.search(r"(?:^|[/\s])(?:ba|z|da|k|a)?sh(?:\s|$)", first):
                self.script(path, depth)
            elif "python" in first:
                self.python([path], depth)
        elif real.endswith(".sh"):
            self.script(path, depth)

    def split_stdin(self, words: list[str]) -> tuple[list[str], list[str]]:
        """(words without here-doc / here-string / other redirects, the stdin texts they carried)."""
        from de_agent.extract import _strip_redirects

        clean: list[str] = []
        texts: list[str] = []
        k = 0
        while k < len(words):
            w = words[k]
            if w == "<<<" and k + 1 < len(words):
                texts.append(words[k + 1])
                k += 2
            elif w == "<<" and k + 1 < len(words) and PLACEHOLDER.fullmatch(words[k + 1]):
                idx = int(PLACEHOLDER.fullmatch(words[k + 1]).group(1))  # type: ignore[union-attr]
                if idx < len(self.bodies):
                    texts.append(self.bodies[idx])
                k += 2
            else:
                clean.append(w)
                k += 1
        return _strip_redirects(clean), texts

    def tmux(self, rest: list[str], depth: int) -> None:
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            i += 2 if rest[i] in ("-L", "-S", "-f", "-c", "-T") else 1
        if i >= len(rest) or rest[i] not in _TMUX_SHELL:
            return
        sub = rest[i]
        valued = {"-c", "-n", "-s", "-t", "-x", "-y", "-e", "-F", "-l", "-f", "-E", "-S", "-h", "-w", "-N", "-p"}
        if sub in ("display-popup", "popup"):
            valued |= {"-d", "-b", "-B", "-T"}
        cmd = _drop_options(rest[i + 1:], valued)
        if sub in ("if-shell", "if"):
            for c in cmd:
                self.command(c, depth + 1)
        elif cmd:
            self.command(" ".join(cmd), depth + 1)

    def deferred(self, name: str, rest: list[str], depth: int, piped: bool) -> None:
        """at / batch / crontab: code run later, read here from a here-string, here-doc, `-f file` or file argument."""
        clean, texts = self.split_stdin(rest)
        pos = _drop_options(clean, {"-f", "-u", "-q", "-m"} if name != "crontab" else {"-u"})
        found = bool(texts)
        if name == "crontab":
            if pos and pos[0] != "-":
                found = True
                try:
                    texts = [open(os.path.expanduser(pos[0]), encoding="utf-8", errors="replace").read()]
                except OSError:
                    texts = []
            for t in texts:
                for c in _cron_lines(t):
                    self.command(c, depth + 1)
            reads_stdin = bool(pos) and pos[0] == "-"
        else:
            for k, a in enumerate(clean):
                if a == "-f" and k + 1 < len(clean):
                    found = True
                    self.script(clean[k + 1], depth)
            for t in texts:
                self.command(t, depth + 1)
            reads_stdin = not found
        if piped and reads_stdin and not texts and GUARD_NAME_RE.search(self.full_text):
            self.out.append(Verdict("unparseable", "shell", "", PIPED_SHELL))

    def program(self, name: str, rest: list[str], args: list[str], depth: int, piped: bool) -> None:
        if name in ("env",):
            r = _unwrap_env(rest)
            if isinstance(r, tuple):
                self.command(r[1], depth + 1)
            else:
                self.inner(r, depth)
            return
        if name in ("sudo", "doas", "pkexec"):
            self.inner(_unwrap_sudo(rest), depth)
            return
        if "/" in args[0] and not args[0].startswith(("-", "$")):
            self.direct(args[0], depth)
        if name in ("nohup", "exec", "builtin", "setsid", "unbuffer", "chronic", "time"):
            self.inner(_drop_options(rest, {"-f", "--format", "-o", "--output", "-a"} if name in ("time", "exec") else set()), depth)
            return
        if name == "command":
            if any(a in ("-v", "-V") for a in rest[:2]):
                return
            self.inner(_drop_options(rest, set()), depth)
            return
        if name == "caffeinate":
            self.inner(_drop_options(rest, {"-t", "-w"}), depth)
            return
        if name in ("ionice",):
            self.inner(_drop_options(rest, {"-c", "-n", "-p", "-P", "-u"}), depth)
            return
        if name == "chrt":
            self.inner(_drop_options(rest, set())[1:], depth)
            return
        if name == "taskset":
            r = _drop_options(rest, {"-c", "--cpu-list", "-p"})
            cpus = any(f.startswith("--cpu-list") or (f.startswith("-") and not f.startswith("--") and "c" in f[1:])
                       for f in rest[:len(rest) - len(r)])
            self.inner(r if cpus else r[1:], depth)  # the mask positional is absent when -c/--cpu-list gives the list
            return
        if name == "nice":
            self.inner(_unwrap_nice(rest), depth)
            return
        if name == "stdbuf":
            self.inner(_unwrap_stdbuf(rest), depth)
            return
        if name == "timeout":
            self.inner(_unwrap_timeout(rest), depth)
            return
        if name == "watch":
            r = _unwrap_watch(rest)
            if len(r) == 1 and " " in r[0]:
                self.command(r[0], depth + 1)
            else:
                self.inner(r, depth)
            return
        if name == "flock":
            self.inner(_unwrap_flock(rest), depth)
            return
        if name == "xargs":
            self.inner(_unwrap_xargs(rest), depth)
            return
        if name == "parallel":
            r = _unwrap_parallel(rest)
            if r and " " in r[0].strip():
                self.command(" ".join(r), depth + 1)
            elif r:
                self.inner(r, depth)
            else:
                for k, w in enumerate(rest):  # `parallel ::: 'cmd1' 'cmd2'`: the arguments are the commands
                    if w in (":::", ":::+"):
                        for c in rest[k + 1:]:
                            self.command(c, depth + 1)
                        break
            return
        if name == "find":
            for cmd in _unwrap_find(rest):
                self.words(cmd, depth + 1)
            return
        if name in ("su", "runuser"):
            c = _su_command(rest)
            if c is not None:
                self.command(c, depth + 1)
            elif name == "runuser" and any(a in ("-u", "--user") for a in rest):
                self.inner(_drop_options(rest, {"-u", "--user", "-g", "--group", "-G", "--supp-group", "-s", "--shell"}), depth)
            return
        if name in ("strace", "ltrace"):
            self.inner(_drop_options(rest, set("-" + c for c in "oepusaEIOPSXbCW") | {"--output", "--trace", "--signal", "--env"}), depth)
            return
        if name == "chroot":
            self.inner(_drop_options(rest, {"--userspec", "--groups"})[1:], depth)
            return
        if name == "nsenter":
            self.inner(_drop_options(rest, {"-t", "--target", "-S", "--setuid", "-G", "--setgid"}), depth)
            return
        if name == "unshare":
            self.inner(_drop_options(rest, {"--propagation", "--setgroups", "-R", "--root", "-w", "--wd", "--forward-signals"}), depth)
            return
        if name == "busybox":
            self.inner(rest, depth)
            return
        if name == "systemd-run":
            self.inner(_drop_options(rest, {
                "-p", "--property", "-u", "--unit", "--slice", "-E", "--setenv", "--uid", "--gid", "--description", "--service-type",
                "--working-directory", "-H", "--host", "-M", "--machine", "--nice", "--timer-property", "--on-active", "--on-boot",
                "--on-startup", "--on-unit-active", "--on-unit-inactive", "--on-calendar", "--on-clock-change",
                "--on-timezone-change"}), depth)
            return
        if name == "script":
            c = _script_cmd(rest)
            if isinstance(c, str):
                self.command(c, depth + 1)
            else:
                self.inner(c, depth)
            return
        if name == "tmux":
            self.tmux(rest, depth)
            return
        if name == "screen":
            self.inner(_drop_clusters(rest, "SsctTphe"), depth)
            return
        if name == "trap":
            r = _drop_options(rest, set())
            if len(r) >= 2 and r[0] not in ("-", ""):
                self.command(r[0], depth + 1)
            return
        if name in ("at", "batch", "crontab"):
            self.deferred(name, rest, depth, piped)
            return
        if name in _RUNNER_PREFIX:
            r = _RUNNER_PREFIX[name](rest)
            if r is not None:
                self.inner(r, depth)
                return
        if name == "eval":
            self.command(" ".join(rest), depth + 1)
            return
        if name in _SHELLS:
            self.shell(name, rest, depth, piped)
            return
        if name in ("source", "."):
            if rest:
                self.script(rest[0], depth)
            return
        if name == "ssh":
            r, opt_cmds = _ssh_parse(rest)
            for c in opt_cmds:
                self.command(c, depth + 1)
            if r:
                clean, texts = self.split_stdin(r)
                self.command(" ".join(clean), depth + 1)  # the remote shell re-parses the joined arguments
                if clean and _base(clean[0]) in _SHELLS:
                    for t in texts:
                        self.command(t, depth + 1)
                    if piped and not texts and GUARD_NAME_RE.search(self.full_text):
                        self.out.append(Verdict("unparseable", "shell", "", PIPED_SHELL))
            return
        if name in ("docker", "podman", "docker-compose", "nerdctl"):
            inner = _container_inner(name, rest)
            if inner and inner[0] == NOARGS:
                self.out.append(_u(inner[1], "", "a container image whose entrypoint is this CLI, run with no command"))
            else:
                self.inner(inner, depth)
            return
        if name == "kubectl":
            pos, _ = scan(rest, _KUBECTL_VALUED)
            if pos and pos[0] == "exec":
                k = rest.index("exec")
                inner = _kubectl_exec_inner(rest[k + 1:])
                self.kubectl_exec(inner, depth)
                return
        if name.startswith("python") and re.fullmatch(r"python[0-9.]*", name):
            self.python(rest, depth)
            return
        from de_agent.extract import _strip_redirects

        rest = _strip_redirects(rest)  # `2>&1`, `> file`: shell syntax, not arguments of the CLI
        v = _guarded(name, rest)
        if v is None:
            return
        self.out.append(self.apply_allow(name, [args[0], *rest], v))

    def apply_allow(self, name: str, args: list[str], v: Verdict) -> Verdict:
        if v.kind in ("write", "unknown"):
            shaped = [_base(args[0]), *args[1:]]
            for p in self.allow:
                if len(shaped) >= len(p) and [_base(p[0]), *p[1:]] == shaped[:len(p)]:
                    return Verdict("allow", v.cli, v.verb, "matched guard.cli_allow")
        return v

    def kubectl_exec(self, inner: list[str] | None, depth: int) -> None:
        if not inner:
            self.out.append(_u("kubectl", "exec", "no command after `--`"))
            return
        before = len(self.out)
        self.words(inner, depth + 1)
        exe = _base(next((a for a in inner if not _ASSIGN.match(a)), ""))
        if len(self.out) == before and exe in _SHELLS and any(
                re.search(r"(?<![\w-])" + re.escape(c) + r"(?![\w-])", " ".join(inner)) for c in DB_CLIENTS):
            exe = "psql"  # `bash -c "psql ..."`: sql_guard reads the client inside
        if len(self.out) == before and not _sql_client(exe):
            self.out.append(_u("kubectl", "exec", f"runs `{exe or '?'}` inside a pod"))
        elif len(self.out) == before:
            self.out.append(_s("kubectl", "exec"))

    def shell(self, name: str, rest: list[str], depth: int, piped: bool) -> None:
        c = _shell_c(rest)
        if c is not None:
            self.command(c, depth + 1)
            return
        for k, w in enumerate(rest):
            if w == "<<" and k + 1 < len(rest) and PLACEHOLDER.fullmatch(rest[k + 1]):
                idx = int(PLACEHOLDER.fullmatch(rest[k + 1]).group(1))  # type: ignore[union-attr]
                if idx < len(self.bodies):
                    self.command(self.bodies[idx], depth + 1)
                return
            if w == "<<<" and k + 1 < len(rest):
                self.command(rest[k + 1], depth + 1)
                return
        files = _shell_files(rest)
        if files:
            self.script(files[0], depth)
            return
        if piped and GUARD_NAME_RE.search(self.full_text):
            self.out.append(Verdict("unparseable", "shell", "", PIPED_SHELL))

    full_text = ""

    def script(self, path: str, depth: int) -> None:
        if self.norm(path) in self.written:
            self.out.append(Verdict("unparseable", "shell", "", WRITE_RUN))
            return
        if depth >= 3:
            return
        p = os.path.expanduser(path)
        if not os.path.isabs(p):
            p = os.path.join(self.cwd or os.getcwd(), p)
        try:
            if os.path.getsize(p) > MAX_SCRIPT:
                self.out.append(Verdict("unparseable", "shell", "", f"de-agent: script {path} is too large to check"))
                return
            text = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            return  # a missing script: the shell itself fails, nothing runs
        self.command(text, depth + 1)

    def python(self, rest: list[str], depth: int) -> None:
        """Strings and argv lists in `python -c` / heredoc code that run a guarded CLI (subprocess, os.system)."""
        from de_agent.extract import _python_code

        for k, a in enumerate(rest):
            if a in ("-W", "-X", "-Q", "--check-hash-based-pycs"):
                continue
            mod = rest[k + 1] if a == "-m" and k + 1 < len(rest) else (a[2:] if a.startswith("-m") and len(a) > 2 else None)
            if mod is not None:
                cli = _PY_MODULE_CLI.get(mod.split(".")[0])
                if cli:
                    self.words([cli, *rest[k + 2:]], depth + 1)
                    return
                break
            if not a.startswith("-"):
                break
        try:
            code, _why = _python_code(rest, False, self.bodies, self.cwd)
        except Exception:
            return
        for src in code:
            if src.startswith("<unreadable") or not _RUNS_PROCESSES_RE.search(src) or not GUARD_NAME_RE.search(src):
                continue
            try:
                tree = ast.parse(src)
            except (SyntaxError, ValueError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and GUARD_NAME_RE.search(node.value):
                    self.command(node.value, depth + 1)
                elif isinstance(node, (ast.List, ast.Tuple)) and node.elts and all(
                        isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
                    vals = [e.value for e in node.elts]  # type: ignore[attr-defined]
                    if any(_base(v) in GUARDED_CLIS for v in vals):
                        self.words(vals, depth + 1)


GUARD_NAME_RE = re.compile(r"(?<![\w.-])(?:" + "|".join(re.escape(n) for n in sorted(GUARDED_CLIS, key=len, reverse=True)) +
                           r")(?:\.sh)?(?![\w-])")
DOLLAR_PROG_RE = re.compile(r"^(?:[^$/\s]*/)*(?:" + "|".join(re.escape(n) for n in sorted(GUARDED_CLIS, key=len, reverse=True)) +
                            r")(?:\.sh)?\$")
_PY_MODULE_CLI = {"awscli": "aws", "airflow": "airflow", "dbt": "dbt", "dagster": "dagster", "prefect": "prefect"}
_RUNS_PROCESSES_RE = re.compile(r"subprocess|os\.system|os\.popen|Popen|check_output|check_call|pty\.spawn|\bsh\.|plumbum|asyncio\.create_subprocess")


def parse_allow(entries: list[str] | tuple[str, ...]) -> list[list[str]]:
    """`guard.cli_allow` strings -> word lists. An entry needs the CLI and a verb and no shell operators; others are ignored."""
    out: list[list[str]] = []
    for e in entries:
        if not isinstance(e, str) or re.search(r"[;&|<>`$()\n\\]", e):
            continue
        try:
            words = shlex.split(e)
        except ValueError:
            continue
        if len(words) >= 2:
            out.append(words)
    return out


def classify_command(command: str, cwd: str | None = None, allow: list[str] | tuple[str, ...] = ()) -> list[Verdict]:
    """Verdicts for every guarded CLI invocation in a shell command line (empty list: nothing to guard)."""
    w = Walker(cwd, parse_allow(allow))
    w.full_text = command
    w.command(command, 0)
    seen: set[Verdict] = set()
    out = []
    for v in w.out:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def classify_argv(argv: list[str], allow: list[str] | tuple[str, ...] = ()) -> list[Verdict]:
    """Verdicts for one argv (what a pack's `run:` list will execute). `shell=False` is assumed: no operators are interpreted."""
    w = Walker(None, parse_allow(allow))
    w.full_text = " ".join(argv)
    w.words(list(argv), 0)
    return w.out


def first_denial(verdicts: list[Verdict]) -> Verdict | None:
    for v in verdicts:
        if v.denied:
            return v
    return None
