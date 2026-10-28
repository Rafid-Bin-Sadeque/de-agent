---
name: new-pipeline
description: Plan a new pipeline from a source, a target and a schedule. Reads the repo conventions (root and nested CLAUDE.md, the nearest similar DAG from the catalog), writes diff steps for the DAG file, dbt source and staging model, extract SQL and a parse test, verifies with parse checks and a post-apply scan showing the new edges, and refuses credentials in code, overlapping maintenance windows, and tables another DAG already owns. Use when asked to add an extract, a load or a model.
---

# new-pipeline

## When to use
A new DAG, extract, load or dbt model is wanted: a source, a target and a schedule are known. Not for changing an
existing pipeline's columns (`schema-change`) or recomputing history (`backfill`).

## Inputs
- The source (system and table), the target (`schema.table`), the schedule (cron or dataset), and the owner.
- The scanned repo to add it to (a scan name from `de-agent.yaml`).

## Steps
1. **Conventions.** Read the repo's root `CLAUDE.md` and the nested one nearest the target folder. Spawn
   `de-agent:explorer` to find the nearest similar DAG in `catalog/` (same tags or folder) and report its
   structure: default args, connection-id convention, callbacks, naming, how its SQL is kept.
2. **Ownership.** `dea edges <target>`: if another DAG already writes the target, STOP and tell the human. The
   hub's `conventions.md` may define a maintenance window: the schedule must not overlap it.
3. **Write diff steps** with the `plan` skill, each ```` ```diff file=<path> repo=<scan> ```` and one file per step:
   - the DAG file, copied from the similar DAG's shape, using the repo's connection-id convention;
   - a dbt source plus a staging model, if the repo is dbt;
   - an `extract` SQL file, if the repo keeps SQL in files;
   - a DAG parse test, if the repo has a tests folder.
   New files use `--- /dev/null`. Every patch must pass `git apply --check`, which `dea plan check` runs.
4. **Verify steps** in the plan:
   - shell verify: ```` ```shell verify expect_exit=0 ```` with
     `python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" <file>` (quotes keep the `;` literal);
   - `dbt parse` as a shell verify when the repo is dbt;
   - after apply (not in the plan): `dea scan`, then check the new DAG or model and its expected edges appear.
5. **Rollback** reverse-applies the diffs (automatic) and says so in `## Rollback`; note that deployed DAG runs are
   not undone.
6. **New project?** If the repo is not onboarded, run `onboard-project` after the plan is applied.
7. `dea plan check <id>` and hand the human the `dea approve <id>` command.

## Output
The conventions found (with the similar DAG's path), the ownership check, the files in the plan, the plan id, the
check result, the approve command, and the post-apply `dea scan` check to run.

## Never
- Never put credentials in code; use the repo's connection-id convention. The check refuses secret-looking text.
- Never schedule over the target's maintenance window.
- Never write to a table another DAG owns without saying so in the plan.
- Never commit: apply leaves the changes in the working tree for the human to review.
- Never run `dea approve` (human only) or `dea apply` (executor only).
