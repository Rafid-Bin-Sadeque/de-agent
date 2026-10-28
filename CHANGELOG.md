# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security
- `sql_guard` reads `xargs` / `find -exec` / `env` wrapped clients, MCP tools of any name and any input field, and mysql, mariadb,
  snowsql, sqlcmd, clickhouse-client, sqlite3, duckdb, trino/presto, spark-sql and beeline; Python scripts that import a database
  driver are scanned (heuristic). One shared client list feeds `sql_guard` and `secret_guard`.
- `secret_guard` resolves symlinks, blocks environment dumps, `ln`/`cp -s`/`dd if=`/`gpg -d`/editors on secrets and Python `open()`.
- Size cap (200 KB input, 100 KB SQL) before parsing; new `self_guard` protects the plugin's hooks, the `guard:` mode, `writable:`
  sources and the hook settings; `test_guard` covers `dd of=`, `ex`/`ed`/`vi`, `perl -i`, `ruby -i`.
- Diff steps are hashed over the exact patch bytes; `shell_destructive` looks inside `bash -c` and `env`.

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
