# Writing a scanner

A scanner reads a repo's files and extracts structure that code already encodes: which job reads or writes which table, what depends on
what. `dea scan` turns the results into `catalog/` pages and table-level edges (`catalog/edges.json`), which `dea edges`, `dea impact` and
`dea find` read. Built in: `airflow`, `dbt`, `sql`.

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
