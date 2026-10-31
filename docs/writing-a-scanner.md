# Writing a scanner

A scanner reads a repo's files and extracts structure that code already encodes: which job reads or writes which table, what depends on
what. `dea scan` turns the results into `catalog/` pages and table-level edges (`catalog/edges.json`), which `dea edges`, `dea impact` and
`dea find` read. Built in: `airflow`, `dbt`, `sql`, `python` (scripts, notebooks, pyspark) and `terraform`.

## The contract

A scanner is a pair of pure functions plus a renderer. The `sql` scanner is the smallest real one (`cli/src/de_agent/scanners/sql.py`):

```python
def scan_<type>_text(text: str, rel: str, dialect: str) -> dict:
    """Parse ONE file. Return {"rel": rel, ...}: JSON-serialisable, a pure function of the text (it is cached by file hash)."""

def assemble(scan, per_file: dict[str, dict], revision: str) -> dict:
    """Combine every file's result into {"scan", "type", "project", "revision", ..., "stats": {...}}."""
```

Per-file results are cached by content hash, so a second `dea scan` re-parses only changed files. The revision is the repo's git HEAD
(or a content hash outside git), and goes in the frontmatter of every generated page so staleness is visible.

## A minimal example

A scanner that lists the targets of a Makefile:

```python
import re


def scan_makefile_text(text: str, rel: str, dialect: str) -> dict:
    targets = []
    for n, line in enumerate(text.splitlines(), 1):
        m = re.match(r"^([A-Za-z0-9_./-]+)\s*:(?!=)", line)
        if m:
            targets.append({"name": m.group(1), "file": rel, "line": n, "reads": [], "writes": [], "calls": []})
    return {"rel": rel, "targets": targets}


def assemble(scan, per_file: dict, revision: str) -> dict:
    targets = [t for rel in sorted(per_file) for t in per_file[rel]["targets"]]
    return {"scan": scan.name, "type": "makefile", "project": scan.project_name, "revision": revision,
            "targets": targets, "stats": {"files": len(per_file), "targets": len(targets)}}
```

Wiring it in (the scanners are a closed set today, so this touches core files):

1. `cli/src/de_agent/config.py`: add the name to `SCAN_TYPES` (and a field to `ScanConfig` if it needs options).
2. `cli/src/de_agent/scanners/run.py`: a branch in `scan_one` that lists the files, calls `_per_file(...)` with your function and `assemble`.
3. `cli/src/de_agent/scanners/catalog.py`: a `render_<type>(result)` returning `{"catalog/<type>/<scan>.md": text}`; call it from `run_scan`.
4. `cli/src/de_agent/scanners/edges.py`: a branch that calls `put(table, "writers" | "readers", {...})` so the objects join the lineage.
5. Fixtures and a golden test (below), and a line in the README supported-systems table.

For SQL inside code or files use `scanners/common.analyze_sql(sql, dialect)` (sqlglot): it returns `reads`, `writes`, `calls` and `unparsed`.
When a statement cannot be parsed, fall back to `heuristic_sql` and mark the object `heuristic: true`; the catalog shows the mark.
Never guess silently.

## What the built-in code scanners cover (and do not)

- **`python`**: SQL string literals given to `execute`, `executemany`, `read_sql`, `read_sql_query`, `text()`, `spark.sql`; pandas `read_sql_table` / `to_sql`;
  pyspark `table`, `read.table`, `saveAsTable`, `insertInto`, `DeltaTable.forName` (plus `merge`/`update`/`delete` as writes), `option("dbtable", ..)`, `jdbc`,
  and `read.format(..).load(path)`, `write.parquet(path)`, `.save(path)` as path nodes (`s3://bucket/prefix/`). One owner per function (`py:<file>:<func>`),
  per file's module-level code (`py:<file>`) and per notebook (`nb:<file>`). Local variables, f-strings, `.format`, `"a" + b`, loops over a module-level list or
  `dict.items()` and `zip(..)` are unrolled (an unrolled edge is marked); anything else is counted as `unresolved_sql`, never guessed.
- **Notebooks**: every code cell of an `.ipynb` (magics other than `%sql` are dropped, cell numbers appear in the edge's file) and Databricks source notebooks
  (`# MAGIC %sql` cells are parsed as SQL; line numbers are real file lines).
- **Util attribution** (`airflow` and `python`): a call to a function in another scanned file (through `import`, `from x import y`, relative imports, `__init__` re-exports)
  passes its literal arguments in, so `utils.run_proc("etl.sp_x")` becomes the `CALL etl.sp_x()` the helper executes. The caller gets the edge with
  `via: util:<module.func>` (two calls deep: `via: util:a.f>b.g`, `hops: 2`); a third call is not followed. In an Airflow scan the callers are the DAGs: calls in task callables,
  `python_callable=` with `op_args` / `op_kwargs`, helper functions in the DAG file and loops over config lists. An argument it cannot resolve counts as `unresolved_util_args`.
- **`terraform`**: every `resource` is catalogued (type, name, file:line). DMS tasks (selection rules, S3 or database targets), Firehose, Glue tables, crawlers and jobs
  (`s3://` default arguments; the direction comes from the argument name and is marked unclear when it has none), MWAA, BigQuery views, Pub/Sub subscriptions, Snowflake
  tasks/views/pipes and Databricks SQL queries become edges where their attributes name the objects. Variable defaults, locals and resource names resolve; anything else is
  listed under "Unresolved references". `python-hcl2` is used when installed, otherwise a built-in reader produces the same result.
- **Limits**: no dataflow beyond string values (a table name read from a file, an environment variable or a function's return value is unresolved); methods on objects
  (`self.db.run(..)` on an injected client) are not followed; a call through `getattr`, `importlib` or `*args` is not followed; loops over more than 500 items are
  not unrolled; correlated arguments from two different `for x in LIST` loops are paired only for `dict.items()` / `zip` / tuple targets.
- `edges.ignore_tables: [etl.load_state, ..]` in `de-agent.yaml` keeps those tables' own edges but stops `dea impact` from traversing through them.

## Rules

- **Parse, never run.** No `import` of the scanned code, no `exec`, no subprocess in the scanned repo, no network. Python is read with `ast`,
  SQL with sqlglot, JSON and YAML as data. A scanner must be safe to point at a repo you do not trust.
- **Read-only.** A scan writes only inside the hub (`catalog/`, `.dea/`), never into the scanned folder.
- **Deterministic.** Sorted output, no timestamps inside results, so a rescan of unchanged code changes no bytes.
- **Honest about uncertainty.** Dynamic names (an f-string table, a loop) are reported as unresolved or as a pattern, not expanded by guess.
- **Cheap on a big repo.** Parse per file, cache by hash, skip generated folders. The perf test scans 1,000 synthetic DAGs (`pytest -m perf`).

## Fixtures and golden renders

Put a tiny fictional repo under `cli/tests/fixtures/<type>_repo/` that exercises the happy path and the awkward cases (dynamic names,
a syntax error, a file that is not SQL). Test the per-file function directly, then `scan_one` on the fixture, then compare the rendered
`catalog/` page against a golden file (the same `DEA_UPDATE_GOLDEN=1` convention as collector renders), and assert the edges
`dea edges <table>` should print. Add a test that a second scan parses zero files.
