# M1a spec — CLI engine, Postgres collector, hub templates, session hooks

Read first: `docs/PLAN.md` (§3, §4) and `docs/KNOWLEDGE_ARCHITECTURE.md`. Builds on M0/M0.1 (449ea76).
Approved: files under the repo, the local venv, local commits, `docker run` of a throwaway `postgres:16` container for
integration tests (removed afterwards). Not approved: remote/push/publish, global installs.
Zero company data: use only generic names (shop, orders, customers, events).

## 1. Package layout (cli/src/de_agent/)

```
config.py      models + loading + secret refs
hubpath.py     locate the hub
sqlexec.py     run read-only SQL → list[dict] (psycopg or psql CLI), local or over ssh
runner.py      run collectors (parallel, timeouts, local | ssh:<host>)
collectors/__init__.py   registry {type: Collector}
collectors/base.py       Collector protocol + CollectorResult
collectors/postgres.py
render.py      JSON → markdown per collector (registry), deterministic
snapshot.py    orchestrates runner → .dea/snapshots/<ts>/<source>.json + platform/<source>.md; retention 30
diff.py        snapshot vs previous → "what changed" markdown/JSON
lint.py        hub rules
find.py        SQLite FTS5 index + query
initcmd.py     scaffold a hub from templates
cli.py         typer app: init | snapshot | diff | lint | find | doctor | guard-test
templates/hub/ (package data)
```
Python ≥ 3.10 for the CLI. Deps: add `psycopg[binary]>=3.1` to the `[pg]` extra only. The core must work without it (psql fallback).

## 2. Config (`de-agent.yaml` at hub root) — `config.py`

```yaml
version: 1
hub: {name: "Acme data platform", map_budget_tokens: 1500, snapshot_max_age_hours: 24, retention: 30}
sources:
  - name: warehouse            # unique, [a-z0-9_-]+
    type: postgres
    exec: local                 # local | ssh:<host-alias>
    timeout_seconds: 120
    conn: {host: localhost, port: 5432, dbname: shop, user: readonly_agent, password: "pgpass", sslmode: prefer}
    options: {schemas: [public, analytics], top_tables: 25}
guard: {mode: strict}           # strict | ask
```
- pydantic v2 models with `extra="forbid"`. Unknown source `type` → error listing the registered types.
- Secret fields (`password`, `token`, any key ending `_secret`, `_key`) accept ONLY refs: `pgpass` | `env:NAME` |
  `file:/abs/path` (the file must be mode 0600 or stricter, otherwise an error) | `none`. A literal value is a load error:
  "secret literal in config; use env:/pgpass/file:".
- `resolve_secret(ref)` happens at run time only, and the value is never logged, rendered or put in a snapshot.

Hub discovery (`hubpath.py`), first match wins: `$DEA_HUB` → nearest `de-agent.yaml` walking up from cwd →
`~/.config/de-agent/config.yaml` key `hub:` (abs path). None found → commands that need a hub exit 2 with a hint
(`dea init` or set DEA_HUB).

## 3. SQL execution — `sqlexec.py`

`run_query(conn_cfg, sql, exec_target, timeout_s) -> list[dict]`
- Every query runs inside `BEGIN READ ONLY; SET LOCAL statement_timeout = <timeout_ms>; <sql>; COMMIT`. This is enforced
  here, so collectors can't write even by mistake.
- Before executing, every SQL string a collector sends goes through `sqlclass.classify` and must be all-read (the BEGIN/SET/COMMIT
  wrapper is added after the check). A write raises `CollectorError`, so collectors are guarded by the same classifier.
- Backend A: psycopg if importable AND exec is local.
- Backend B (default for ssh, and the fallback locally): the `psql` CLI with
  `-X -q -A -t -v ON_ERROR_STOP=1 -c "<wrapped sql producing: select coalesce(json_agg(q), '[]') from (<sql>) q>"`.
  The password goes via `PGPASSWORD` in the subprocess env only (never argv). `pgpass` → let psql read ~/.pgpass itself.
  Over ssh: `ssh -o BatchMode=yes -o ConnectTimeout=10 <host> psql …`, with the SQL passed on stdin (`-f -`), never interpolated
  into the remote shell string. The secret for ssh must be `pgpass` (resolved on the remote box). Any other ref with ssh → config error.
- Parse stdout JSON → list[dict]. A non-zero exit → `CollectorError(stderr tail)`, with secrets redacted from messages.

## 4. Collector protocol — `collectors/base.py`

```python
class Collector(Protocol):
    type: str                 # "postgres"
    schema_version: int       # bump when output shape changes
    list_keys: dict[str, list[str]]   # for diff: {"tables": ["schema","name"], "slots": ["slot_name"], ...}
    def collect(self, source: SourceConfig, ctx: RunContext) -> dict: ...
```
`CollectorResult = {source, type, schema_version, collected_at (UTC ISO), duration_s, data: dict, errors: [str]}`.
One failing section → appended to `errors`, the other sections still returned (partial results beat nothing).

## 5. Postgres collector — `collectors/postgres.py` (PG 12–17; each section its own query)

| section | content |
|---|---|
| `server` | version, uptime, max_connections, current connections by state, cache hit ratio, oldest xid age (wraparound %) |
| `databases` | name, size_bytes |
| `schemas` | name, table count, total bytes (options.schemas filter, default: non-system) |
| `tables` | top N by total size: schema, name, total/table/index bytes, n_live_tup, n_dead_tup, dead_pct, last_autovacuum, last_autoanalyze, seq_scan, idx_scan |
| `unused_indexes` | idx_scan = 0 and size > 10 MB, excluding unique/PK |
| `long_queries` | state <> idle, now()-query_start > 5 min: pid, user, duration_s, state, wait_event, query (first 200 chars) |
| `blocking` | pairs via pg_blocking_pids: blocked pid/query, blocker pid/query, wait seconds |
| `replication_slots` | slot_name, plugin, active, lag bytes (pg_wal_lsn_diff) |
| `settings` | a fixed list: shared_buffers, work_mem, maintenance_work_mem, autovacuum, statement_timeout, idle_in_transaction_session_timeout |

All queries are pure SELECTs over pg_catalog / pg_stat_* views. No extensions are required: if pg_stat_statements is
missing, skip that part silently, and add a `top_statements` section when it's present (top 10 by total_exec_time).

## 6. Render — `render.py`

- `render(result) -> str`: frontmatter (`source`, `type`, `generated` UTC, `schema_version`), a header line
  "GENERATED by dea snapshot — do not edit", then a **Signals** list first (rule-based one-liners such as "table
  analytics.orders 34% dead tuples", "2 queries running > 5 min", "slot cdc_slot inactive, lag 3.1 GB", "xid age 61% of wraparound"),
  then compact tables per section, then an Errors block if any.
- Signal thresholds are module constants (dead_pct ≥ 20 and n_dead_tup ≥ 100k; long query > 5 min; inactive slot or lag > 1 GB;
  xid age ≥ 50%; connections ≥ 80% of max; cache hit < 95%).
- Deterministic: stable sort orders, fixed float formatting, byte sizes humanised (KB/MB/GB, 1 decimal).

## 7. Snapshot + diff — `snapshot.py`, `diff.py`

- `dea snapshot [--source NAME ...] [--no-render]` runs sources concurrently (ThreadPoolExecutor, max 4) with per-source
  timeout_seconds. It writes `.dea/snapshots/<YYYYMMDDTHHMMSSZ>/<source>.json` and `platform/<source>.md`, then
  `platform/index.md` (one line per source: status, generated, signal count, link). Keeps the newest `retention` snapshot dirs.
  Exit 0 if every source succeeded, 1 if any failed (the others are still written).
- `dea diff [--source NAME] [--since <snapshot-id>] [--json]`: compares the latest snapshot with the previous one (or `--since`).
  Generic engine: for each list in `list_keys`, it reports added/removed rows by key and changed fields on shared keys. Numeric fields
  are only reported when |Δ| ≥ 20% and the absolute delta is meaningful (bytes ≥ 100 MB, counts ≥ 1,000, seconds ≥ 60). Scalars are
  compared directly. It also reports new and cleared Signals. Output: markdown "What changed since <ts> (<age>)", grouped
  by source, signals first. Nothing changed → one line saying so.

## 8. Lint — `lint.py` (`dea lint [--json]`, exit 1 on any error)

| rule id | check | level |
|---|---|---|
| map-budget | map.md tokens (≈ chars/4) ≤ map_budget_tokens | error |
| page-budget | domains/*.md ≤ 60 lines, projects/*.md ≤ 150 lines, conventions.md ≤ 150 | error |
| broken-link | every `[[name]]` resolves to a hub page (projects/, domains/, integrations/, top-level) | error |
| orphan-project | a project in map/domain pages with no profile, or a profile listed nowhere | error |
| profile-frontmatter | profile has `owner`, `domain`, `updated` (YYYY-MM-DD); `repo` optional | error |
| profile-stale | `repo:` path given and its last git commit date > `updated` + 14 days | warning |
| snapshot-stale | any `platform/*.md` generated older than snapshot_max_age_hours | warning |
| status-overdue | status.md table rows whose first date cell < today and state ≠ done | warning |
| secret-in-hub | any hub file matching the secret_guard patterns (DSN with password, PGPASSWORD=, AWS secret) | error |

## 9. Find — `find.py` (`dea find <query> [-n 10] [--json]`)

- Index `.dea/index.db`: an FTS5 table `docs(path, kind, title, body)` with tokenizer `unicode61 tokenchars '_'`, plus a second
  column `body_split` where `.`, `/`, `-` and `:` are replaced by spaces. `analytics.fct_orders` then matches `fct_orders`,
  `analytics.fct_orders` and `analytics`.
- Indexed: all hub `*.md` (`kind` = map | domain | project | integration | status | conventions | other) and `platform/*.md`
  (`kind` = platform). It reindexes changed files only (stored mtime + size). `dea find --rebuild` forces a rebuild. `dea snapshot` reindexes platform.
- Query: bm25 with weights title 5 / body 1 / body_split 1. Output lines `path:line  snippet`, where the line is the first line
  containing a query term. If FTS5 is unavailable, fall back to a linear scan with a warning.
- Performance test: 5,000 generated pages, query p95 < 50 ms, incremental reindex of 1 changed file < 200 ms.

## 10. Hub templates + `dea init` — `templates/hub/`, `initcmd.py`

`dea init [DIR] --name "<platform name>" [--with-example]` creates: `de-agent.yaml` (one commented postgres source),
`map.md`, `status.md`, `conventions.md`, `domains/_template.md`, `projects/_template.md`, `integrations/_template.md`,
`platform/.gitkeep`, `.gitignore` (`.dea/`), and `README.md` explaining the layout in about 20 lines. It refuses to overwrite existing files
(prints the skipped list). The templates follow KNOWLEDGE_ARCHITECTURE §1–2: the map has a "Domains/Projects" table, a "Platform" line
that points to platform/index.md, and an "In flight" pointer to status.md. The profile template has a frontmatter block, then
Purpose / Layout / Conventions / Connections / Gotchas sections, each with a one-line hint. The status template has Dated checks /
In progress / Open issues / Done tables with the columns used by lint.

## 11. Hooks — `scripts/session_context.py`, `scripts/query_log.py`, `hooks/hooks.json`

- SessionStart (matcher `startup|resume|clear|compact`): locate the hub (same discovery as hubpath.py, reimplemented with the stdlib
  only and Python 3.9 compatible). Then emit
  `{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext": "<text>"}}` where text ≤ 6,000 chars =
  map.md, then "Due / overdue" status rows (date ≤ today + 2 and not done), then "Stale platform data" lines, then one line:
  "Use `dea find <term>` before searching repos; platform state: <hub>/platform/". No hub → no output, exit 0.
  The hook must never fail a session: catch everything and exit 0 silently.
- PostToolUse (matcher `Bash`): if `extract_sql` finds DB SQL, append a JSONL line to `<project>/.dea/logs/queries.jsonl`:
  ts, session_id, agent_type, source (psql/bq/…), kind, type, hash, first 300 chars, exit status if available. Never block,
  never print. Python 3.9 compatible, and uses the vendored path setup from sql_guard.
- Add both hooks to hooks.json (timeouts 10 s). Keep the existing guard hooks unchanged.

## 12. CLI — `cli.py`

`dea doctor`: Python version, hub found + path, config valid, each source reachable (`select 1` through sqlexec), psql
present, FTS5 available, hooks' python3 version ≥ 3.9 and vendored sqlglot importable. Prints a ✓/✗ table and exits 1 on any ✗.

## 13. Tests (`cli/tests/`)

- `test_config.py`: valid config; secret literal rejected; file: ref with permissions too open rejected; unknown type; ssh + env: secret rejected.
- `test_sqlexec.py`: wrapper composition; a collector write is refused before execution; psql argv never contains the password
  (assert on the constructed argv/env); stderr redaction.
- `test_postgres_collector.py` (marker `integration`): starts `postgres:16` via docker (skips if docker is unavailable), creates schema
  `shop` with orders/customers, 200k rows, deletes 40% to create dead tuples, an unused 20 MB index, and a background
  `pg_sleep(400)` session for long_queries. Then asserts each section populated and the expected signals present. Uses a read-only role
  created in the fixture (`readonly_agent`, `pg_read_all_stats` + `pg_read_all_data` on PG ≥ 14).
- `test_render.py`: fixture JSON → golden markdown files (`tests/golden/`); determinism (render twice, byte-identical).
- `test_diff.py`: added/removed/changed rows, threshold suppression, new/cleared signals, "no changes".
- `test_lint.py`: one fixture hub per rule (pass + fail).
- `test_find.py`: identifier matching (dotted, underscored), incremental reindex, the 5k-page perf check.
- `test_session_hook.py`: hub found → JSON with map + due rows, ≤ 6,000 chars; no hub → silent; corrupt files → silent exit 0; runs under
  DEA_HOOK_PYTHON (3.9).
- CI: add a `postgres:16` service to the 3.12 job and run integration tests there.

## 14. Validate + commit

`ruff check cli scripts` · `pytest -q cli/tests` (report counts; integration included if docker is available) ·
`DEA_HOOK_PYTHON=/usr/bin/python3 pytest -q cli/tests/test_hooks.py cli/tests/test_session_hook.py` ·
`claude plugin validate . --strict` · an end-to-end run in a scratch dir:
`dea init /tmp/<scratch>/hub --name Demo` → edit config to the test container → `dea doctor` → `dea snapshot` → `dea snapshot` again after
deleting more rows → `dea diff` (shows the dead-tuple change) → `dea find orders` → `dea lint`. Paste the outputs into the report.
Also fix the M0 polish item: a SELECT denied for a side-effect function says "SELECT calling <fn>() is a write".
Commit: `M1a: CLI engine (config, sqlexec, snapshot, diff, lint, find), postgres collector, hub templates, session hooks`
+ the Co-Authored-By trailer.
