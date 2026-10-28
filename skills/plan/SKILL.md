---
name: plan
description: Turn a triage result or a request into a plan file (.dea/plans/<id>.md), run dea plan check, and hand the human the exact dea approve command to run in their own terminal. After approval, hand off to the executor agent and report the applied result plus the post-apply diff. Use whenever a change to a database or data pipeline is needed.
---

# plan

## When to use
Any change that writes: DDL, DML, a backfill, a config change. Reads never need a plan.

## Inputs
- The goal, the evidence (from incident-triage or the request), and the sources involved.
- Which source to write through: a postgres source with `writable: true` in `de-agent.yaml`. If none exists,
  tell the human; do not use a read-only source and do not ask for credentials.

## Plan file format
`.dea/plans/<id>.md`, id is a slug like `2026-10-27-fault01-schema-drift`:
````
---
id: <slug, same as the file name>
title: <one line>
risk: low | medium | high
sources: [<source names>]
statement_timeout: 300s
---
## Why
<root cause and evidence lines, cited from snapshot/diff/query output>
## Steps
### 1. <title>
```sql source=<name>
<one or more statements>
```
```sql verify source=<name> expect="= 0"
<one read statement returning one value>
```
## Rollback
```sql source=<name>
<statements that undo the steps>
```
## Not doing
<unsafe alternatives considered and rejected, with why>
````
### Code and shell steps
Besides SQL, a step can carry a code change or one command (both are hashed and approved like SQL):
````
```diff file=airflow/dags/extract_shop.py repo=pipelines
--- a/airflow/dags/extract_shop.py
+++ b/airflow/dags/extract_shop.py
@@ -10,1 +10,1 @@
-old line
+new line
```
```shell exec=local cwd=. timeout=900
docker compose exec -T airflow airflow dags trigger extract_shop
```
```shell verify expect_exit=0
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" dags/extract_shop.py
```
````
- `repo=` is a scan name; the patch paths are relative to that scan's path and `file=` must be the only file it
  touches (one file per diff step, one diff step per file). `dea plan check` runs `git apply --check`. Apply never
  commits; `dea apply <id> --rollback` reverse-applies the diffs, newest first, then runs `## Rollback`.
- A shell step is ONE command: no pipes, `&&`, `;`, redirection, `$(...)` or newlines outside quotes. `exec=` is
  `local` or `ssh:<host>`, `cwd=` is relative to the hub. `rm`, `drop`, `truncate`, `--full-refresh`, `dags delete`,
  `db reset`, `tasks clear` and `aws ... delete|remove|terminate` are flagged destructive (an error under `risk: low`).
- A shell `verify` takes `expect_exit=N` (default 0) and/or `expect_stdout~=<regex>`.
- A plan with only code steps needs no `sources:`.

Every SQL block needs `source=`. Verify blocks must be reads. Use `transaction: false` on a fence only for
statements that cannot run in a transaction (VACUUM, CREATE INDEX CONCURRENTLY). `expect` accepts `= N`, `!= N`,
`> N`, `>= N`, `< N`, `<= N`, `between A and B`, `is null`, `is not null`. Mark `risk: low` only if nothing is
destructive (DROP, TRUNCATE, DELETE without WHERE are rejected in low-risk plans).

## Steps
1. Write the plan file with the Write tool. Smallest fix that works; one verify per step that proves the effect.
2. `dea plan check <id>`. Fix every error and re-run until it is clean. Read the statement table: types, write/read,
   destructive flags.
3. Tell the human, verbatim: "Review `.dea/plans/<id>.md`, then run this in your own terminal: `dea approve <id>`".
   Stop and wait. You cannot approve, and the hooks deny every attempt to touch approval files or the key.
4. When the human says it is approved, `dea plans` should show `approved`. Then spawn `de-agent:executor` with the
   plan id and path (never a paraphrase) and ask it to apply, snapshot and diff.
5. Report the executor's result: per-step status, verify results, and the relevant `dea diff` lines.
   If a step failed, show the error and offer `dea apply <id> --rollback` via the executor if the plan has a Rollback.

## Output
- The plan path, the `dea plan check` result, the exact approve command, then (after apply) the applied result and diff.

## Never
- Never plan, or make, a change that removes or weakens a dbt test (delete it, `severity: warn`, `where:`,
  `enabled: false`, looser `warn_if`/`error_if`) as the fix for a failing test or build. If the test itself is wrong,
  the plan carries a `diff` step for a human to review and approve; adding or tightening tests is fine.
- Never run `dea approve`, write or edit `.approved` files, or read the approval key.
- Never run `dea apply` yourself: only the executor agent may.
- Never edit a plan after it is approved; write a new plan instead (a changed statement fails apply anyway).
- Never put secrets in a plan. Never plan a write to a source that is not `writable`.
