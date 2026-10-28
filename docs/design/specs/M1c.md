# M1c spec — `dea scan`: catalog, symbol index, cross-project edges, generated profile headers

Depends on M1a (config, find) and M1b (the demo repo as a fixture). See KNOWLEDGE_ARCHITECTURE §3 and §5. Static analysis
only: never import user code, never execute it, never connect to a database.

## 1. Config

```yaml
scans:
  - {name: pipelines, type: airflow, path: ../airflow_repo, dag_folders: [dags], util_packages: [common, utils]}
  - {name: analytics, type: dbt, path: ../analytics, target_dir: target}       # uses manifest.json
  - {name: warehouse_sql, type: sql, path: ../warehouse_sql, globs: ["**/*.sql"], dialect: redshift}
```
Paths are relative to the hub. `project:` (optional) links a scan to `projects/<project>.md` (default: same as name).

## 2. Scanners (`cli/src/de_agent/scanners/`)

**airflow.py** (Python `ast`, no Airflow import):
- DAG discovery: `DAG(...)`, `with DAG(...)`, `@dag(...)`-decorated functions; dag_id from the first positional arg or `dag_id=`.
  A non-constant id (f-string, variable, loop/factory) → record `dynamic: true`, a pattern (e.g. `export_{x}`) and the source line.
  Also detect DAG factories (a function returning DAG, called in a loop) → one entry per call site, marked dynamic.
- Per DAG: schedule (`schedule`/`schedule_interval`/timetable expr as text), tags, owner (default_args), file:line,
  operator classes used, Dataset/Asset outlets and inlets (URIs), ExternalTaskSensor `external_dag_id`, TriggerDagRunOperator
  `trigger_dag_id`, `pool`.
- SQL: string constants (and simple concatenations or `.format` of constants) passed as `sql=` to any operator, or to hook methods
  `run|get_records|get_first|get_pandas_df|execute|executemany` → `sqlglot` (configured dialect, default postgres) → tables
  read / written + `CALL <proc>` names. Parse failure → keep the raw first 120 chars, `sql_unparsed: true`.
- Files ending `.sql` referenced via `sql="path/x.sql"` (template_searchpath or relative) → read and parse the same way.
- **Symbol index**: for `util_packages`, list top-level functions/classes with file:line and first docstring line. For every
  DAG file, resolve imports (`import a.b as c`, `from a.b import f as g`) and record which util symbols are called → `symbol → [dag files]`.
  Unresolvable dynamic calls are ignored and counted in `unresolved_calls`.

**dbt.py**: from `manifest.json`: models (name, materialization, schema, folder), parent/child edges, sources (→ physical
`schema.table`), exposures. Each model maps to its physical relation `schema.alias`.

**sql.py**: per file: objects defined (`CREATE [OR REPLACE] TABLE|VIEW|MATERIALIZED VIEW|PROCEDURE|FUNCTION`) and, for each,
the tables read/written in its body (procedure bodies in `$$…$$` are parsed as SQL; on failure, regex for `INSERT INTO|UPDATE|DELETE FROM|MERGE INTO|FROM|JOIN`, marked `heuristic: true`).

**edges.py**: builds a table-centric graph from every scan plus the latest platform snapshot (e.g. CDC tasks with table
mappings once M3 collectors exist):
- nodes = physical tables `db.schema.table` (normalized lowercase; `schema.table` when the db is unknown)
- writer edges: dag task / proc / dbt model / cdc task → table; reader edges: table → dag / proc / dbt model / exposure
- `CALL proc` from a DAG + proc writes table T ⇒ the DAG writes T (via proc)
- Output `catalog/edges.json` and `catalog/edges.md`: per table one line `table ← writers | → readers`; a "Cross-project"
  section listing only edges whose writer and reader belong to different projects; "Unresolved" (names referenced but never
  defined/written anywhere: possible external sources or typos).

## 3. Output (`catalog/`, all generated, deterministic, frontmatter `generated:`)

- `catalog/airflow/<scan>.md`: grouped by top-level folder under dag_folders, then by tag. One line per DAG:
  `dag_id · schedule · owner · tags · file:line · in: <datasets/sensors> · out: <datasets/triggers> · writes: <tables> · reads: <tables>`.
  Over 300 DAGs → split into one file per folder plus an index.
- `catalog/symbols/<scan>.md`: `module.symbol (file:line) — docstring — used by N DAGs: a, b, c…` (sorted by N desc).
- `catalog/dbt/<scan>.md`: model · materialization · relation · upstream · downstream count.
- `catalog/sql/<scan>.md`: object · kind · reads · writes.
- `catalog/edges.md` and `catalog/edges.json`.
- `dea find` indexes `catalog/**` (kind = catalog).

## 4. Generated profile headers

In `projects/<project>.md`, `dea scan` rewrites ONLY the text between `<!-- dea:generated:start -->` and `<!-- dea:generated:end -->`
(inserted right after the frontmatter if absent): repo path, last commit (hash, date, subject), DAG/model/object counts,
top 10 tables written and read, cross-project edges (`→ [[other_project]] via table X`), and a link to the catalog page.
Human text outside the markers is byte-identical after a scan (tested). The `updated:` frontmatter is not touched; lint compares it
against human edits only.

## 5. Incremental + performance

- `.dea/scan_state.json`: per scan the git HEAD (or the max file mtime when not a git repo) and per-file sha1. Only changed
  files are re-parsed; edges are rebuilt from the cached per-file results.
- Target: a 1,000-DAG synthetic repo full scan < 60 s, and an incremental scan with one changed file < 3 s (tested with a
  generated fixture repo).

## 6. CLI

`dea scan [--name N] [--full]` → writes the catalog + profile headers + reindex. `dea edges <table>` → that table's writers
and readers (from edges.json), so an agent can ask "who writes analytics.fct_orders?" in one call. `dea impact <symbol|table>`
→ for a util symbol: the DAGs using it; for a table: downstream readers transitively (depth ≤ 3).

## 7. Tests

Fixture repo `cli/tests/fixtures/airflow_repo/` with tricky cases: TaskFlow `@dag`, `with DAG` nested inside a function,
factory loop (dynamic ids), Dataset/Asset (2.x/3.x names), ExternalTaskSensor, SQL via operator kwarg, via hook call, via
`.sql` file, `CALL proc()`, string concat, an unparseable SQL, aliased imports of util functions, star import (counted as unresolved).
dbt: the demo manifest. sql: procs with `$$` bodies (one that fails to parse → heuristic). Edges: a DAG CALLs a proc that writes T,
a dbt model reads T → cross-project edge. Marker rewrite preserves human text. Perf tests per §5. The demo stack scan
must produce `extract_shop writes raw.orders → read by dbt stg_orders → fct_orders → mart_daily_revenue → exported by export_daily_orders`.

## 8. Validate + commit

ruff · pytest (counts, perf numbers) · `dea scan` on the demo hub (paste catalog/edges.md excerpt) · `dea impact raw.orders` ·
`claude plugin validate . --strict`. Commit `M1c: dea scan — airflow/dbt/sql catalog, symbol index, edges, profile headers` + trailer.
