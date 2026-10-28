---
name: backfill
description: Plan a backfill of a table, dbt model or DAG over a date range. Finds the writers and the downstream, proves the writer is idempotent from its code, sizes the range with dea query, chunks it so each chunk stays under about 30 minutes, and writes one plan with a step, a verify and a rollback per chunk. Use when data for past dates is missing or wrong and must be recomputed.
---

# backfill

## When to use
A table, dbt model or DAG needs past dates recomputed (a gap after an outage, a fixed bug, a new column). Not for
the first load of a new pipeline (use `new-pipeline`) or for a schema change (use `schema-change`).

## Inputs
- The target (`schema.table`, a dbt model, or a DAG id) and the date range (inclusive, in the table's own timezone).
- Optional: the tolerance for the per-day verify (default: exact match for counts, 0.1% for sums).

## Steps
1. **Writers and downstream.** `dea edges <schema.table>` for who writes it, `dea impact <schema.table>` for the
   readers. For a DAG or model, resolve the tables it writes first. Show both lists to the human.
2. **Idempotency, from the code.** Spawn `de-agent:explorer` on each writer and classify it as exactly one of:
   delete+insert by partition; MERGE or upsert on a key; dbt incremental with `unique_key`; Airflow SQL templated by
   the logical date; otherwise NOT idempotent. Cite file:line. If any writer is not idempotent (or you cannot tell),
   STOP and ask the human how to proceed; do not draft a plan.
3. **Size it.** `dea query <source> "select <date col>::date d, count(*) from <src> where ... group by 1 order by 1"`
   for the source range. Chunk by SIZE, not by calendar: pick chunks so each stays under about 30 minutes. A range
   that fits in one chunk stays one step. Report the row counts and estimated minutes per chunk.
4. **Current state.** Read the airflow snapshot section: no overlapping running or queued run of the DAG for these
   dates. If `conventions.md` in the hub defines a busy window, keep every chunk out of it. Say what you checked.
5. **Draft the plan** with the `plan` skill. One `### N. <date range>` step per chunk. A single step for the whole range is acceptable only when the plan
   states a size or runtime estimate (rows and minutes, from step 3) AND verifies EACH day or partition; otherwise
   write one step per chunk:
   - the run as a shell block, e.g. ```` ```shell exec=local cwd=. timeout=1800 ```` with
     `airflow dags backfill <dag> -s <start> -e <end>` (or `dbt build --select <model>` with the date vars), or
     the delete+insert SQL if the writer is SQL;
   - a verify per chunk: target count per day against the source within the tolerance (SQL verify with `expect`,
     or a shell verify with `expect_stdout~=`);
   - a rollback for each chunk in `## Rollback` (restore the range from the snapshot or re-run the previous
     logic); say plainly when a chunk cannot be undone.
6. **Say what runs downstream.** If a finished chunk triggers downstream DAGs through datasets, list them in
   `## Why` and ask the human to accept that, or add a step that pauses them.
7. `dea plan check <id>`, then hand the human the `dea approve <id>` command (the `plan` skill's steps 2 to 5).

## Output
The writers, the idempotency class with file:line, the chunk table (range, rows, estimated minutes), the plan id,
the `dea plan check` result, and the approve command.

## Never
- Never use `--full-refresh` on an incremental model unless the human asked for it in words.
- Never clear upstream tasks, delete DAG runs, or reset watermarks to force a rerun.
- Never let a backfill trigger downstream DAGs without saying so in the plan.
- Never run a range that exceeds the chunk size as one step, and never write a single step without a stated size/runtime estimate and a verify of each day or partition. Never draft a plan for a writer that is not idempotent.
- Never run `dea approve` (human only) or `dea apply` (executor only).
