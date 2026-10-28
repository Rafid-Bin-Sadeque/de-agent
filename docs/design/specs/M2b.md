# M2b spec: code and shell steps in plans; backfill, schema-change and new-pipeline skills

Depends on M2 (plans, approve, apply, guards). Zero company data, and the examples are Acme demo only.

## 0. M3.3 carry-overs (first, separate commit `M3.5: redshift heavy-section robustness`)

1. The single-pass workload query gets its own timeout, `options.heavy_section_timeout_s` (default 300). One heavy scan per TTL window is acceptable; a 120 s cap is not on a loaded cluster.
2. Heavy-section reuse picks, per section, the latest snapshot where THAT section succeeded (not just the latest non-failed snapshot), so a partial snapshot never hides an older good result.
3. The spill bound probe uses the same heavy timeout and is skipped when the single pass supplied the bound.
4. A source-level `options.light: true` skips heavy sections entirely (no scan, rendered "skipped (light mode)"), for clusters where the operator wants zero heavy reads.
5. Fixture tests only. NO live Redshift runs in this milestone.

## 1. Plan step kinds beyond SQL

Today a plan can only carry SQL. Real fixes also change code (DAGs, dbt models) and run tools (`airflow dags backfill`, `dbt build`).

**Diff steps:**
```diff file=airflow/dags/extract_shop.py repo=pipelines
--- a/airflow/dags/extract_shop.py
+++ b/airflow/dags/extract_shop.py
@@ …
```
- `repo=` names a configured scan (its path is the git root, resolved from de_agent.yaml). `file=` is relative to that repo.
- `dea plan check` runs `git apply --check` against the current tree and errors if the patch doesn't apply.
- The hash covers the normalized patch text (strip trailing whitespace per line, LF endings).
- `dea apply` runs `git apply` (never commits) and records `git diff --stat` in applied.json.
- `--rollback` reverse-applies the diff steps, newest first.

**Shell steps:**
```shell exec=local cwd=demo timeout=900
docker compose exec -T airflow airflow dags backfill extract_shop -s 2026-10-01 -e 2026-10-03
```
- The command text is hashed and approved like SQL. `dea apply` runs it with `shell=False`, using `shlex.split`. Pipes, `&&`, `;`, redirection and `$(…)` are refused by `plan check` (one command per step). It runs under the given `exec` (`local` or `ssh:<host>`), `cwd` and timeout.
- stdout/stderr tails (last 40 lines, secrets redacted) go into applied.json, and the exit code must be 0.
- `plan check` flags these as `destructive` (and errors under `risk: low`): `rm`, `drop`, `truncate`, `--full-refresh`, `dags delete`, `db reset`, `clear` (airflow tasks clear), `aws … delete|remove|terminate`.

**Verify for shell:** a `verify` block may be SQL (as now) or a shell command with `expect_exit=0` or `expect_stdout~=<regex>`.

**Guards:** unchanged. `dea apply` runs only from the executor, and approval is human-only. `sql_guard` sees nothing new because the executor runs only `dea apply`.

## 2. Skills

Each SKILL.md is ≤ 150 lines and has the same sections as M2's skills. Each produces a plan through the `plan` skill, never ad-hoc writes.

**`backfill`**
1. Takes a target (table, dbt model or DAG) and a date range.
2. Finds the writers with `dea edges`, and the downstream with `dea impact`.
3. Determines idempotency from the writer's code, via the explorer:
   - delete+insert by partition
   - MERGE / upsert on key
   - dbt incremental with `unique_key`
   - Airflow logical-date-templated SQL
   - otherwise *not idempotent*, which means stop and ask the human.
4. Sizes it with `dea query` (row counts per day in the source range).
5. Chunks by day or week so each chunk stays under ~30 min.
6. Writes a plan with one step per chunk, each with a verify (target count per day matches source within tolerance) and a rollback per chunk.
7. Checks the current state: no overlapping running DAG run (from the snapshot), and it is not in the busy window if the hub's `conventions.md` defines one.
8. **Never-dos:**
   - `--full-refresh` of an incremental model unless the human asked for it
   - clearing upstream tasks
   - a backfill that runs downstream DAGs automatically without saying so
   - running all chunks as one step

**`schema-change`**
1. Takes a change request, e.g. "rename orders.customer_id to cust_id" or "widen amount to numeric(18,4)".
2. Impact: `dea impact <table>` plus explorer greps for the column across the scanned repos, giving the list of readers with file:line.
3. Uses the expand → migrate → contract pattern by default:
   1. Add the new column (or a view alias).
   2. Backfill it.
   3. Dual-write: diff steps in the writers.
   4. Switch readers: diff steps.
   5. Verify.
   6. Contract (drop the old column) as a separate plan, dated later.
4. Type changes check value fit first, with `dea query` (max length / precision overflow count must be 0).
5. **Never-dos:**
   - drop and add in one plan
   - in-place rename while readers exist
   - ALTER on a table with blocked sessions (check the snapshot `blocking` section)

**`new-pipeline`**
1. Takes a source, a target and a schedule.
2. Reads the repo conventions: root and nested CLAUDE.md, plus the nearest similar DAG from the catalog (same tags/folder).
3. Writes diff steps:
   - the DAG file
   - a dbt source and staging model if the target repo is dbt
   - an `extract` SQL file if the repo uses them
   - a DAG parse test if the repo has a tests folder
4. Plan verify steps:
   - shell `python -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" <file>`
   - `dbt parse` if dbt
   - after apply, `dea scan` shows the new DAG/model with the expected edges
5. Updates the project profile via `onboard-project` if it is a new project.
6. **Never-dos:**
   - credentials in code (use the repo's connection-id convention)
   - a schedule overlapping the target's maintenance window
   - writing to a table that another DAG already owns, without saying so (check `dea edges`)

## 3. Demo and tests

- **Unit:**
  - parsing diff and shell steps
  - `plan check` errors: patch not applying, chained shell, destructive command under `risk: low`
  - hashing parity
  - `apply` ordering across SQL/diff/shell
  - rollback reverse-applies diffs
  - shell verify (`expect_exit`, `expect_stdout~=`)
  - redaction of shell output
- **Demo integration** (`DEA_DEMO=1`, one run at the end): fault 01 fixed the code way. A scripted plan with a diff step (`customer_id` → `cust_id` in `extract_shop` plus the matching `raw.orders`/`stg_orders` changes the fault's EXPECTED lists) and a shell step that triggers `extract_shop`. Approve via pty, apply, then snapshot/diff shows the success. Revert with `--rollback`, and git status is clean afterwards.
- `dea lint` on the demo hub, hooks under 3.9, `claude plugin validate . --strict`.

## 4. Commit
`M2b: diff and shell plan steps, backfill / schema-change / new-pipeline skills`.
