# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
