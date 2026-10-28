# Safety model

de-agent lets an agent look at live data systems and, only through a reviewed plan, change them. This page lists the
guard layers, what each one stops, and what none of them can stop.

## The layers

| # | layer | where | what it stops |
|---|---|---|---|
| 0 | Read-only database roles | your databases (documented, not enforced by this repo) | Everything: the agent's reads cannot write. This is the PRIMARY control. Everything below is defence in depth. |
| 1 | `dea query` | CLI | Writes: classified with the SQL classifier first (`sqlclass`, fails closed), then run inside `BEGIN READ ONLY` with statement and lock timeouts and a row cap. Refuses writable sources. |
| 2 | `sql_guard` (PreToolUse hook) | `scripts/sql_guard.py` | SQL writes in the clients listed under "SQL clients" below (`psql`, `mysql`/`mariadb`, `snowsql`, `sqlcmd`, `clickhouse-client`, `sqlite3`, `duckdb`, `trino`/`presto`, `spark-sql`, `beeline`, `bq`, `aws redshift-data`, `aws athena`, `dea query`), including when wrapped by `xargs`, `find -exec`, `env`, `sudo`, `bash -c`; Python scripts that import a database driver (heuristic, see below); and ANY MCP tool: every string in the tool input, at any depth and under any key, that is SQL is classified. A write is allowed only for `agent_type == de-agent:executor` AND a hash listed in an `.approved` file whose HMAC verifies. Unparseable SQL counts as a write. Malformed hook input is a deny. |
| 3 | `secret_guard` (PreToolUse hook) | `scripts/secret_guard.py` | Paths are resolved with `realpath` first (symlinks, `~`, `..`). Commands that print the environment (`printenv`, bare `env`, bare `set`, `export -p`, `declare -p/-x`, `compgen -v`, `echo`/`printf` of `$...PASS/SECRET/TOKEN/KEY...` variables); `ln`, `cp -s`, `dd if=`, `gpg -d`, editors (vi, vim, nvim, nano, emacs, ed, ex, micro) and Python `open()`/`read_text()` on a credential path. Inline passwords and DSN secrets in commands, reads of `~/.pgpass`, `.env`, `*.pem`, `~/.aws/credentials`, `~/.aws/config`, cached SSO tokens, and the approval key (`approve.key`), via Bash, `Read` and `Grep` (the Grep tool's `path`, `glob`, `include` and `type` are all checked, and a secret-looking `pattern` such as `password|token|api_key` is denied where credential files are in scope); `aws configure get aws_secret_access_key`, `aws configure export-credentials`, `aws sts get-session-token`, `aws secretsmanager get-secret-value`. |
| 4 | `approval_guard` (PreToolUse hook) | `scripts/approval_guard.py` | `dea approve` from any agent (including the main session), any Bash command mentioning `approve.key` or `.approved`, Write/Edit/MultiEdit/NotebookEdit on `.dea/plans/*.approved` or the key, and `dea apply` from anything but `de-agent:executor`. |
| 4b | `test_guard` (PreToolUse hook) | `scripts/test_guard.py` | Inside a dbt project (an ancestor has `dbt_project.yml`), for every agent: Edit/Write/MultiEdit of a `*.yml` that leaves fewer `tests:` / `data_tests:` entries than before or adds `severity: warn`, `enabled: false`, `where:` or a loosened `warn_if` / `error_if`; emptying a singular test file under `test-paths`; Bash `rm`, `git rm`, `mv`, `sed -i`, `truncate`, `tee` or `> file` aimed at those files. Adding or tightening tests is allowed; the way to change a wrong test is a plan with a `diff` step. `dea apply` is unaffected. |
| 4c | `self_guard` (PreToolUse hook) | `scripts/self_guard.py` | The guards guarding themselves, for every agent: Write/Edit/MultiEdit/NotebookEdit and Bash writes (`sed -i`, `>`, `>>`, `tee`, `cp`, `mv`, `rm`, `chmod`, `ln`, `install`, `dd of=`, `truncate`, `rsync`, `perl/ruby -i`) that target `hooks/`, `scripts/`, `.claude-plugin/`, `agents/` or `skills/` under `$CLAUDE_PLUGIN_ROOT`; edits to a hub's `de-agent.yaml` that change the `guard:` mode or set or alter any `writable:` value (before and after are compared; adding sources and scans stays allowed; Bash writes to that file are denied, use Edit); edits to `.claude/settings*.json` that change `hooks` or `disableAllHooks`. Maintainers developing the plugin itself start Claude with `DEA_ALLOW_PLUGIN_EDITS=1` in their own shell (an agent cannot set the hook's environment). |
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

A client run with SQL on a pipe (`echo ... | mysql`), from a shell variable, or from a file that cannot be read is denied as
unreadable. A client name after a package manager (`pip install duckdb`) is an argument, not a program being run.
`xargs`, `find -exec`, `awk`, `sed`, `env`, `sudo`, `time` and `docker exec` are execution wrappers: a client anywhere after
them is parsed as run. Only text tools that never execute their arguments (`echo`, `grep`, `cat`, `head`, ...) are skipped.

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

## Size cap

Before anything is parsed, `sql_guard` (and the other guards that tokenise a command) deny input that is too large to
check: a Bash command or MCP tool input over 200 KB, or one extracted SQL string over 100 KB. Worst case measured under the
cap on `/usr/bin/python3` 3.9: 1.7 s for the whole hook (a 98 KB `a+a+...` chain; 99 KB of `select 1;` statements took 1.3 s),
under the 5 s target. Pathologically nested SQL fails closed (a deny), never a crash or an allow.

## Guarding the guards

An agent that cannot write SQL could still try to switch the guards off. `self_guard` denies, for every agent, editing the
plugin's own `hooks/`, `scripts/`, `.claude-plugin/`, `agents/` and `skills/` (checked on the realpath under
`$CLAUDE_PLUGIN_ROOT`, also through `cd`, `$CLAUDE_PLUGIN_ROOT` and `..`), changing a hub's `guard:` mode or any `writable:`
value, and changing `hooks` or `disableAllHooks` in `.claude/settings*.json`. Ordinary `de-agent.yaml` edits (a new source or
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
- shell obfuscation that hides `dea approve`, a key path, a SQL client or a protected plugin path from pattern matching (the hooks match command text; variables, `eval`, base64 and similar indirection are out of scope);
- an attacker who controls your home directory, your terminal, or the hook configuration itself;
- a database role that can write: that is why layer 0 comes first.

Hook denials hold even when the session runs in a permissive permission mode, but they only apply to tool calls Claude
Code routes through hooks. Treat the MAC key like any local secret.
