---
name: schema-change
description: Plan a schema change (rename a column, widen a type, add or drop a column) using expand, migrate, contract. Lists every reader and writer with file:line from dea impact and explorer greps, checks that values fit before a type change, refuses to alter a table with blocked sessions, and writes the expand-to-verify plan with the contract as a separate, later plan. Use when someone asks to rename or retype a column or table.
---

# schema-change

## When to use
A change request such as "rename orders.customer_id to cust_id" or "widen amount to numeric(18,4)". Not for adding a
new pipeline (`new-pipeline`) or recomputing history (`backfill`), though a schema change usually contains a backfill.

## Inputs
- The change in words, the table, and the source that owns it (a writable source must exist for SQL steps).
- Whether the table has a different owner team: if so, the human must confirm they have signed off.

## Steps
1. **Impact.** `dea impact <schema.table>` for downstream tables and `dea edges <schema.table>` for writers. Spawn
   `de-agent:explorer` to grep the column name across every scanned repo (DAGs, dbt models, SQL files). Produce one
   list of readers and writers with file:line. A reader you cannot find is a risk: say so.
2. **Pre-checks.** Read the snapshot `blocking` section for the table: any blocked session or long transaction means
   STOP and tell the human to retry later. For a type change run `dea query` and require zero overflow, for example
   `select count(*) from t where length(col::text) > N` or `where abs(col) >= 10^(P-S)`. A non-zero count stops the plan.
3. **Pattern: expand, migrate, contract.** The plan holds only the first five; the sixth is a separate later plan.
   1. Expand: add the new column (or a view alias). It is nullable and has no default rewrite on a big table.
   2. Backfill it (use the `backfill` skill's chunking for large tables).
   3. Dual-write: ```` ```diff file=<path> repo=<scan> ```` steps in each writer so both columns are filled.
   4. Switch readers: one diff step per reader file, found in step 1.
   5. Verify: SQL verifies (old and new columns agree: `expect="= 0"` on the mismatch count) and, where it helps,
      a shell verify such as `dbt parse` or the DAG parse check.
   6. Contract (drop the old column): a different plan, dated after readers have run clean for a few days. Write
      its draft only if the human asks now.
4. **Draft** with the `plan` skill. Every step has a verify, and `## Rollback` undoes expand by dropping only the
   new column (nothing reads it yet) and reverse-applies the diffs. Mark risk `medium` or `high` for any table with
   downstream readers.
5. `dea plan check <id>` and hand the human the `dea approve <id>` command.

## Output
The reader and writer list (file:line), the pre-check results, the six-stage outline with which stage is in this
plan, the plan id, the check result, and the approve command.

## Never
- Never drop and add a column in the same plan. Never rename in place while a reader exists.
- Never ALTER a table that has blocked sessions in the snapshot.
- Never skip the value-fit check on a type change. Never edit the source table without the owner's sign-off.
- Never run `dea approve` (human only) or `dea apply` (executor only).
