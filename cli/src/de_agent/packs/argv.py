"""Read-verb classifier for the argv lists of `kind: cli` packs. Fails closed.

An argv is a READ only when its executable is a bare name from the table below, the shared classifier behind the `cli_guard`
hook (`de_agent.cliclass`) calls it a read, and its sub-command is in the narrower pack table here (packs run unattended, so
reads that write local files, mint presigned URLs or can print secrets are refused too). Everything else (an unknown CLI, an
unknown verb, a path-qualified executable) is refused. `shell=False` is enforced by the runner; this module decides whether the
argv may run at all.
"""
from __future__ import annotations

import re
from typing import Callable

from de_agent.cliclass import classify_cli

# flags that take a separate value, per CLI: they are skipped when looking for the sub-command words
_VALUE_FLAGS = {
    "kubectl": {"-n", "--namespace", "--context", "--kubeconfig", "--cluster", "--user", "--server", "-s",
                "--request-timeout", "-o", "--output", "-l", "--selector", "--field-selector", "-c", "--container",
                "--chunk-size", "--sort-by", "--tail", "--since"},
    "helm": {"-n", "--namespace", "--kube-context", "--kubeconfig", "-o", "--output", "--max", "--filter"},
    "gcloud": {"--project", "--format", "--filter", "--limit", "--region", "--location", "--locations", "--zone",
               "--sort-by", "--page-size", "--created-after", "--status", "--configuration", "--billing-project"},
    "gsutil": {"-p", "-o"},
    "bq": {"--project_id", "--location", "--format", "--max_rows", "-n", "--dataset_id"},
    "databricks": {"-p", "--profile", "-o", "--output", "--limit", "--job-id", "--run-id", "--start-time-from",
                   "--start-time-to", "--page-token", "--offset", "--expand-tasks"},
    "aws": {"--profile", "--region", "--output", "--query", "--endpoint-url", "--max-items", "--page-size",
            "--filters", "--cli-read-timeout", "--cli-connect-timeout"},
    "az": {"--subscription", "-g", "--resource-group", "-o", "--output", "--query", "-n", "--name"},
    "terraform": {"-chdir"},
    "tofu": {"-chdir"},
    "airflow": {"-o", "--output", "-d", "--dag-id", "-s", "--start-date", "-e", "--end-date", "--state"},
    "dbt": {"--profiles-dir", "--project-dir", "--target", "-t", "--select", "-s", "--exclude", "--vars"},
    "snow": {"-c", "--connection", "--format", "--role", "--warehouse"},
}

# flags that redirect credentials to another endpoint, load config / keys from elsewhere, write files or carry a secret on the
# command line (any CLI; `=` and separate forms). `--profile`, `--context` and `--target` stay allowed: they pick among the
# user's own configured credentials (values must be plain names, see _NAME).
_BANNED_FLAGS = frozenset({
    "--endpoint-url", "--server", "--token", "--password", "--username", "--client-key", "--client-certificate",
    "--kubeconfig", "--kube-apiserver", "--kube-token", "--kube-as-user", "--kube-as-group", "--kube-ca-file", "--as", "--as-group",
    "--certificate-authority", "--insecure-skip-tls-verify", "--host", "--account", "--private-key-file", "--private-key-path",
    "--token-file-path", "--impersonate-service-account", "--access-token-file", "--flags-file", "--log-http", "--endpoint",
    "--ca-bundle", "--no-verify-ssl", "--debug", "--profiles-dir", "--project-dir", "--log-file", "--diag-log-path",
    "--enable-diag", "--file", "--save", "--output-directory", "--repo"})
_NAME_FLAGS = ("--profile", "--context", "--kube-context")
_NAME = re.compile(r"[A-Za-z0-9._-]+")
_SENSITIVE = re.compile(r"secret|token|password|credential|decrypt|private-key|login-password|session-token|key-string", re.I)
_AZ_SENSITIVE = re.compile(_SENSITIVE.pattern + r"|\b(?:keys|shared-key|appsettings|connection-string|get-connection-info|sas|generate-sas|"
                           r"list-keys|list-secrets|access-token)\b", re.I)
_AWS_READ_PREFIX = ("describe-", "list-", "get-", "head-", "batch-get-", "lookup-")


def positionals(args: list[str], value_flags: set[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            out += args[i + 1:]
            break
        if a.startswith("-") and len(a) > 1:
            if "=" not in a and a in value_flags:
                i += 1
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def _kubectl(p: list[str], args: list[str]) -> str | None:
    if not p:
        return "no kubectl sub-command"
    if any(a == "-s" or (a.startswith("-s") and not a.startswith("--")) for a in args):
        return "kubectl -s (--server) would send the credentials to another endpoint"
    if p[0] in {"get", "describe", "logs", "top", "explain", "version", "api-resources", "api-versions", "cluster-info"}:
        if p[0] == "get" and any(a == "--raw" or a.startswith("--raw=") for a in args):
            return "kubectl get --raw can read any API path, secrets included"
        if p[:2] == ["cluster-info", "dump"]:
            return "kubectl cluster-info dump collects secrets-adjacent cluster state"
        if p[0] in {"get", "describe"} and any(re.fullmatch(r"secrets?([./].*)?", r) for t in p[1:] for r in t.lower().split(",")):
            return "kubectl get/describe secrets can return secret values"
        return None
    if p[0] == "config" and len(p) > 1 and p[1] in {"view", "get-contexts", "current-context"}:
        return "kubectl config view --raw/--flatten prints credentials" if any(a in ("--raw", "--flatten") for a in args) else None
    if p[:2] == ["auth", "can-i"]:
        return None
    return f"kubectl {p[0]} is not a read verb"


def _helm(p: list[str], args: list[str]) -> str | None:
    if p and p[0] == "show" and any("://" in a for a in p[1:]):
        return "helm show of a chart URL fetches from another host"
    return None if p and p[0] in {"list", "ls", "status", "history", "version", "show"} else f"helm {p[0] if p else ''} is not a read verb"


_GCLOUD_READ = {"list", "describe", "ls"}
_GCLOUD_WRITE = {
    "create", "delete", "update", "deploy", "run", "submit", "start", "stop", "restart", "reset", "resize", "cancel",
    "drain", "import", "export", "copy", "cp", "mv", "rm", "rsync", "patch", "attach", "detach", "ssh", "scp", "execute",
    "set-iam-policy", "add-iam-policy-binding", "remove-iam-policy-binding", "access", "login", "print-access-token",
    "print-identity-token", "revoke", "activate-service-account", "set", "unset", "add", "remove", "apply", "invoke", "call", "pull",
}


def _gcloud(p: list[str], args: list[str]) -> str | None:
    # `gcloud <group> [<subgroup>] <verb> [RESOURCE]`: the verb is the first verb-like word, at most 4th
    if not p:
        return "no gcloud sub-command"
    for i, w in enumerate(p[:4]):
        if w in _GCLOUD_WRITE or _SENSITIVE.search(w):
            return f"gcloud {w} is not a read verb"
        if w in _GCLOUD_READ or w.startswith("get-"):
            return None
        if p[i:i + 2] == ["logs", "read"] or p[:2] in (["config", "list"], ["auth", "list"]):
            return None
    return f"gcloud {' '.join(p[:3])}: no recognised read verb"


def _gsutil(p: list[str], args: list[str]) -> str | None:
    return None if p and p[0] in {"ls", "stat", "du"} else "gsutil: only ls, stat and du are read verbs"


def _bq(p: list[str], args: list[str]) -> str | None:
    return None if p and p[0] in {"ls", "show", "head"} else "bq: only ls, show and head are read verbs (SQL goes through a sql pack)"


def _databricks(p: list[str], args: list[str]) -> str | None:
    # `databricks <group> <verb> [ID]`: the verb is the second word
    if p and p[0] in {"api", "secrets", "tokens", "token-management"}:
        return f"databricks {p[0]} can call any endpoint or return secrets and tokens"
    if len(p) >= 2 and p[1] in {"list", "get", "list-runs", "get-run", "export", "ls", "me"} and not _SENSITIVE.search(" ".join(p[:2])):
        return None
    if p[:2] == ["fs", "cat"]:
        return None
    return f"databricks {' '.join(p[:2])} is not a read verb"


_AWS_DENY = [
    ("s3api", r"get-object-torrent"),
    ("lambda", r"get-function|get-function-configuration|get-layer-version.*"),
    ("ecr", r"get-download-url-for-layer|get-login.*|get-authorization-token"),
    ("ecr-public", r"get-authorization-token"),
    ("eks", r"get-token"),
    ("athena", r"get-session-endpoint"),
    ("quicksight", r"get-.*embed-url"),
    ("apigateway", r"get-api-keys?"),
    ("lightsail", r"get-instance-access-details|get-relational-database-master-user-password"),
    ("appsync", r"list-api-keys"),
    ("sts", r"get-session-token|get-federation-token|assume-role.*"),
    ("redshift", r"get-cluster-credentials.*"),
    ("redshift-serverless", r"get-credentials"),
    ("rds", r"generate-db-auth-token"),
    ("secretsmanager", r".*"),
    ("ssm", r"get-parameter.*"),
    ("kms", r"decrypt"),
]
_AWS_SECRET_FLAGS = {"--include-value", "--include-values", "--with-decryption"}


def _aws(p: list[str], args: list[str]) -> str | None:
    if len(p) < 2:
        return "aws: need a service and an operation"
    svc, op = p[0], p[1]
    if _SENSITIVE.search(op) or any(a.split("=", 1)[0] in _AWS_SECRET_FLAGS for a in args):
        return f"aws {svc} {op} can return secrets"
    if any(s == svc and re.fullmatch(rx, op) for s, rx in _AWS_DENY) or (
            svc == "glue" and op in ("get-connection", "get-connections") and "--hide-password" not in args):
        return f"aws {svc} {op} can return credentials, code locations or secrets"
    if svc == "s3api" and op == "get-object" and p[-1] not in ("-", "/dev/stdout"):
        return "aws s3api get-object writes a local file unless the outfile is - or /dev/stdout"
    if svc == "s3" and op == "ls":
        return None
    if op.startswith(_AWS_READ_PREFIX):
        return None
    return f"aws {svc} {op} is not a read operation"


def _az(p: list[str], args: list[str]) -> str | None:
    if p and (p[-1] in {"list", "show", "query"} or p[-1].startswith("get-")) and not _AZ_SENSITIVE.search(" ".join(p)) \
            and "keyvault" not in p:
        return None
    return f"az {' '.join(p)} is not a read verb"


def _terraform(p: list[str], args: list[str]) -> str | None:
    names = {a.lstrip("-").split("=", 1)[0] for a in args if a.startswith("-")}
    if "out" in names or any(n.startswith("generate-config-out") for n in names):
        return "terraform -out / -generate-config-out writes a file"
    if "show-sensitive" in names:
        return "terraform -show-sensitive prints sensitive values"
    if p and p[0] in {"output", "show"} and "json" in names:
        return f"terraform {p[0]} -json prints sensitive values"
    if p[:2] in (["providers", "mirror"], ["providers", "lock"]):
        return f"terraform providers {p[1]} writes files"
    if p and p[0] in {"plan", "show", "validate", "output", "providers", "version"}:
        if any(a == "-out" or a.startswith("-out=") for a in args):
            return "terraform plan -out writes a file"
        return None if p[0] != "output" or "-raw" not in args else "terraform output -raw can print a secret"
    if p[:2] in (["state", "list"], ["state", "show"]):
        return None
    return "terraform: only plan, show, validate, output, providers, version, state list|show are read"


_AF_SECRET_KEY = re.compile(r"fernet_key|secret_key|sql_alchemy_conn|password|smtp_password|broker_url|result_backend|connection|token|key", re.I)


def _airflow(p: list[str], args: list[str]) -> str | None:
    if any(a.startswith("-S") or a == "--subdir" or a.startswith("--subdir=") for a in args):
        return "airflow -S/--subdir loads DAG code from another path"
    if p[:1] == ["connections"]:
        return "airflow connections can print connection passwords and URIs"
    if p[:2] == ["config", "get-value"] and any(_AF_SECRET_KEY.search(w) for w in p[2:4]):
        return "airflow config get-value of a secret-looking key"
    if p[:1] == ["info"] and "--file-io" in args:
        return "airflow info --file-io uploads environment info"
    ok = {("dags", v) for v in ("list", "show", "state", "list-runs", "list-jobs", "report", "details")}
    ok |= {("tasks", v) for v in ("list", "state", "states-for-dag-run")}
    ok |= {("config", "get-value"), ("config", "list")}
    if tuple(p[:2]) in ok or p == ["version"]:
        return None
    return f"airflow {' '.join(p[:2])} is not a read command"


def _dbt(p: list[str], args: list[str]) -> str | None:
    if any(a.startswith("--inline") for a in args):
        return "dbt --inline runs SQL given on the command line"
    if any(a == "--vars" or a.startswith("--vars=") for a in args):
        return "dbt --vars can steer macros that run SQL"
    return None if p and p[0] in {"parse", "compile", "ls", "list", "debug"} else "dbt: only parse, compile, ls, debug are read"


def _snow(p: list[str], args: list[str]) -> str | None:
    return None if p[:2] in (["object", "list"], ["object", "describe"]) else "snow: only `object list|describe` is read here"


_RULES: dict[str, Callable[[list[str], list[str]], str | None]] = {
    "kubectl": _kubectl, "helm": _helm, "gcloud": _gcloud, "gsutil": _gsutil, "bq": _bq, "databricks": _databricks,
    "aws": _aws, "az": _az, "terraform": _terraform, "tofu": _terraform, "airflow": _airflow, "dbt": _dbt, "snow": _snow,
}
KNOWN_CLIS = tuple(sorted(_RULES))


def refusal(argv: list[str]) -> str | None:
    """None when the argv is a recognised read-only command, else the reason it is refused."""
    if not argv or not all(isinstance(a, str) for a in argv):
        return "argv must be a non-empty list of strings"
    if any("\x00" in a or "\n" in a for a in argv):
        return "argv elements may not contain newlines or NUL"
    exe = argv[0]
    if "/" in exe or "\\" in exe:
        return f"{exe!r}: the executable must be a bare command name (resolved on PATH)"
    rule = _RULES.get(exe)
    if rule is None:
        return f"{exe!r} is not a recognised read-only CLI (known: {', '.join(KNOWN_CLIS)})"
    args = argv[1:]
    for i, a in enumerate(args):
        name, eq, val = a.partition("=")
        if name in _BANNED_FLAGS:
            return f"flag {name} redirects credentials, loads config from elsewhere, writes a file or carries a secret"
        if name in _NAME_FLAGS and not _NAME.fullmatch(val if eq else (args[i + 1] if i + 1 < len(args) else "")):
            return f"{name} must be a plain profile or context name ([A-Za-z0-9._-]+)"
    shared = classify_cli(exe, args)
    if shared is None or shared.kind != "read":
        return f"{' '.join(argv[:3])}: {shared.kind if shared else 'not classified'} for the CLI guard (packs run reads only)"
    return rule(positionals(args, _VALUE_FLAGS.get(exe, set())), args)
