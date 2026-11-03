# Safety model

## In short

- The real wall is the read-only database login: with it, the agent's reads cannot change anything.
- Hooks add defence in depth: they deny writes, secret reads, self-approval, weakening tests and editing the guards themselves.
- A change happens only through a plan that a human signs in their own terminal (`dea approve`); the agent cannot sign.
- This stops accidents and casual bypass. It is not a sandbox: a determined attacker with shell access is out of scope.
- The detailed layer-by-layer table is below.

de-agent lets an agent look at live data systems and, only through a reviewed plan, change them. This page lists the
guard layers, what each one stops, and what none of them can stop.

## The layers

| # | layer | where | what it stops |
|---|---|---|---|
| 0 | Read-only database roles | your databases (set up per system in [read-only-access.md](read-only-access.md); not enforced by this repo) | Everything: the agent's reads cannot write. This is the PRIMARY control. Everything below is defence in depth. |
| 1 | `dea query` | CLI | Writes: classified with the SQL classifier first (`sqlclass`, fails closed), then run inside `BEGIN READ ONLY` with statement and lock timeouts and a row cap. Refuses writable sources. |
| 1b | Pack read-only enforcement | `cli/src/de_agent/collectors/pack_*.py` | Sources of `type: sql`, `cli` and `http` run YAML packs. A pack that fails `dea pack check` never runs. SQL: every statement is classified as exactly one read in the pack's dialect before any connection is made, then runs in a read-only transaction with timeouts. CLI: the argv must be a recognised read command of a known CLI (no shell, no wrappers, no path-qualified executables) before any process starts; `shell=False`. HTTP: only GET and read-only GraphQL queries, enforced in the one function that opens a socket; no redirects, 5 MB cap. See [packs.md](packs.md). |
| 2 | `sql_guard` (PreToolUse hook) | `scripts/sql_guard.py` | SQL writes in the clients listed under "SQL clients" below (`psql`, `mysql`/`mariadb`, `snowsql`, `sqlcmd`, `clickhouse-client`, `sqlite3`, `duckdb`, `trino`/`presto`, `spark-sql`, `beeline`, `bq`, `aws redshift-data`, `aws athena`, `dea query`), including when wrapped by `xargs`, `find -exec`, `env`, `sudo`, `bash -c`; Python scripts that import a database driver (heuristic, see below); and ANY MCP tool: every string in the tool input, at any depth and under any key, that is SQL is classified. A write is allowed only for `agent_type == de-agent:executor` AND a hash listed in an `.approved` file whose HMAC verifies. Unparseable SQL counts as a write. Malformed hook input is a deny. |
| 2b | `cli_guard` (PreToolUse hook) | `scripts/cli_guard.py` | Writes through the cloud and data CLIs, for every agent including the executor (the executor runs approved shell steps through `dea apply`, which runs them itself, not through the Bash tool): `aws`, `gcloud`, `gsutil`, `bq`, `databricks`, `kubectl`, `helm`, `terraform`/`tofu`, `pulumi`, `az`, `snow`, `dbt`, `airflow`, `dagster`, `prefect`, `kafka-topics`, `kafka-configs`. Each is classified as read or write from its subcommand; a write is denied, and so is a verb of a known CLI that is not on its read list (fail closed). Follows `env`, `sudo`, `bash -c`, `xargs`, `find -exec`, `timeout`, `nice`, `uv run`, `poetry run`, `ssh`, `docker exec`/`run`, `docker compose exec`/`run` and `kubectl exec --`. Details and the verb tables: "CLI write guard" below. |
| 3 | `secret_guard` (PreToolUse hook) | `scripts/secret_guard.py` | Paths are resolved with `realpath` first (symlinks, `~`, `..`). Commands that print the environment (`printenv`, bare `env`, bare `set`, `export -p`, `declare -p/-x`, `compgen -v`, `echo`/`printf` of `$...PASS/SECRET/TOKEN/KEY...` variables); `ln`, `cp -s`, `dd if=`, `gpg -d`, editors (vi, vim, nvim, nano, emacs, ed, ex, micro) and Python `open()`/`read_text()` on a credential path. Inline passwords and DSN secrets in commands, reads of `~/.pgpass`, `.env`, `*.pem`, `~/.aws/credentials`, `~/.aws/config`, cached SSO tokens, and the approval key (`approve.key`), via Bash, `Read` and `Grep` (the Grep tool's `path`, `glob`, `include` and `type` are all checked, and a secret-looking `pattern` such as `password|token|api_key` is denied where credential files are in scope); `aws configure get aws_secret_access_key`, `aws configure export-credentials`, `aws sts get-session-token`, `aws secretsmanager get-secret-value`. |
| 4 | `approval_guard` (PreToolUse hook) | `scripts/approval_guard.py` | `dea approve` from any agent (including the main session), any Bash command mentioning `approve.key` or `.approved`, Write/Edit/MultiEdit/NotebookEdit on `.dea/plans/*.approved` or the key, and `dea apply` from anything but `de-agent:executor`. |
| 4b | `test_guard` (PreToolUse hook) | `scripts/test_guard.py` | Inside a dbt project (an ancestor has `dbt_project.yml`), for every agent: Edit/Write/MultiEdit of a `*.yml` that leaves fewer `tests:` / `data_tests:` entries than before or adds `severity: warn`, `enabled: false`, `where:` or a loosened `warn_if` / `error_if`; emptying a singular test file under `test-paths`; Bash `rm`, `git rm`, `mv`, `sed -i`, `truncate`, `tee` or `> file` aimed at those files. Adding or tightening tests is allowed; the way to change a wrong test is a plan with a `diff` step. `dea apply` is unaffected. |
| 4c | `self_guard` (PreToolUse hook) | `scripts/self_guard.py` | The guards guarding themselves, for every agent: Write/Edit/MultiEdit/NotebookEdit and Bash writes (`sed -i`, `>`, `>>`, `tee`, `cp`, `mv`, `rm`, `chmod`, `ln`, `install`, `dd of=`, `truncate`, `rsync`, `perl/ruby -i`) that target `hooks/`, `scripts/`, `.claude-plugin/`, `agents/` or `skills/` under `$CLAUDE_PLUGIN_ROOT`; edits to a hub's `de-agent.yaml` that change the `guard:` mode, the `guard.cli_allow` list or set or alter any `writable:` value (before and after are compared; adding sources and scans stays allowed; Bash writes to that file are denied, use Edit); edits to `.claude/settings*.json` that change `hooks` or `disableAllHooks`. Maintainers developing the plugin itself start Claude with `DEA_ALLOW_PLUGIN_EDITS=1` in their own shell (an agent cannot set the hook's environment). |
| 5 | `dea approve` | CLI | Needs an interactive TTY (no flag, no env override). Prints the full plan check, requires the human to type the plan id, then writes `.dea/plans/<id>.approved`: the statement hashes, approver (`git config user.email` or `$USER`), a timestamp and an HMAC-SHA256 over all of it. The key lives in `$XDG_CONFIG_HOME/de-agent/approve.key` (default `~/.config/de-agent/approve.key`), created 0600 on first use. |
| 6 | `dea apply` | CLI | Verifies the MAC, re-hashes every statement in the plan file (a `diff` step is hashed over the exact patch bytes `git apply` receives, CRLF normalised to LF only; approvals made with the older whitespace-stripped hash still verify only when the two texts are identical) and aborts before running anything if one is not in the approved set (so editing a plan after approval voids it). Runs steps in order, one transaction per step, with the plan's statement timeout; runs each step's verify blocks and stops on the first failure. Writes `.dea/plans/<id>.applied.json`. |
| 7 | `stop_check` (Stop hook) | `scripts/stop_check.py` | Not a guard: it makes the agent re-run `dea snapshot` and confirm the fix in `dea diff` after a plan was applied (blocks the stop once per plan). |

AWS collectors add their own guard inside the CLI (not a hook): every boto3 session carries a botocore `before-call`
handler that raises before any network call unless the operation is `Describe*`, `List*`, `Get*`, `BatchGet*` or `Head*`
(data-reading `Get` operations such as `GetObject` are refused); the Redshift Data API is allowed only for SQL that
classifies as a read. The policy in `docs/aws-iam-readonly.json` is the minimal IAM grant for exactly those calls and is
layer 0 for AWS: give the profile or instance role nothing more. Its credential statement carries placeholder ARNs to fill in. Plainly: `redshift-data:BatchExecuteStatement` is not read-only at the IAM level; on the Data API path read-only depends on the classifier, the session guard and a read-only database user, so use one. Details: [docs/collectors.md](collectors.md).

Write access is also separated by configuration: only sources marked `writable: true` in `de-agent.yaml` may receive
write statements from a plan; they are never snapshotted or used by `dea query`.

## SQL clients

One list drives both `sql_guard` (reads the SQL) and `secret_guard` (inline passwords): `cli/src/de_agent/clients.py`.
Dialects are per client.

| client | SQL read from | dialect |
|---|---|---|
| `psql`, `pgcli` (secrets only) | `-c`, `-f`, heredoc, herestring, `<`, pipe | postgres |
| `mysql`, `mariadb` | `-e` / `--execute` (also `-Ne`, `-e<sql>`), heredoc, herestring, `<` file, pipe | mysql |
| `snowsql` | `-q` / `--query`, `-f` / `--filename`, stdin forms | snowflake |
| `sqlcmd` | `-Q`, `-q`, `-i` / `--input-file`, stdin forms | tsql |
| `clickhouse-client`, `clickhouse client`, `clickhouse-local` | `-q` / `--query`, `--queries-file`, stdin forms | clickhouse |
| `sqlite3` | trailing SQL arguments, `-cmd`, `-init`, stdin forms, `.read FILE` (inlined); other dot-commands are denied unless read-only (`.tables`, `.schema`, ...) | sqlite |
| `duckdb` | `-c`, `-cmd`, `-s`, `-f`, `-init`, trailing arguments, stdin forms, `.read` | duckdb |
| `trino`, `presto` (and `-cli`) | `--execute`, `--file` / `-f`, stdin forms | trino, presto |
| `spark-sql` | `-e`, `-f`, `-i`, stdin forms | spark |
| `beeline` | `-e`, `-f`, stdin forms | hive |
| `bq`, `aws redshift-data`, `aws athena`, `dea query` | their query flags | bigquery, redshift, athena, postgres |

A client run with SQL on a pipe (`echo ... | mysql`), from stdin by another name (`psql -f /dev/stdin`, `-f /dev/fd/0`, `snow sql --stdin`),
from a shell variable, from a mysql `--defaults-file` / `--defaults-extra-file` (a config file can run SQL), or from a file that cannot be read is denied as
unreadable. `mysql --init-command` is SQL and is classified; `psql -o` / `-L` / `\o` / `\copy ... to` / `\g file` write a local file and are writes.
MySQL and MariaDB `/*! ... */` and `/*M! ... */` comments are refused (the server runs their content); T-SQL statements without a `;`
(`select 1 delete from t`, which parses as an alias) are refused when an unquoted identifier is a write keyword.

**Side-effecting functions.** A `SELECT` that calls a function that writes, reaches another server or changes the session is a write, in
the guard and in packs (same classifier): postgres/redshift `query_to_xml*`, `cursor_to_xml*`, `dblink*`, `pg_stat_reset*`,
`pg_stat_statements_reset`, `lo_import|export|unlink|from_bytea|put`, `pg_logical_emit_message`, `pg_notify`, `set_config`,
`pg_terminate_backend`, `pg_cancel_backend`, `pg_reload_conf`, `pg_rotate_logfile`, `pg_switch_wal`, `nextval`, `setval`; snowflake
any `system$...` except a short read allowlist (`system$clustering_information`, `system$typeof`, ...); databricks and spark
`http_request`, `java_method`, `reflect`, `secret`, `remote_query`; bigquery `external_query`; T-SQL `openquery`, `openrowset`,
`opendatasource`, `next value for`; sqlite and duckdb `writefile`, `load_extension`, `edit`. A client name after a package manager (`pip install duckdb`) is an argument, not a program being run.
`xargs`, `find -exec`, `awk`, `sed`, `env`, `sudo`, `time` and `docker exec` are execution wrappers: a client anywhere after
them is parsed as run. Only text tools that never execute their arguments (`echo`, `grep`, `cat`, `head`, ...) are skipped.

## CLI write guard

`scripts/cli_guard.py` is a second Bash hook next to `sql_guard`; the classifier is `cli/src/de_agent/cliclass.py` (stdlib, Python 3.9, shared
with the pack runner). It reads the command text, follows wrappers, and classifies every guarded CLI it finds:

- **read**: nothing is decided (the command runs);
- **write**: denied. The deny says to put the command in a plan as a shell step; `dea apply` runs approved shell steps itself, so it is unaffected;
- **unknown verb** of a known CLI: denied with "not a recognised read-only command; put it in a plan as a shell step". Only the read lists below are
  allowed, so a new or odd subcommand fails closed;
- **SQL-carrying** (`aws redshift-data execute-statement`, `aws athena start-query-execution`, `bq query`, `snow sql`, `databricks sql`, `dbsqlcli -e`,
  and `psql` & co inside `kubectl exec`): left to `sql_guard`, which classifies the statement. `cli_guard` only requires that the SQL can be read.

| CLI | read | write (denied) |
|---|---|---|
| `aws` | `describe-*`, `list-*`, `get-*`, `head-*`, `batch-get-*`, `lookup-*`, `wait`, `s3 ls`, `s3 cp\|sync` from `s3://` to a local path, `s3 presign`, `sts get-caller-identity`, `configure list\|get`, `logs tail\|filter-log-events`, `dynamodb query\|scan`; `redshift-data`/`athena` keep SQL handling | `start-*`, `stop-*`, `create-*`, `delete-*`, `update-*`, `put-*`, `modify-*`, `run-*`, `terminate-*`, `reboot-*`, `invoke`; `s3 cp\|sync` to `s3://`, `s3 mv\|rm\|mb\|rb`; any other verb is unknown (denied) |
| `gcloud`, `gsutil` | `list`, `describe`, `get-*`, `logs read`, `storage ls\|cat\|du`, `storage cp` out of `gs://`, `config list`, `auth list`, `version`; gsutil `ls`, `cat`, `du`, `stat`, `hash`, `cp` out of `gs://`, `acl\|iam get` | `create`, `delete`, `update`, `deploy`, `run`, `submit`, `set-*`, `add-*`, `remove-*`, `start`, `stop`, `publish`, `execute`, `storage cp` into `gs://`, `storage mv\|rm`, gsutil `cp` into `gs://`, `mv`, `rm`, `mb`, `rb`, `setmeta`, `rsync` into `gs://`, `acl\|iam set` |
| `bq` | `ls`, `show`, `head`, `version`, `wait`; `query` is SQL | `mk`, `rm`, `cp`, `load`, `insert`, `update`, `extract`, `cancel`, `partition`, `undelete` |
| `databricks` | `list*`, `get*`, `export`, `fs ls\|cat`, `fs cp` out of dbfs, `api get`, `bundle validate\|summary`, `version`; `sql` is SQL | `run-now`, `submit`, `create`, `delete`, `reset`, `cancel*`, `start`, `edit`, `fs rm\|mv\|mkdirs`, `fs cp` into dbfs, `bundle deploy\|run\|destroy`, `api post` |
| `kubectl` | `get`, `describe`, `logs`, `top`, `explain`, `version`, `api-resources`, `events`, `diff`, `wait`, `config view\|get-contexts\|current-context`, `auth can-i`, `rollout status\|history`, `cp` out of a pod | `apply`, `create`, `delete`, `edit`, `patch`, `replace`, `scale`, `rollout restart\|undo`, `cordon`, `drain`, `label`, `annotate`, `set`, `cp` into a pod, `port-forward`, `run`, `debug`; `exec` recurses (below) |
| `helm` | `list`, `status`, `get`, `history`, `show`, `template`, `lint`, `search`, `version`, `repo list` | `install`, `upgrade`, `uninstall`, `rollback` |
| `terraform`, `tofu` | `plan`, `show`, `validate`, `fmt -check`, `output`, `state list\|show\|pull`, `providers`, `version`, `init`, `graph`, `workspace list\|show` | `apply`, `destroy`, `import`, `refresh`, `fmt` (without `-check`), `state rm\|mv\|push`, `taint`, `untaint`, `force-unlock`, `workspace new\|select\|delete` |
| `pulumi` | `preview`, `stack ls\|output\|history\|export`, `config get`, `whoami`, `version` | `up`, `destroy`, `refresh`, `import`, `cancel`, `stack rm\|init\|select`, `config set\|rm` |
| `az` | `list`, `show`, `get-*`, `query`, `account show`, `rest` with GET, `download*` | `create`, `delete`, `update`, `set`, `start`, `stop`, `restart`, `run`, `invoke`, `upload*`, `rest` with another method |
| `snow` | `object list\|describe`; `sql` is SQL | everything else |
| `dbt` | `parse`, `compile`, `ls`/`list`, `docs generate`, `debug`, `deps`, `source freshness`, `test`, `show` | `run`, `build`, `seed`, `snapshot`, `run-operation`, `retry`, `clone`; `clean`, `init` and other verbs are unknown (denied) |
| `airflow` | `dags list\|show\|state\|list-runs\|list-jobs\|report\|details`, `tasks list\|state\|states-for-dag-run`, `version`, `config get-value\|list`, `connections list\|get`, `variables list\|get`, `pools list\|get` | `dags trigger\|pause\|unpause\|delete\|backfill\|reserialize\|test`, `tasks clear\|run\|test`, `db *`, `users *`, `variables set\|delete\|import`, `pools set\|delete`, `connections add\|delete\|import` |
| `dagster`, `prefect` | `list`, `ls`, `info`, `inspect`, `print`, `preview`, `logs`, `view` | `job launch\|execute`, `asset materialize`, `schedule\|sensor start\|stop`, `deployment run`, `flow-run cancel\|delete`, and the like |
| `kafka-topics`, `kafka-configs` | `--list`, `--describe` | `--create`, `--delete`, `--alter`, `--add-config` |
| `docker`, `docker compose` | not guarded itself: `exec` and `run` (and `docker-compose`, `podman`) are read through to the inner command | the inner command's verdict applies |

**Wrappers.** `env`, `sudo`, `nohup`, `time`, `timeout`, `nice`, `stdbuf`, `ionice`, `flock`, `watch`, `xargs`, `find -exec/-execdir/-ok`, `parallel`,
`uv run`/`uvx`/`uv tool run`, `poetry run`, `pipenv run`, `pdm run`, `hatch run`, `rye run`, `pipx run`, `conda run`, `bash`/`sh`/`zsh -c` (also a heredoc,
a herestring, and a script file `bash x.sh` / `source x.sh`, read up to 200 KB), `eval`, `ssh host cmd`, `$( ... )`, backticks and `<( ... )` are followed.
`kubectl exec pod -- cmd` classifies `cmd`: a SQL client inside goes to `sql_guard`, a guarded CLI inside gets its own verdict, anything else is denied.
`docker exec`/`run` and `docker compose exec`/`run` recurse into the inner command and leave other containers' commands alone (dev tooling stays
allowed). `python -c` / heredoc / script code that calls `subprocess` or `os.system` with a guarded CLI is read too (literal strings and argv lists).
Shell code piped into `bash` is denied when the command mentions a guarded CLI. A command that cannot be tokenised and mentions one is denied.

**Ask mode.** `guard: {mode: ask}` turns every deny of this hook into an ask (the same switch as `sql_guard`).

**`guard.cli_allow`.** A human may list exact command prefixes in `de-agent.yaml`:

```yaml
guard:
  mode: strict
  cli_allow:
    - dbt run --select staging
    - terraform apply -target=module.sandbox
```

An entry matches when the command's words, after wrappers and path stripping (`/usr/bin/dbt` is `dbt`), start with exactly the entry's words: `dbt run --select staging`
also allows `dbt run --select staging --target dev`, but not `dbt run --select staging_x` or `dbt run`. An entry needs the CLI and a verb (`dbt` alone is ignored) and
no shell operators (`;`, `&`, `|`, `$`, backticks, redirects); such entries are ignored and `dea` rejects them at load. Options typed before the verb are part of the prefix. The key is read
with a small stdlib parser (flow `[..]`, block list, or inside `guard: {..}`); keep entries free of `]` and `}`. `self_guard` denies any agent edit that changes the list,
like it does for the guard mode.

Beyond the CLIs themselves it follows a long list of wrappers: leading redirects (`>/dev/null aws ...`), container images whose entrypoint is a
guarded CLI (`docker run amazon/aws-cli s3 rm ...`, `hashicorp/terraform`, `bitnami/kubectl`, `dbt-labs/dbt-*`, `apache/airflow`; with no command
words they are denied as unknown), `python -m awscli|airflow|dbt|dagster|prefect`, `parallel`, `taskset`, `runuser`, `su -c`, `strace`, `chroot`, `nsenter`,
`unshare`, `busybox`, `systemd-run`, `script -c`, `tmux`, `screen`, `coproc`, `trap`, `at`/`batch`/`crontab`, shell functions, runners with global options
(`uv --directory . run`, `poetry -C . run`, ...), `ssh host -- cmd`, `ssh host sh -s <<< ...` and `ssh -o ProxyCommand=...`, direct execution of a readable shell
script under the working directory (`./s.sh`), and a program word built with `$IFS` after a guarded name (denied as unparseable).

### Known limits of cli_guard

It matches command text; it is not a sandbox. These are not seen:

- renamed or copied binaries and aliases (`cp $(which aws) x; ./x s3 rm ...`), and a program name that is a variable (`$CLI s3 rm ...`)
  beyond the `$IFS` form above;
- `eval` of a built string, base64 and similar encodings, a Makefile or a script that is not a shell file it can read;
- code that writes a script and runs it in a later, separate command (the same command line that writes and runs a script is denied);
- a hostile process that already runs inside the session.

A deny is a pattern match on the verb, not proof that nothing was written, and a read verb is not proof of a read (`aws lambda invoke` is denied, but a custom `get-*` on a
misconfigured API is only as safe as the API). Give the profile read-only grants: layer 0 again.

## Python scripts (heuristic)

`python[3] -c`, python heredocs and herestrings, and `python[3] file.py` (the file is read, up to 1 MB) are scanned when
the code imports a database driver: `psycopg`, `psycopg2`, `sqlalchemy`, `pymysql`, `MySQLdb`, `mysql.connector`,
`snowflake.connector`, `redshift_connector`, `pyodbc`, `pymssql`, `duckdb`, `sqlite3`, `clickhouse_driver`, `asyncpg`,
`pg8000`, `trino`, `pyhive`, or `boto3` with `execute_statement` / `redshift-data` / `start_query_execution`, or pandas
`read_sql` / `to_sql`. Then:

- string literals that start like SQL (also f-strings, `"a" + "b"`, `.format()`, `text(...)`, and names assigned a literal) are
  classified, and a write is denied;
- `execute(...)` (and `executemany`, `read_sql`, `execute_statement`, ...) with an argument that is not a literal is denied as
  unreadable (fail closed); `DataFrame.to_sql` is denied;
- string literals and argv lists that run a database client (`os.system("mysql -e ..")`, `subprocess.run(["psql", ...])`) are
  extracted like shell commands;
- `exec`/`eval`/`__import__`/`importlib.import_module` of non-literal code is denied; code piped to a bare `python` is denied;
- code with no driver, `python -m module`, and a script that does not exist are left alone.

Limits, stated plainly: it is a heuristic over source text. It does not follow `import helper` into local modules, does not see
a file written by one command and run by the next, cannot read SQL assembled from files or the network at run time, and
treats a driver that is called in an unrecognised way (an ORM `session.execute(delete(User))` is a non-literal call, so it is
denied; a custom wrapper that runs SQL with a literal is judged by the literal only) on a best-effort basis. Use a read-only role.

## Packs and the expression sandbox

Packs are declarative sources ([packs.md](packs.md)). Three properties are enforced in code, not by convention:

- **Read-only per kind.** A write SQL statement, a mutating CLI argv, a GraphQL `mutation` or `subscription`, and any HTTP method but GET (plus read-only GraphQL POST) are refused before any
  engine, subprocess or socket is touched; the same checks run again right before every execution. Tests mock the SQLAlchemy engine, `subprocess` and the HTTP opener and assert they are never called.
- **Values cannot become code.** SQL values are bind parameters; CLI and HTTP values are validated (no leading `-`, no spaces, quotes, `;` or `$`), substituted into a list argv or a relative
  path, never into a shell string; `shell=False` always; over ssh every argument is `shlex.quote`d. A source's secret is a reference (`env:`/`file:`), resolved at run time and, over ssh for HTTP, only on the remote host.
- **Rules are not code.** `when:` / `rows:` expressions run in a whitelist interpreter built on `ast` (no attribute access, no calls but `len`, `lower`, `age_h`, `age_d`, `now`, no lambdas or comprehensions),
  with size, depth, step, time and exponent limits. Hostile input (`__import__`, dunder attributes, huge exponents, deep nesting) is a pack error.

**Pack CLI argv** must be a `cli_guard` read first (the shared classifier gates packs since commit 7bcc9d9) and then pass the narrower pack table. Packs run unattended, so
the table also refuses what can mint or print credentials or write local files: the flags `--kubeconfig --kube-apiserver --kube-token --kube-as-user --kube-as-group --kube-ca-file --as
--as-group --certificate-authority --insecure-skip-tls-verify --host --account --private-key-file --private-key-path --token-file-path --impersonate-service-account --access-token-file
--flags-file --log-http --endpoint --endpoint-url --ca-bundle --no-verify-ssl --debug --profiles-dir --project-dir --log-file --diag-log-path --enable-diag --file --save
--output-directory --repo` and kubectl `-s<value>`; `--profile`, `--context` and `--kube-context` stay allowed but their values must be plain names (`[A-Za-z0-9._-]+`). Operations refused
include `aws s3api get-object` (except to `-` or `/dev/stdout`), `lambda get-function*`, `ecr get-login*`, `eks get-token`, `sts assume-role*`, `secretsmanager *`, `ssm get-parameter*`,
`kms decrypt`, `glue get-connection(s)` without `--hide-password`; gcloud `print-*-token`, `get-credentials`, `secrets versions access`, `api-keys get-key-string`; az `keys`, `appsettings`,
`connection-string`, `credential`, `sas`, `access-token`; terraform `-json` on `output`/`show`, `-show-sensitive`, `-generate-config-out`, `providers mirror|lock` and any `-out`; airflow
`connections *`, `config get-value` of secret-looking keys, `-S/--subdir`; databricks `api`, `secrets`, `tokens`; kubectl `get --raw`, `config view --flatten`, `cluster-info dump` and
`secret/...` anywhere in a `get`; dbt `--inline` and `--vars`; helm `show` of a URL. GraphQL documents are tokenised (comments and strings read left to right) before the
mutation / subscription check.

What packs do not give you: a Snowflake, BigQuery or Databricks pack has no read-only transaction to fall back on (classification and a read-only role are the controls), and a CLI read verb is only as safe as
the tool. Use least-privilege credentials for every source.

## Size cap

Before anything is parsed, `sql_guard` (and the other guards that tokenise a command) deny input that is too large to
check: a Bash command or MCP tool input over 200 KB, or one extracted SQL string over 100 KB. Worst case measured under the
cap on `/usr/bin/python3` 3.9: 1.7 s for the whole hook (a 98 KB `a+a+...` chain; 99 KB of `select 1;` statements took 1.3 s),
under the 5 s target. Pathologically nested SQL fails closed (a deny), never a crash or an allow.

## Guarding the guards

An agent that cannot write SQL could still try to switch the guards off. `self_guard` denies, for every agent, editing the
plugin's own `hooks/`, `scripts/`, `.claude-plugin/`, `agents/` and `skills/` (checked on the realpath under
`$CLAUDE_PLUGIN_ROOT`, also through `cd`, `$CLAUDE_PLUGIN_ROOT` and `..`) and its `cli/src` and `cli/pyproject.toml` (the classifiers the hooks import), running
`git -C $CLAUDE_PLUGIN_ROOT checkout|pull|reset|restore|stash|merge|rebase|apply`, `pip install --force-reinstall|-e ... de-agent` or `pip uninstall de-agent` (also `uv pip`), changing a hub's `guard:` mode, its
`guard.cli_allow` list or any `writable:` value, and changing `hooks` or `disableAllHooks` in `.claude/settings*.json`. A Bash command that mentions `de-agent.yaml` (or a glob that
matches it) together with an interpreter or tool that can write it (`python -c` that opens it for writing, `perl -e`, `node -e`, `ruby -e`, `awk -i inplace`, `ed`, `ex`, `patch`, `git apply|checkout|restore`, `yq -i`,
`sponge`, `vim -c`, `curl -o`, `wget -O`, a `sed` `w` command) is denied; reads (`cat`, `grep`, a Python `open()` without a write mode) are not. Known gap: `cp -r src/* .` where `src` holds a `de-agent.yaml`. Ordinary `de-agent.yaml` edits (a new source or
scan) are unaffected. A human edits those files in their own editor.

## Why the approval is a human action

A model must not be able to approve its own plan. Three things enforce that together: `dea approve` refuses without a TTY;
the hooks deny every Claude tool path to it (Bash `dea approve`, shell redirection into an `.approved` file, Write/Edit of
one, reading the key); and the file is HMAC-signed, so a hand-written or edited `.approved` file grants nothing. The deny
message tells the human to run `dea approve <id>` in their own terminal.

## Threat model (read this)

This design stops **accidents and casual bypass**: a model that writes SQL it should not, tries to approve its own plan,
edits a plan after approval, or pipes an id into `dea approve`. It is **not a sandbox against a hostile local process**.
Anything that runs as your user can read `approve.key`, run `dea approve` under a pty, or talk to the database directly.
Specifically, it does not stop:

- a process that bypasses Claude Code's hooks (another tool, a shell you open yourself);
- a program that reaches the database in a way the heuristics above do not read (a compiled binary, a script that imports a
  local helper which holds the SQL, code written by one command and run by the next);
- shell obfuscation that hides `dea approve`, a key path, a SQL client, a guarded CLI or a protected plugin path from pattern matching (the hooks match command text; variables, `eval`, base64 and similar indirection are out of scope);
- an attacker who controls your home directory, your terminal, or the hook configuration itself;
- a database role that can write: that is why layer 0 comes first.

Hook denials hold even when the session runs in a permissive permission mode, but they only apply to tool calls Claude
Code routes through hooks. Treat the MAC key like any local secret.
