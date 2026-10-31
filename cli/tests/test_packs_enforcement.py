"""Read-only enforcement is in code for all three pack kinds: a pack with a write SQL, a mutating argv, a POST mutation or an
http DELETE is rejected BEFORE anything executes (SQLAlchemy engine, subprocess and the HTTP opener are mocked and asserted
untouched), and a pack that fails `pack check` never runs."""
import json
import subprocess
from datetime import datetime, timezone
from unittest import mock

import pytest
from de_agent.collectors import _pack_http_core as core
from de_agent.collectors import pack_cli, pack_http, pack_sql
from de_agent.collectors.base import RunContext
from de_agent.config import parse_config
from de_agent.packs import argv as argv_rules
from de_agent.packs import check_text, load_pack, parse_pack
from de_agent.packs.check import sql_refusal
from de_agent.packs.errors import PackError
from de_agent.runner import result_status, run_source
from packs_helpers import install

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def write_pack(tmp_path, text):
    hub = tmp_path / "hub"
    (hub / "packs").mkdir(parents=True, exist_ok=True)
    (hub / "packs" / "evil.yaml").write_text(text)
    return hub


@pytest.fixture(autouse=True)
def _reset_hint(tmp_path):
    from de_agent.packs.loader import set_hub_hint

    install(tmp_path)  # small generic test packs (pgtest, mytest, snowtest, fttest, dgtest)
    yield
    set_hub_hint(None)


def source(hub, kind, extra=""):
    (hub / "de-agent.yaml").write_text(
        {"sql": "version: 1\nsources:\n  - {name: s, type: sql, pack: evil, conn: {url: 'postgresql+psycopg://u@h/d'}}\n",
         "cli": "version: 1\nsources:\n  - {name: s, type: cli, pack: evil}\n",
         "http": "version: 1\nsources:\n  - {name: s, type: http, pack: evil, base_url: 'http://example.invalid', auth: none}\n"}[kind] + extra)
    from de_agent.config import load_config

    return load_config(hub / "de-agent.yaml").sources[0]


def sql_pack(sql):
    return f"pack: evil\nkind: sql\ndialect: postgres\nrequires: {{extra: pg}}\nsections:\n  - name: s\n    sql: {json.dumps(sql)}\n"


# ------------------------------------------------------------------------------------------------ SQL

WRITE_SQL = [
    "delete from orders",
    "insert into orders values (1)",
    "update orders set a = 1",
    "drop table orders",
    "truncate orders",
    "create table x as select 1",
    "alter table orders add column z int",
    "select 1; drop table orders",
    "select * into backup from orders",
    "with d as (delete from orders returning *) select * from d",
    "select pg_terminate_backend(123)",
    "select set_config('a', 'b', false)",
    "select nextval('seq')",
    "copy orders to '/tmp/x'",
    "grant all on orders to public",
    "vacuum orders",
    "call do_it()",
    "set role admin",
    "explain analyze delete from orders",
    "select * from orders for update",
    "this is not sql at all (",
    "",
]


@pytest.mark.parametrize("sql", WRITE_SQL)
def test_write_sql_is_refused_by_pack_check_and_never_runs(sql, tmp_path):
    pack, errs = check_text(sql_pack(sql))
    assert errs, sql
    hub = write_pack(tmp_path, sql_pack(sql))
    src = source(hub, "sql")
    with mock.patch("sqlalchemy.create_engine") as ce, mock.patch.object(pack_sql, "create_read_engine") as cre:
        res = run_source(src)
    assert result_status(res) == "failed"
    assert "failed `dea pack check`" in res["errors"][0]
    ce.assert_not_called()
    cre.assert_not_called()


@pytest.mark.parametrize("sql", [s for s in WRITE_SQL if s])
def test_write_sql_is_refused_at_runtime_even_if_check_is_bypassed(sql):
    """The per-statement classification runs again right before execution (defense in depth)."""
    pack = parse_pack(sql_pack("select 1"))
    sec = pack.sections[0].model_copy(update={"sql": sql})
    coll = pack_sql.SqlPackCollector()
    state = mock.Mock()
    with pytest.raises(PackError, match="refused"):
        coll.run_section(state, mock.Mock(), RunContext(timeout_seconds=60), pack, sec, None, {}, NOW)
    state.query.assert_not_called()


def test_a_select_runs_inside_a_read_only_transaction_with_a_timeout(monkeypatch):
    calls = []

    class FakeResult:
        returns_rows = True

        def mappings(self):
            return self

        def fetchmany(self, n):
            calls.append(("fetchmany", n))
            return [{"ok": 1}]

    class FakeConn:
        def execution_options(self, **kw):
            calls.append(("isolation", kw["isolation_level"]))
            return self

        def exec_driver_sql(self, sql):
            calls.append(("raw", sql))

        def execute(self, stmt, binds):
            calls.append(("execute", str(stmt), dict(binds)))
            return FakeResult()

        def close(self):
            calls.append(("close",))

    class FakeEngine:
        def connect(self):
            return FakeConn()

        def dispose(self):
            pass

    monkeypatch.setattr(pack_sql, "create_read_engine", lambda *a, **k: FakeEngine())
    src = parse_config("sources:\n  - {name: s, type: sql, pack: pgtest, conn: {url: 'postgresql+psycopg://u@h/d'}}\n").sources[0]
    pack = load_pack("pgtest")
    sess = pack_sql.SqlSession(src, pack, 30)
    rows = sess.query("select 1 as ok", {}, {}, 5)
    sess.close()
    assert rows == [{"ok": 1}]
    raw = [c[1] for c in calls if c[0] == "raw"]
    assert raw == ["BEGIN READ ONLY", "SET LOCAL statement_timeout = 30000", "SET LOCAL lock_timeout = 3000", "ROLLBACK"]
    assert calls[0] == ("isolation", "AUTOCOMMIT")
    order = [c[0] for c in calls]
    assert order.index("raw") < order.index("execute") < len(order) - 1


def test_mysql_uses_start_transaction_read_only():
    fam = pack_sql.FAMILIES["mysql"]
    assert fam["begin"] == "START TRANSACTION READ ONLY" and "max_execution_time" in fam["timeout"][0]
    assert pack_sql.FAMILIES["postgres"]["begin"] == "BEGIN READ ONLY"


def test_mssql_url_gets_a_read_intent_route():
    pytest.importorskip("sqlalchemy")
    u = pack_sql.build_url("mssql+pyodbc://u@host/db?driver=ODBC+Driver+18+for+SQL+Server", "pw")
    assert u.query["ApplicationIntent"] == "ReadOnly" and u.password == "pw"


def test_sql_url_dialect_must_match_the_pack():
    with pytest.raises(Exception, match="pack mytest is for mysql"):
        parse_config("sources:\n  - {name: s, type: sql, pack: mytest, conn: {url: 'postgresql+psycopg://u@h/d'}}\n")


def test_sql_sources_run_locally_only():
    with pytest.raises(Exception, match="run locally"):
        parse_config("sources:\n  - {name: s, type: sql, pack: mytest, exec: 'ssh:bastion', conn: {url: 'mysql+pymysql://u@h/d'}}\n")


def test_missing_driver_gives_the_install_hint(monkeypatch):
    src = parse_config("sources:\n  - {name: s, type: sql, pack: snowtest, conn: {url: 'snowflake://u@acct/db'}, "
                       "options: {warehouses: [W]}}\n").sources[0]
    import builtins

    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "sqlalchemy":
            raise ImportError("no sqlalchemy", name="sqlalchemy")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    res = run_source(src)
    assert "pip install 'de-agent[snowflake]'" in res["errors"][0]


def test_sql_classifier_accepts_placeholders_and_reads():
    assert sql_refusal("select * from t where a in :xs and b = :y", "postgres", {"xs": [1], "y": 1}) is None
    assert sql_refusal("select 1 from `region-{{r}}`.INFORMATION_SCHEMA.JOBS", "bigquery", {}) is None


SIDE_EFFECT_SQL = [
    ("postgres", "select query_to_xml('delete from t', true, true, '')"),
    ("postgres", "select query_to_xmlschema('select 1', true, true, '')"),
    ("postgres", "select cursor_to_xml('c'::refcursor, 1, true, false, '')"),
    ("redshift", "select * from dblink('host=x', 'select 1') as t(a int)"),
    ("postgres", "select pg_stat_statements_reset()"),
    ("postgres", "select pg_stat_reset_shared('bgwriter')"),
    ("postgres", "select lo_import('/etc/passwd')"),
    ("postgres", "select lo_export(1, '/tmp/x')"),
    ("postgres", "select lo_unlink(1)"),
    ("postgres", "select lo_from_bytea(0, 'x')"),
    ("postgres", "select lo_put(1, 0, 'x')"),
    ("postgres", "select pg_logical_emit_message(true, 'a', 'b')"),
    ("postgres", "select pg_notify('a', 'b')"),
    ("postgres", "select set_config('a', 'b', false)"),
    ("postgres", "select pg_cancel_backend(1)"),
    ("postgres", "select pg_reload_conf()"),
    ("postgres", "select pg_rotate_logfile()"),
    ("postgres", "select pg_switch_wal()"),
    ("postgres", "select setval('s', 1)"),
    ("postgres", "SELECT PG_TERMINATE_BACKEND(1)"),
    ("snowflake", "select system$abort_session(1)"),
    ("snowflake", "select system$cancel_all_queries(1)"),
    ("databricks", "select http_request('http://x')"),
    ("spark", "select java_method('java.lang.Runtime', 'getRuntime')"),
    ("spark", "select reflect('java.lang.Math', 'abs', 1)"),
    ("databricks", "select secret('scope', 'key')"),
    ("databricks", "select * from remote_query('c', 'select 1')"),
    ("bigquery", "select * from external_query('c', 'select 1')"),
    ("tsql", "select * from openquery(srv, 'select 1')"),
    ("tsql", "select * from openrowset('a', 'b', 'c')"),
    ("tsql", "select * from opendatasource('a', 'b').db.dbo.t"),
    ("tsql", "select next value for seq1"),
    ("tsql", "select 1 delete from t"),
    ("tsql", "select 1\ndelete from t"),
    ("sqlite", "select writefile('/tmp/x', 'y')"),
    ("sqlite", "select load_extension('x')"),
    ("duckdb", "select edit('x')"),
    ("mysql", "select 1 /*! ; drop table t */"),
    ("mysql", "/*!50000 select 1 */"),
    ("mariadb", "select 1 /*M!100100 ; drop table t */"),
]


@pytest.mark.parametrize("dialect,sql", SIDE_EFFECT_SQL, ids=lambda v: v[:40] if isinstance(v, str) else v)
def test_side_effecting_functions_and_hidden_statements_are_refused(dialect, sql):
    from de_agent.sqlclass import classify

    assert sql_refusal(sql, dialect, {}) is not None
    assert any(s.kind == "write" for s in classify(sql, dialect))


@pytest.mark.parametrize("dialect,sql", [
    ("snowflake", "select system$typeof(1)"),
    ("snowflake", "select system$clustering_information('t')"),
    ("tsql", "select a as [delete] from t"),
    ("tsql", "select a, b from t where c = 1"),
    ("mysql", "select 1 /* plain comment */"),
    ("sqlite", "select abs(-1)"),
    ("postgres", "select now(), count(*) from t"),
], ids=lambda v: v[:40] if isinstance(v, str) else v)
def test_ordinary_reads_stay_allowed_next_to_the_denylist(dialect, sql):
    assert sql_refusal(sql, dialect, {}) is None


# ------------------------------------------------------------------------------------------------ CLI

MUTATING_ARGV = [
    ["kubectl", "delete", "pod", "x"],
    ["kubectl", "apply", "-f", "x.yaml"],
    ["kubectl", "exec", "pod", "--", "rm", "-rf", "/"],
    ["kubectl", "scale", "deploy/x", "--replicas=0"],
    ["kubectl", "rollout", "restart", "deploy/x"],
    ["kubectl", "--context", "prod", "delete", "ns", "x"],
    ["aws", "ec2", "terminate-instances", "--instance-ids", "i-1"],
    ["aws", "s3", "rm", "s3://b/k"],
    ["aws", "s3", "cp", "x", "s3://b/k"],
    ["aws", "secretsmanager", "get-secret-value", "--secret-id", "x"],
    ["aws", "ssm", "get-parameter", "--name", "x", "--with-decryption"],
    ["aws", "lambda", "invoke", "--function-name", "x", "out.json"],
    ["gcloud", "compute", "instances", "delete", "vm"],
    ["gcloud", "composer", "environments", "update", "e"],
    ["gcloud", "secrets", "versions", "access", "latest", "--secret", "s"],
    ["gcloud", "auth", "print-access-token"],
    ["gcloud", "storage", "rm", "gs://b/x"],
    ["databricks", "jobs", "run-now", "--job-id", "1"],
    ["databricks", "jobs", "delete", "1"],
    ["databricks", "clusters", "delete", "abc"],
    ["databricks", "fs", "rm", "dbfs:/x"],
    ["databricks", "auth", "token"],
    ["helm", "uninstall", "rel"],
    ["helm", "upgrade", "rel", "chart"],
    ["terraform", "apply", "-auto-approve"],
    ["terraform", "destroy"],
    ["terraform", "state", "rm", "x"],
    ["airflow", "dags", "trigger", "d"],
    ["airflow", "db", "reset"],
    ["dbt", "run"],
    ["az", "vm", "delete", "-n", "x"],
    ["bq", "rm", "-f", "ds.t"],
    ["bq", "query", "select 1"],
    ["snow", "sql", "-q", "drop table t"],
    ["rm", "-rf", "/"],
    ["sh", "-c", "kubectl get pods; rm -rf /"],
    ["bash", "-c", "echo hi"],
    ["env", "kubectl", "get", "pods"],
    ["sudo", "kubectl", "get", "pods"],
    ["curl", "-X", "DELETE", "http://x"],
    ["python3", "-c", "import os"],
    ["/usr/bin/kubectl", "get", "pods"],
    ["./kubectl", "get", "pods"],
    ["kubectl", "get", "secrets", "-n", "x", "-o", "json"],
    ["kubectl", "get", "secret/db", "-o", "yaml"],
    ["kubectl", "describe", "secret", "db"],
    ["kubectl", "get", "pods,secrets"],
    ["kubectl", "config", "view", "--raw"],
    ["kubectl", "-s", "http://evil", "get", "pods"],
    ["kubectl", "get", "pods", "--server=http://evil"],
    ["kubectl", "get", "pods", "--token", "abc"],
    ["aws", "ec2", "describe-instances", "--endpoint-url", "http://evil"],
    ["terraform", "plan", "-out=x.tfplan"],
    ["az", "keyvault", "secret", "show", "--name", "x"],
    ["az", "keyvault", "secret", "list"],
    ["kubectl"],
    ["unknown-cli", "list"],
    [],
]

# ---- v0.2 red-team round D: banned flags, secret-bearing operations, per-CLI holes (items 28-37)
MUTATING_ARGV += [
    # 28: flags that redirect credentials, load config from elsewhere, or write files
    ["kubectl", "get", "pods", "--kubeconfig", "/tmp/evil"],
    ["kubectl", "get", "pods", "--kubeconfig=/tmp/evil"],
    ["helm", "list", "--kube-apiserver", "http://evil"],
    ["helm", "list", "--kube-token", "t"],
    ["helm", "list", "--kube-as-user", "root"],
    ["helm", "list", "--kube-as-group", "g"],
    ["helm", "list", "--kube-ca-file", "/x"],
    ["kubectl", "get", "pods", "--as", "root"],
    ["kubectl", "get", "pods", "--as-group", "g"],
    ["kubectl", "get", "pods", "--certificate-authority", "/x"],
    ["kubectl", "get", "pods", "--insecure-skip-tls-verify"],
    ["kubectl", "get", "pods", "-shttp://evil"],
    ["kubectl", "get", "pods", "-s=http://evil"],
    ["databricks", "jobs", "list", "--host", "http://evil"],
    ["gcloud", "compute", "instances", "list", "--account", "evil@example.invalid"],
    ["gcloud", "compute", "instances", "list", "--impersonate-service-account", "sa@example.invalid"],
    ["gcloud", "compute", "instances", "list", "--access-token-file", "/x"],
    ["gcloud", "compute", "instances", "list", "--flags-file", "/x"],
    ["gcloud", "compute", "instances", "list", "--log-http"],
    ["snow", "object", "list", "--private-key-path", "/x"],
    ["snow", "object", "list", "--private-key-file", "/x"],
    ["snow", "object", "list", "--diag-log-path", "/x"],
    ["snow", "object", "list", "--enable-diag"],
    ["databricks", "jobs", "list", "--token-file-path", "/x"],
    ["aws", "ec2", "describe-instances", "--endpoint", "http://evil"],
    ["aws", "ec2", "describe-instances", "--ca-bundle", "/x"],
    ["aws", "ec2", "describe-instances", "--no-verify-ssl"],
    ["aws", "ec2", "describe-instances", "--debug"],
    ["dbt", "ls", "--profiles-dir", "/x"],
    ["dbt", "ls", "--project-dir", "/x"],
    ["dbt", "ls", "--log-file", "x"],
    ["databricks", "workspace", "export", "/x", "--file", "/tmp/y"],
    ["airflow", "dags", "show", "d", "--save", "/tmp/x.png"],
    ["databricks", "jobs", "list", "--output-directory", "x"],
    ["helm", "show", "values", "chart", "--repo", "http://evil"],
    ["kubectl", "--context", "a b", "get", "pods"],
    ["kubectl", "--context=a;rm", "get", "pods"],
    ["aws", "--profile", "$(id)", "ec2", "describe-instances"],
    ["aws", "--profile=a/b", "ec2", "describe-instances"],
    # 29: aws operations that return credentials, code or secrets
    ["aws", "s3api", "get-object", "--bucket", "b", "--key", "k", "out.csv"],
    ["aws", "s3api", "get-object-torrent", "--bucket", "b", "--key", "k", "out"],
    ["aws", "lambda", "get-function", "--function-name", "f"],
    ["aws", "lambda", "get-function-configuration", "--function-name", "f"],
    ["aws", "lambda", "get-layer-version", "--layer-name", "l", "--version-number", "1"],
    ["aws", "lambda", "get-layer-version-by-arn", "--arn", "x"],
    ["aws", "ecr", "get-download-url-for-layer", "--repository-name", "r", "--layer-digest", "d"],
    ["aws", "ecr", "get-login-password"],
    ["aws", "ecr", "get-login"],
    ["aws", "ecr", "get-authorization-token"],
    ["aws", "eks", "get-token", "--cluster-name", "c"],
    ["aws", "athena", "get-session-endpoint", "--workgroup", "w"],
    ["aws", "quicksight", "get-dashboard-embed-url", "--aws-account-id", "1", "--dashboard-id", "d"],
    ["aws", "quicksight", "get-session-embed-url", "--aws-account-id", "1"],
    ["aws", "apigateway", "get-api-key", "--api-key", "k"],
    ["aws", "apigateway", "get-api-keys"],
    ["aws", "apigateway", "get-api-keys", "--include-values"],
    ["aws", "glue", "get-connection", "--name", "c"],
    ["aws", "glue", "get-connections"],
    ["aws", "lightsail", "get-instance-access-details", "--instance-name", "i"],
    ["aws", "appsync", "list-api-keys", "--api-id", "a"],
    ["aws", "sts", "get-session-token"],
    ["aws", "sts", "assume-role", "--role-arn", "x", "--role-session-name", "s"],
    ["aws", "sts", "assume-role-with-web-identity", "--role-arn", "x"],
    ["aws", "redshift", "get-cluster-credentials", "--db-user", "u"],
    ["aws", "redshift", "get-cluster-credentials-with-iam"],
    ["aws", "rds", "generate-db-auth-token", "--hostname", "h"],
    ["aws", "secretsmanager", "list-secrets"],
    ["aws", "secretsmanager", "describe-secret", "--secret-id", "s"],
    ["aws", "ssm", "get-parameter", "--name", "n"],
    ["aws", "ssm", "get-parameters-by-path", "--path", "/"],
    ["aws", "ssm", "get-parameter-history", "--name", "n"],
    ["aws", "kms", "decrypt", "--ciphertext-blob", "x"],
    ["aws", "ec2", "describe-instances", "--include-value"],
    ["aws", "ec2", "describe-instances", "--with-decryption"],
    # 30: gcloud verb position and credential-minting commands
    ["gcloud", "functions", "call", "get-user"],
    ["gcloud", "pubsub", "subscriptions", "pull", "get-sub"],
    ["gcloud", "pubsub", "subscriptions", "pull", "--auto-ack", "show"],
    ["gcloud", "services", "api-keys", "get-key-string", "k"],
    ["gcloud", "auth", "print-identity-token"],
    ["gcloud", "auth", "application-default", "print-access-token"],
    ["gcloud", "container", "clusters", "get-credentials", "c"],
    # 31: az secret-bearing command words
    ["az", "storage", "account", "keys", "list", "-n", "a"],
    ["az", "storage", "account", "show-connection-string", "-n", "a"],
    ["az", "webapp", "config", "appsettings", "list", "-n", "a"],
    ["az", "webapp", "config", "connection-string", "list", "-n", "a"],
    ["az", "acr", "credential", "show", "-n", "r"],
    ["az", "cosmosdb", "keys", "list", "-n", "c"],
    ["az", "redis", "list-keys", "-n", "r"],
    ["az", "account", "get-access-token"],
    ["az", "functionapp", "keys", "list", "-n", "f"],
    ["az", "storage", "blob", "generate-sas", "-n", "b"],
    ["az", "sql", "db", "show-connection-string", "-n", "d"],
    # 32: terraform output that prints secrets or writes files
    ["terraform", "output", "-json"],
    ["terraform", "show", "-json"],
    ["terraform", "show", "-json", "plan.tfplan"],
    ["terraform", "state", "show", "-show-sensitive", "aws_x.y"],
    ["terraform", "plan", "-generate-config-out=gen.tf"],
    ["terraform", "plan", "-generate-config-out", "gen.tf"],
    ["terraform", "providers", "mirror", "/tmp/m"],
    ["terraform", "providers", "lock"],
    ["terraform", "plan", "-out", "x.tfplan"],
    ["terraform", "state", "show", "-out=x", "a.b"],
    # 33: airflow secrets and file writes
    ["airflow", "config", "get-value", "core", "fernet_key"],
    ["airflow", "config", "get-value", "database", "sql_alchemy_conn"],
    ["airflow", "config", "get-value", "smtp", "smtp_password"],
    ["airflow", "config", "get-value", "celery", "broker_url"],
    ["airflow", "config", "get-value", "celery", "result_backend"],
    ["airflow", "config", "get-value", "webserver", "secret_key"],
    ["airflow", "config", "get-value", "core", "some_token"],
    ["airflow", "connections", "get", "c"],
    ["airflow", "connections", "export", "/tmp/c.json"],
    ["airflow", "connections", "list"],
    ["airflow", "info", "--file-io"],
    ["airflow", "dags", "list", "-S", "/x"],
    ["airflow", "dags", "list", "--subdir", "/x"],
    ["airflow", "dags", "list", "-S/x"],
    # 34: databricks
    ["databricks", "api", "get", "/api/2.0/secrets/list"],
    ["databricks", "api", "post", "/api/2.0/x"],
    ["databricks", "secrets", "list-secrets", "scope"],
    ["databricks", "secrets", "list-scopes"],
    ["databricks", "tokens", "list"],
    ["databricks", "token-management", "list"],
    # 35: kubectl
    ["kubectl", "get", "--raw", "/api/v1/namespaces/x/secrets"],
    ["kubectl", "get", "--raw=/api"],
    ["kubectl", "config", "view", "--flatten"],
    ["kubectl", "cluster-info", "dump"],
    ["kubectl", "get", "pods", "secret/db"],
    ["kubectl", "get", "secrets", "-o", "jsonpath={.items}"],
    ["kubectl", "get", "-o", "yaml", "secrets"],
    # 36: dbt
    ["dbt", "compile", "--inline", "select 1"],
    ["dbt", "compile", "--inline-direct", "select 1"],
    ["dbt", "debug", "--vars", "{a: b}"],
    ["dbt", "compile", "--vars", "a: b"],
    ["dbt", "ls", "--vars=a: 1"],
    ["dbt", "run-operation", "x"],
    # 37: helm
    ["helm", "show", "values", "https://evil.example/chart.tgz"],
    ["helm", "show", "chart", "oci://evil.example/c"],
    ["helm", "list", "--repo", "http://evil"],
]


@pytest.mark.parametrize("av", MUTATING_ARGV, ids=lambda a: " ".join(a)[:50] or "empty")
def test_mutating_argv_is_refused(av):
    assert argv_rules.refusal(av), av


READ_ARGV = [
    ["kubectl", "get", "pods", "-n", "shop", "-o", "json"],
    ["kubectl", "--context", "x", "get", "jobs"],
    ["kubectl", "describe", "pod", "p"],
    ["kubectl", "config", "view"],
    ["aws", "glue", "get-jobs"],
    ["aws", "dms", "describe-replication-tasks"],
    ["aws", "s3", "ls", "s3://b/"],
    ["gcloud", "pubsub", "subscriptions", "list", "--project", "p", "--format", "json"],
    ["gcloud", "storage", "objects", "list", "gs://b/x", "--format", "json"],
    ["gcloud", "composer", "environments", "describe", "e", "--location", "l"],
    ["databricks", "jobs", "list-runs", "--job-id", "1", "-o", "json"],
    ["databricks", "clusters", "get", "abc", "-o", "json"],
    ["helm", "list", "-A"],
    ["terraform", "plan", "-lock=false"],
    ["terraform", "state", "list"],
    ["airflow", "dags", "list"],
    ["dbt", "ls"],
    ["az", "vm", "list"],
    ["bq", "ls"],
]

READ_ARGV += [
    ["kubectl", "--context", "minikube", "get", "pods"],
    ["kubectl", "--context=my-ctx.1", "get", "pods", "-n", "shop"],
    ["aws", "--profile", "acme-prod", "ec2", "describe-instances"],
    ["aws", "--profile=acme_prod.eu", "ec2", "describe-instances"],
    ["aws", "s3api", "get-object", "--bucket", "b", "--key", "k", "-"],
    ["aws", "s3api", "get-object", "--bucket", "b", "--key", "k", "/dev/stdout"],
    ["aws", "s3api", "head-object", "--bucket", "b", "--key", "k"],
    ["aws", "sts", "get-caller-identity"],
    ["aws", "lambda", "list-functions"],
    ["aws", "ec2", "describe-instances", "--region", "eu-west-1"],
    ["dbt", "compile"],
    ["dbt", "debug"],
    ["dbt", "ls", "--select", "tag:daily"],
    ["helm", "show", "values", "bitnami/nginx"],
    ["terraform", "output"],
    ["terraform", "show"],
    ["terraform", "plan", "-lock=false"],
    ["terraform", "state", "show", "aws_x.y"],
    ["airflow", "config", "get-value", "core", "dags_folder"],
    ["airflow", "dags", "list", "-o", "json"],
    ["databricks", "workspace", "export", "/Shared/x/nb"],
    ["databricks", "jobs", "list", "-o", "json"],
    ["kubectl", "cluster-info"],
    ["kubectl", "get", "pods", "-n", "shop"],
    ["az", "storage", "account", "list"],
    ["gcloud", "functions", "list"],
    ["gcloud", "pubsub", "subscriptions", "describe", "s", "--project", "p"],
]


@pytest.mark.parametrize("av", READ_ARGV, ids=lambda a: " ".join(a)[:50])
def test_read_argv_is_allowed(av):
    assert argv_rules.refusal(av) is None, av


def cli_pack(run):
    return f"pack: evil\nkind: cli\nrequires: {{cli: {run[0]}}}\nsections:\n  - name: s\n    run: {json.dumps(run)}\n"


@pytest.mark.parametrize("av", [a for a in MUTATING_ARGV if a and a[0] in ("kubectl", "aws", "gcloud", "databricks", "helm", "terraform", "airflow", "dbt", "az", "bq", "snow")],
                         ids=lambda a: " ".join(a)[:50])
def test_mutating_argv_in_a_pack_fails_check_and_subprocess_is_never_called(av, tmp_path):
    _, errs = check_text(cli_pack(av))
    assert errs
    hub = write_pack(tmp_path, cli_pack(av))
    src = source(hub, "cli")
    with mock.patch.object(subprocess, "run") as run, mock.patch.object(subprocess, "Popen") as popen:
        res = run_source(src)
    assert result_status(res) == "failed" and "failed `dea pack check`" in res["errors"][0]
    run.assert_not_called()
    popen.assert_not_called()


@pytest.mark.parametrize("av", [a for a in MUTATING_ARGV if a], ids=lambda a: " ".join(a)[:50])
def test_mutating_argv_is_refused_at_runtime_even_if_check_is_bypassed(av):
    pack = parse_pack(cli_pack(["kubectl", "get", "pods"]))
    sec = pack.sections[0].model_copy(update={"run": av})
    with mock.patch.object(subprocess, "run") as run, mock.patch.object(subprocess, "Popen") as popen:
        with pytest.raises(PackError, match="refused"):
            pack_cli.CliPackCollector().run_section(None, mock.Mock(exec="local"), RunContext(timeout_seconds=60), pack, sec, None, {}, NOW)
    run.assert_not_called()
    popen.assert_not_called()


def test_a_placeholder_cannot_become_the_verb_or_a_flag(tmp_path):
    _, errs = check_text("pack: evil\nkind: cli\nrequires: {cli: kubectl}\nparams:\n  v: {type: str, default: get}\nsections:\n  - name: s\n    run: [kubectl, '{{v}}', pods]\n")
    assert errs
    from de_agent.packs import template

    with pytest.raises(PackError, match="not allowed"):
        template.resolve_params({"ns": parse_pack(cli_pack(["kubectl", "get"]) + "params:\n  ns: {type: list}\n").params["ns"]}, {"ns": ["--all-namespaces"]})
    with pytest.raises(PackError):
        template.resolve_params({"ns": parse_pack(cli_pack(["kubectl", "get"]) + "params:\n  ns: {type: list}\n").params["ns"]}, {"ns": ["a; rm -rf /"]})
    with pytest.raises(PackError):
        template.resolve_params({"ns": parse_pack(cli_pack(["kubectl", "get"]) + "params:\n  ns: {type: list}\n").params["ns"]}, {"ns": ["$(id)"]})


def test_cli_runs_with_shell_false_and_an_argv_list():
    pack = parse_pack(cli_pack(["kubectl", "get", "pods", "-o", "json"]))
    done = subprocess.CompletedProcess(["kubectl"], 0, stdout='{"items": []}', stderr="")
    with mock.patch.object(subprocess, "run", return_value=done) as run:
        rows = pack_cli.CliPackCollector().run_section(None, mock.Mock(exec="local"), RunContext(timeout_seconds=60), pack,
                                                       pack.sections[0].model_copy(update={"select": "items"}), None, {}, NOW)
    assert rows == []
    args, kwargs = run.call_args
    assert args[0] == ["kubectl", "get", "pods", "-o", "json"] and kwargs["shell"] is False and kwargs["stdin"] == subprocess.DEVNULL


def test_ssh_argv_quotes_every_argument_after_double_dash():
    av = pack_cli.ssh_argv("bastion", ["kubectl", "get", "pods", "-n", "a b", "-l", "x=y;rm -rf /"])
    i = av.index("--")
    assert av[i - 1] == "bastion" and av[0] == "ssh"
    assert av[i + 1:] == ["kubectl", "get", "pods", "-n", "'a b'", "-l", "'x=y;rm -rf /'"]


def test_cli_non_json_output_is_an_error_not_a_crash():
    pack = parse_pack(cli_pack(["kubectl", "get", "pods"]))
    done = subprocess.CompletedProcess(["kubectl"], 0, stdout="NAME READY\npod 1/1", stderr="")
    with mock.patch.object(subprocess, "run", return_value=done):
        with pytest.raises(Exception, match="not JSON"):
            pack_cli.CliPackCollector().run_section(None, mock.Mock(exec="local"), RunContext(timeout_seconds=60), pack, pack.sections[0], None, {}, NOW)


# ------------------------------------------------------------------------------------------------ HTTP

def http_pack(section):
    return "pack: evil\nkind: http\nsections:\n  - name: s\n" + section


@pytest.mark.parametrize("section", [
    "    get: https://evil.example/steal\n",
    "    get: //evil.example/x\n",
    "    get: /a/../../etc/passwd\n",
    "    get: v1/no-leading-slash\n",
    "    get: '/a b'\n",
    "    graphql: 'mutation { deleteAll }'\n",
    "    graphql: 'query { a } mutation { b }'\n",
    "    graphql: 'subscription { a }'\n",
    "    graphql: '  Mutation'\n",
    "    graphql: |\n      query Q { a } # fine\n      mutation M { b }\n",
    "    graphql: 'fragment F on T { a }'\n",
    "    graphql: ''\n",
    "    get: /ok\n    method: DELETE\n",
    "    get: /ok\n    graphql: 'query { a }'\n",
    "    delete: /ok\n",
    "    post: /ok\n",
])
def test_bad_http_sections_fail_pack_check_and_no_request_is_made(section, tmp_path):
    _, errs = check_text(http_pack(section))
    assert errs, section
    hub = write_pack(tmp_path, http_pack(section))
    src = source(hub, "http")
    with mock.patch.object(core, "_OPENER") as opener:
        res = run_source(src)
    assert result_status(res) == "failed"
    opener.open.assert_not_called()


@pytest.mark.parametrize("method", ["DELETE", "PUT", "PATCH", "HEAD", "OPTIONS", "TRACE", "CONNECT", "get", "POST"])
def test_core_request_refuses_every_method_but_get_and_graphql_post(method):
    with mock.patch.object(core, "_OPENER") as opener:
        with pytest.raises(ValueError):
            core.request("http://example.invalid", method, "/x", {}, 5)  # POST without a graphql query is refused too
    opener.open.assert_not_called()


@pytest.mark.parametrize("query", ["mutation { x }", "subscription { x }", "query { a } mutation { b }", "{ a } mutation { b }", "", None])
def test_core_request_refuses_graphql_writes(query):
    with mock.patch.object(core, "_OPENER") as opener:
        with pytest.raises(ValueError):
            core.request("http://example.invalid", "POST", "/graphql", {}, 5, query)
    opener.open.assert_not_called()


def test_core_request_refuses_get_with_a_body_and_off_host_paths():
    with mock.patch.object(core, "_OPENER") as opener:
        for args in (("GET", "/x", None, "query { a }"), ("GET", "http://evil/x", None, None), ("GET", "//evil/x", None, None)):
            with pytest.raises(ValueError):
                core.request("http://example.invalid", args[0], args[1], {}, 5, args[3])
    opener.open.assert_not_called()


def test_graphql_strings_and_comments_do_not_hide_or_fake_a_mutation():
    assert core.graphql_refusal('query { a(x: "mutation") }') is None
    assert core.graphql_refusal("query { a } # mutation here") is None
    assert core.graphql_refusal('query { a(x: """subscription\nmutation""") }') is None
    assert core.graphql_refusal("{ a }") is None
    assert core.graphql_refusal("mutation { a }")
    assert core.graphql_refusal("query { a }\nmutation { b }")


@pytest.mark.parametrize("query", [
    '{ a }\n# """\nmutation { x }\n# """',
    'query A { a }\n# """\nmutation M { x }\n# """',
    '{ a }\n# "\nmutation { x }',
    'query { a(x: "# not a comment") }\nmutation { b }',
    'query { a(x: "\\"") } mutation { b }',
    'query { a(x: """has # and \\""" inside""") } subscription { s }',
    "{ a } # c\nmutation { b }",
])
def test_graphql_comments_and_strings_cannot_hide_a_mutation_from_each_other(query):
    assert core.graphql_refusal(query), query


@pytest.mark.parametrize("query", [
    "query { a } # mutation { b }",
    'query { a(x: "mutation { b }") }',
    'query { a(x: """\nmutation { b }\n""") }',
    '# """\nquery { a }',
    "query Q($x: Int) { a(x: $x) } # trailing # hash",
])
def test_graphql_reads_with_comments_and_strings_stay_allowed(query):
    assert core.graphql_refusal(query) is None, query


def test_http_collector_refuses_a_graphql_mutation_at_runtime_even_if_check_is_bypassed():
    pack = parse_pack(http_pack("    get: /ok\n"))
    sec = pack.sections[0].model_copy(update={"get": None, "graphql": "mutation { deleteEverything }"})
    with mock.patch.object(core, "_OPENER") as opener:
        with pytest.raises(PackError, match="refused"):
            pack_http.HttpPackCollector().run_section(None, mock.Mock(exec="local", base_url="http://example.invalid"), RunContext(timeout_seconds=60), pack, sec, None, {}, NOW)
    opener.open.assert_not_called()


def test_http_collector_sends_only_get_for_get_sections():
    pack = parse_pack(http_pack("    get: /v1/things\n    select: items\n"))
    resp = mock.MagicMock()
    resp.read.return_value = b'{"items": [{"id": 1}]}'
    resp.status = 200
    with mock.patch.object(core, "_OPENER") as opener:
        opener.open.return_value = resp
        rows = pack_http.HttpPackCollector().run_section(
            None, parse_config("sources:\n  - {name: s, type: http, pack: dgtest, base_url: 'http://example.invalid', auth: none}\n").sources[0],
            RunContext(timeout_seconds=60), pack, pack.sections[0], None, {}, NOW)
    assert rows == [{"id": 1}]
    req = opener.open.call_args[0][0]
    assert req.get_method() == "GET" and req.data is None


# ------------------------------------------------------------------------------------------------ misc: a failing pack never runs

def test_pack_that_fails_check_never_runs_even_with_an_injected_executor(tmp_path):
    called = []
    hub = write_pack(tmp_path, sql_pack("delete from t"))
    src = source(hub, "sql")
    from de_agent.collectors import get_collector

    out = get_collector("sql").collect(src, RunContext(timeout_seconds=60, pack_fn=lambda *a: called.append(a) or []))
    assert out["data"] == {} and out["errors"] and not called


# ------------------------------------------------------------------------------------------------ the pack file itself

def test_yaml_alias_bombs_and_huge_packs_are_rejected_fast():
    import time

    bomb = "pack: evil\nkind: sql\ndialect: postgres\nx: &a [1,1,1,1,1,1,1,1,1]\n"
    for i in range(10):
        bomb += f"y{i}: &b{i} [*a, *a, *a, *a, *a, *a, *a, *a, *a]\n"
    t0 = time.monotonic()
    _, errs = check_text(bomb)
    assert errs and "anchors and aliases" in errs[0] and time.monotonic() - t0 < 1.0
    _, errs = check_text("pack: evil\nkind: sql\ndialect: postgres\ndescription: " + "x" * 300_000 + "\n")
    assert errs and "limited to" in errs[0]
    _, errs = check_text("pack: evil\nkind: !!python/object/apply:os.system ['id']\n")  # safe_load refuses python tags
    assert errs


def test_pack_clis_are_all_guarded_clis():
    from de_agent import cliclass
    for exe in argv_rules.KNOWN_CLIS:
        assert cliclass.classify_cli(exe, ["--version"]) is not None, exe


@pytest.mark.parametrize("av", READ_ARGV, ids=lambda a: " ".join(a)[:50])
def test_every_pack_read_is_a_cli_guard_read(av):
    from de_agent import cliclass
    assert cliclass.classify_cli(av[0], av[1:]).kind == "read", av


@pytest.mark.parametrize("av", [
    ["aws", "s3", "cp", "s3://b/x.csv", "./x.csv"],
    ["aws", "s3", "sync", "s3://b/", "./raw"],
    ["aws", "s3", "presign", "s3://b/x.csv"],
    ["aws", "s3", "rm", "s3://b/x.csv"],
    ["kubectl", "delete", "pod", "p"],
    ["kubectl", "exec", "p", "--", "ls"],
    ["terraform", "apply", "-auto-approve"],
    ["dbt", "run"],
], ids=lambda a: " ".join(a)[:50])
def test_guard_reads_that_write_local_files_and_guard_writes_are_refused_for_packs(av):
    assert argv_rules.refusal(av), av
