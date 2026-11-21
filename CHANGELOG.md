# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.3] - 2026-11-21

Struck-through due items, DMS lineage from Terraform-managed tasks, README status. Patch release, no config changes.

### Fixed
- status.md / session hook: a dated list item with a partial struck span (`~~date ... thing~~ outcome`) was incorrectly
  counted as open. The done rule now matches any item whose text starts with `~~` and has a closing `~~` after the opening
  (meaning the date is inside the struck span). Whole-line strikes and checked tasks are unchanged.
- Terraform DMS scanner: `data.aws_dms_endpoint.<name>.<attr>` references are resolved by `Ctx.address()` and are no
  longer added to unresolved refs.
- Terraform DMS scanner: `replication_task_id = var.<x>` resolves to the variable's literal default; the catalog now
  shows `dms:<task-id>` instead of `dms:<resource-name>`.
- Terraform DMS scanner: schema/table `rename` transformation rules in `table_mappings` are applied to the target key
  for database endpoints, so a task mapping `api_backend.*` to `test_api_backend.*` shows the correct target schema.
- Terraform DMS scanner: when `table_mappings = jsonencode(local...)` cannot be statically resolved, the scanner falls
  back to the latest hub snapshot's `dms_table_mappings` section for that task id. Fallback pairs carry
  `via: live-snapshot:<ts>` and the catalog line notes `(via snapshot)`. When static mapping and snapshot are both
  available but disagree, a `mapping drift: <task>` warning is included in the scan result.
- README header said `Status: v0.2.4` since 0.2.4; it now matches the released version, and a release-metadata test keeps it in step.

## [0.3.2] - 2026-11-19

Hook false positives, DMS table mappings in the aws collector, and help keeping snapshots fresh. No config breaking changes.

### Added
- aws collector: section `dms_table_mappings`, one row per (task, source schema, source table) that a task's selection rules include,
  with `target_schema`/`target_table` after `rename`, `convert-lowercase`/`-uppercase`, `add-`/`remove-prefix`/`-suffix` rules, the
  rule that matched (`explicit` or `wildcard <schema>.<table>`; a wildcard matching nothing stays as one `(unresolved)` row) and the
  source/target endpoint ids. `dea diff` lists added and removed tables and raises `DMS mappings: +a / -r table(s) in <tasks>`; a
  target table written by two tasks is a warn signal. Capped by `options.dms_mappings_max` (default 5000). When a task's table
  statistics cannot be read the section is marked truncated and `dea diff` reports no removals from it.
- `dea export dms-mappings [--csv PATH] [--source NAME]`: the section from the latest snapshot as CSV. Read-only, no AWS call.
- `dea schedule print [--at HH:MM] [--sources a,b]`: prints a launchd plist (macOS) or crontab line that runs `dea snapshot` daily
  with `DEA_HUB` set and a rotated log in `<hub>/.dea/logs/`. Prints only; you install it. The session stale-snapshot row points to it.

### Fixed
- secret_guard: a heredoc body is data unless a shell reads it (`python3 - <<EOF ... set(...)` was refused as "prints shell
  variables"). Python bodies still go through the credential-file check; heredocs into `bash`/`sh`/`ssh` are unchanged.
- secret_guard: `password=` followed by a code reference (`cfg['password']`, `os.environ[...]`, `os.getenv(...)`) or a placeholder
  is no longer refused; the hook uses lint's exemption (one shared test table keeps them in step). Literals stay blocked.
- self_guard: interpreter code (`python3 - <<EOF`, `python -c`, `node -e`, ...) that writes and quotes a path under the plugin's
  hooks, scripts, agents, skills or `cli/src` is denied like an Edit (it went through before). Outside `DEA_ALLOW_PLUGIN_EDITS=1`
  sessions, a writing script run from the plugin checkout that merely reads a protected path is also refused (fails closed).
- self_guard: a python command that only mentions `de-agent.yaml` in prose is no longer refused; it must name it as a path (argument
  or quoted path literal).
- test_guard: a glob such as `*.yml` in script text no longer counts as a dbt test because some dbt project sits below the folder;
  script-text paths must be inside a dbt project.

## [0.3.1] - 2026-11-10

One hub for every session: fixes found when a single hub serves Claude sessions opened in many different repo folders.

### Added
- `hub.map_file` (default `map.md`) and `hub.session_map` (default `true`): a hub that keeps its map under another name, or already
  loads it through a CLAUDE.md import, no longer gets `map is missing` lint errors or a doubly injected map. Lint, `dea find` and the
  session hook honour both; due rows, stale-platform rows and the footer are still injected when `session_map` is false.
- `status.md` due dates: header synonyms `Due`/`Date`/`When` with `State`/`Status`; the date cell may carry emphasis (`**`, `_`, `~~`),
  a leading `~` and trailing text; dated list items under a heading containing `dated`, `due` or `deadline` count too (struck-through
  or `- [x]` is done). The session hook and `dea lint` share one rule.

- The Airflow scanner follows module-level config constants a DAG imports (`from cfg import MART_PROCEDURES`, or `cfg.MART_PROCEDURES`)
  and turns the strings under `proc`/`procedure`/`sp`/`stored_procedure`/`routine`/`function` keys into `calls` edges
  (`via: config:<module>.<NAME>`), so live routine pages list those DAGs under "Called by". Literals only (f-strings are counted in
  `dynamic_config_entries`), 2,000 entries per constant, modules outside the scanned repo ignored. Existing scan caches are rebuilt once.

### Fixed
- The SQL, CLI and query-log hooks resolved `de-agent.yaml` and approved plans from the session folder, so a session opened in a repo
  silently fell back to `strict` and no approved plans. They now find the hub the way the CLI does (`DEA_HUB`, nearest `de-agent.yaml`
  upward, `~/.config/de-agent/config.yaml`). Path-based guards are unchanged. With no hub at all behaviour is as before.
- Lint: pages in `projects/<name>/` are topic pages, not profiles (no `profile-frontmatter` or `orphan-project`); a `password=` value
  that is a code reference (`CREDS["password"]`, `os.environ[...]`, `os.getenv(...)`) is not a secret; a map file named `_map.md` is
  indexed for the orphan check.
- Generated profile headers omit cross-project edges whose table is in `edges.ignore_tables`.
- Postgres collector: the no-lock tables fallback computed sizes with 32-bit page math and failed with an integer overflow on tables
  over 2 GB; it now uses `bigint`.

## [0.3.0] - 2026-11-08

### Added
- Routines: procedures, functions, views, materialized views and triggers from every SQL engine. A reserved `routines` section
  (Postgres, Redshift and the `postgres-lite`, `mysql`, `snowflake`, `mssql`, `bigquery` and `databricks-system` packs) with a row
  contract, redaction before anything is written (`DEA_DENYLIST_FILE` honoured), a content-addressed body store `.dea/routines/` pruned with
  the snapshots, and the options `routines`, `routine_kinds`, `routines_max`, `routines_max_bytes`. Redshift reuses routines for
  `routines_ttl_h` (24) hours unless `--deep`.
- `dea scan` parses routine bodies (cached by `body_hash`) into the lineage graph: `proc:`/`function:`/`view:`/`trigger:` nodes,
  `edges.calls`, a scan option `connections:` (Airflow `conn_id` to source) for `CALL` resolution, and a repo-vs-live drift warning.
- `dea impact proc:<schema.name>` (and `function:`, `view:`, `trigger:`); `dea impact <table>` names the DAGs that call each routine.
- `platform/<source>.md` gets a Routines summary; one page per routine under `platform/<source>/routines/`; `dea diff` prints a unified
  diff of a changed body (80 lines at most).
- Demo: a plpgsql procedure, a SQL function, a view and a trigger in the warehouse, called by a DAG.
- docs/routines.md; routine privileges in docs/read-only-access.md.

### Changed
- `dea diff` says that it compares snapshots, not the live platform, and to take one first (`dea snapshot`) when it has nothing to
  compare or found no change.

### Fixed
- Found in the live check (one RDS PostgreSQL, one RA3 Redshift cluster):
  - Redshift routines failed with `cannot cast type text[] to character varying`; argument names now come via `array_to_string`.
  - Temp tables a routine creates (`CREATE TEMP TABLE`, `SELECT ... INTO TEMP`, `#name`) became tables in the routine's schema; they are
    now left out of the graph (`PARSER_VERSION` 2 re-parses cached routines).
  - A secret key word inside a string literal (`'reset-password' THEN 'Password Reset'`) was redacted; without a `=`/`:` separator the
    key must now be followed by whitespace (`PASSWORD 'x'`).
- A routine body missing on one side of a diff (view text skipped on a lock timeout) is unknown, not changed.

## [0.2.5] - 2026-11-05

### Fixed
- 0.2.5 — fix: dea scan crashed with RecursionError on loops over range() with non-constant arguments. The evaluator's
  `range` branch no longer falls back into itself, and a RecursionError in one file is now recorded as a scan error for
  that file instead of aborting the scan.

## [0.2.4] - 2026-11-03

### Fixed
- CI: the 5,000-page `dea find` timing test is a `perf` test (run nightly with the scan benchmark), so a slow shared runner no
  longer fails a push. The nightly job runs every `perf` test.

## [0.2.3] - 2026-11-03

### Fixed
- `dea diff`: a recovered signal whose DAG failed again (higher failure count) is a NEW signal again; ACTIVE AGAIN is kept for
  a new run in progress with the same count. 0.2.2 reported both as ACTIVE AGAIN.
- CI: the doctor test assumed `kubectl` is absent; the demo e2e test now creates `demo/.env`.
- Demo onboarding: `make demo` (start plus first pipeline run), `make demo-warm`, `make dag-trigger D=`, a next-step hint printed by every
  fault's `inject.sh`, and the Makefile `DEA` default falls back to `dea` on PATH when the repo venv is absent.

### Changed
- Wording: eval report regrade note, README Airflow/dbt version claims, START_HERE safety wording, SAFETY "In short" section,
  SECURITY and Code of Conduct contact.

## [0.2.2] - 2026-11-03

### Added
- `dea --version` (`-V`).

### Fixed
- `dea diff`: a recovered failure signal whose DAG started a new run (or failed again) was printed twice, as NEW and as cleared. It is now
  one `ACTIVE AGAIN` line.

## [0.2.1] - 2026-11-03

### Added
- [docs/read-only-access.md](docs/read-only-access.md): copy-paste read-only logins for Postgres, Redshift, MySQL, Snowflake, BigQuery,
  SQL Server and Databricks, plus the least-privilege setup for AWS, Airflow, GCP, Kubernetes and the HTTP packs. SAFETY.md called this
  layer "documented" without saying how.

### Changed
- `hub-init` asks whether each source has a read-only login and shows the matching block for the human to run; `add-source` points to it.
- START_HERE: the two "Works with your stack" sections are now "Works with your code" and "Works with your systems"; the real-platform
  steps start with the read-only login.

## [0.2.0] - 2026-10-28

### Added
- **Packs**: declarative sources. A pack is a YAML file (`sql`, `cli` or `http` kind) that the new generic collectors (`type: sql`, `type: cli`, `type: http`)
  run read-only; render, diff, snapshots, `dea find` and the session hook work unchanged. Read-only is enforced in code for every kind (SQL classified in the pack's dialect and run in a
  read-only transaction; CLI argv checked against a read-verb table with `shell=False`; HTTP GET and read-only GraphQL only). `when:` / `rows:` rules run in a whitelist expression interpreter.
  See [docs/packs.md](docs/packs.md).
- Eleven built-in packs: `postgres-lite` and `mysql` (validated live against PostgreSQL 16 and mysql:8.4 containers), `snowflake`, `bigquery`, `databricks`, `databricks-system`, `mssql`, `gcp`, `kubernetes`,
  `dagster`, `fivetran` (validated on fixtures: hand-built from documented shapes with fictional values).
- `dea pack list|show|new|check|test` (`dea query` points pack sources to `dea pack test`); `dea doctor` covers pack sources (driver or CLI present, credentials, `pack check`); hub-local `packs/<name>.yaml` overrides a built-in of the same name.
- `add-source` skill: "we also use X" finds or drafts a pack, checks it, proposes the source block with secret references only, tests it.
- Extras `mysql`, `snowflake`, `bigquery`, `databricks`, `mssql`, `terraform` and `all`; `jmespath` is now a core dependency; `pg` also installs SQLAlchemy.
- `tools/release.sh` v2: the release commit goes on top of `origin/main` (or `--base <ref>`), so the public history is linear with one commit per release and pushes fast-forward; `--version`;
  `--base none` keeps the orphan first-release path; the leak greps cover the new tree, the new commit's diff and the whole public history.
- Scanners: `python` (scripts, `.ipynb` and Databricks source notebooks, pyspark and pandas table/path references, per-function edges) and `terraform`
  (resources catalogued, DMS/Firehose/Glue/BigQuery/Snowflake/Pub/Sub/MWAA edges; optional `[terraform]` extra, regex fallback).
- Util attribution: a DAG or job that calls a shared function inherits its SQL, table and proc edges (`via: util:<module.func>`, 2 hops, literal
  arguments instantiated, loops over module-level lists and dicts unrolled). `edges.ignore_tables` keeps bookkeeping tables out of `dea impact`.
- `cli_guard` hook: reads and writes classified for `aws`, `gcloud`, `gsutil`, `bq`, `databricks`, `kubectl`, `helm`, `terraform`/`tofu`, `pulumi`, `az`, `snow`, `dbt`,
  `airflow`, `dagster`, `prefect`, `kafka-topics`/`kafka-configs`; wrappers and `docker`/`kubectl exec` followed; unknown verbs denied; `guard.cli_allow` (human-only, guarded by `self_guard`).

### Changed
- The SQL classifier treats `SHOW ...` in the MySQL and Snowflake dialects as a read (it is parsed into its own node there and was refused as a write).
- `type:` of an unknown source now points to packs in its error message.

### Security
- `sql_guard` reads `xargs` / `find -exec` / `env` wrapped clients, MCP tools of any name and any input field, and mysql, mariadb,
  snowsql, sqlcmd, clickhouse-client, sqlite3, duckdb, trino/presto, spark-sql and beeline; Python scripts that import a database
  driver are scanned (heuristic). One shared client list feeds `sql_guard` and `secret_guard`.
- `secret_guard` resolves symlinks, blocks environment dumps, `ln`/`cp -s`/`dd if=`/`gpg -d`/editors on secrets and Python `open()`.
- Size cap (200 KB input, 100 KB SQL) before parsing; new `self_guard` protects the plugin's hooks, the `guard:` mode, `writable:`
  sources and the hook settings; `test_guard` covers `dd of=`, `ex`/`ed`/`vi`, `perl -i`, `ruby -i`.
- Diff steps are hashed over the exact patch bytes; `shell_destructive` looks inside `bash -c` and `env`.
- `cli_guard` wrappers (red-team round): leading redirects, container images whose entrypoint is a guarded CLI, `python -m awscli|airflow|dbt|...`, `parallel`,
  `taskset`, `runuser`, `su -c`, `strace`, `chroot`, `nsenter`, `unshare`, `busybox`, `systemd-run`, `script`, `tmux`, `screen`, `coproc`, `trap`, `at`/`crontab`, shell functions, runner
  global options (`uv --directory . run`), `ssh -- cmd` / stdin / `-o ProxyCommand`, directly executed scripts, `$IFS` program names and write-then-run command lines; bq write flags, gcloud verb position,
  boolean flags no longer swallow the verb, `gsutil`/`gcloud storage` to any non-local destination, `airflow info --file-io`.
- `self_guard`: interpreter and tool writes to `de-agent.yaml` (including globs), the plugin's `cli/src` and `cli/pyproject.toml`, `git` and `pip` commands against the plugin's own checkout and package.
- SQL: side-effecting function denylist per dialect (guard and packs), SQL on stdin by any path (`/dev/stdin`, `snow sql --stdin`), `mysql --init-command` and `--defaults-file`, MySQL executable
  comments, T-SQL statements without `;`, `psql -o`/`-L`.
- Packs: more banned flags (kubeconfig, host, account, endpoint, file, repo, ...), `--profile`/`--context` values restricted to plain names, secret-bearing operations refused for aws, gcloud, az,
  terraform, airflow, databricks, kubectl, dbt and helm, and GraphQL comments and strings tokenised so one cannot hide a mutation from the other.

### Changed
- `env:NAME` secret references resolve from the process environment, else from a `.env` file in the hub directory (read by `dea`
  itself, never printed). The demo tour now works with just `cd demo && claude --plugin-dir ..`.
- `dea scan`: a dbt scan without a manifest is skipped with a hint (`run dbt parse`) and exit 0; other scans still run.
- `dea doctor` checks the python3 the hooks will actually run and always shows the vendored-sqlglot check.
- `dea find` says `no hits for <term>` (exit 1 kept); `dea edges` / `dea impact` accept an unqualified table name when unique.
- README: audience line, prerequisites and install before the tour, extras (`[pg,aws]`) explained; "how to read the results" moved to `evals/README.md`.

## [0.1.0] - 2026-10-28

First release. The project was renamed from groundtruth to de-agent (CLI `gt` -> `dea`, package `de_agent`) before this first public release.

### Added
- `dea` CLI: `init`, `snapshot`, `diff`, `lint`, `find`, `scan`, `edges`, `impact`, `query`, `plan`, `approve`, `apply`, `plans`,
  `doctor`, `guard-test`, `config schema`.
- Read-only collectors: Postgres, Redshift (PG wire and Data API), Airflow (2.x and 3.x REST), dbt artifacts, and AWS (DMS, Glue, S3,
  Athena, Firehose, CloudWatch), each rendering a deterministic, signal-first `platform/<source>.md`; `dea diff` between snapshots;
  `exec: ssh:<host>` to collect from another host.
- Static scanners (Airflow DAGs and shared utilities, dbt manifests, SQL files) producing `catalog/` pages and table-level lineage
  edges; `dea edges` and `dea impact`.
- Hub scaffold (`map.md`, domains, project profiles with generated headers, integrations, `status.md`), `dea lint` (budgets, links,
  frontmatter, staleness, secrets) and an SQLite FTS5 index behind `dea find`.
- Guardrail hooks: `sql_guard` (database writes denied outside an approved plan, fails closed), `secret_guard`, `approval_guard`,
  `test_guard` (no removing or weakening dbt tests), `stop_check`, and a session-start context injector.
- Plan, approve, apply workflow: plan files with `dea plan check`, human-only `dea approve` (TTY, HMAC-signed statement hashes),
  executor-only `dea apply` (one transaction per step, verify blocks, stop on first failure).
- Skills: `hub-init`, `onboard-project`, `hub-sync`, `pipeline-check`, `incident-triage`, `plan`, `backfill`, `schema-change`,
  `new-pipeline`, `audit`. Agents: `runner`, `explorer`, `executor`.
- Demo platform ("Acme Shop", fictional): source Postgres, Airflow, warehouse Postgres, dbt, an S3 stand-in, and six injectable faults.
- Eval suite (`evals/`): 12 cases run with and without the plugin, deterministic graders, an LLM judge reported separately; results
  and reports for two runs under `evals/results/`.
- Documentation: README, architecture, adoption guide, writing a collector and a scanner, skills reference, generated configuration
  reference (`dea config schema`), safety model, contributing guide and code of conduct.

### Fixed
- SQL scanner: the plpgsql regex fallback no longer reads `EXTRACT(.. FROM col)`, `IS DISTINCT FROM col` or `SUBSTRING/TRIM(.. FROM ..)` as a table.
- Airflow scanner: Python `SyntaxWarning`s raised while parsing scanned code are no longer printed.

### Security
- Guards are defence in depth, not a sandbox: read-only database roles remain the primary control. See `docs/SAFETY.md`.
