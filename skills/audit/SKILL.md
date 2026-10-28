---
name: audit
description: Capacity and health audit of a Postgres or Redshift source. Four sections (Postgres - workload, tables, locks and connections, settings versus size; Redshift - config and infrastructure, workload attribution, pressure signals, inventory and hygiene), a verdict that finds the bottleneck layer first and hardware last, and a written audits/<source>-<date>.md with a then-versus-now table against the previous audit. Use for "why is the DB slow", "do we need a bigger instance or another node", or a periodic health check.
---

# audit

## When to use
A database feels slow or is growing, a sizing or resize question, or a scheduled health check. Two packs:
`references/postgres.md` (a `type: postgres` source) and `references/redshift.md` (a `type: redshift` source).

## Inputs
- The source name (a read-only Postgres or Redshift source from `de-agent.yaml`). For Redshift also the AWS source
  name when one is configured: its page shows DMS, Glue, Firehose and alarm state around the warehouse.
- Optional: the symptom or the question (for example "should we add CPU").

## Steps
1. Read the pack for the source type (`references/postgres.md` or `references/redshift.md`): the queries and the
   verdict rules.
2. Spawn up to four `de-agent:runner` agents IN PARALLEL, one per section R1 to R4, each told the source and
   the section's queries from the pack. Each returns the numbers and the exact commands.
3. Apply the pack's verdict rules yourself (the main session decides): bottleneck layer first, hardware last.
   Postgres: workload and queries, then tables and indexes, then locks and connections, then settings.
   Redshift: orchestration, fixable waste, CPU, memory, I/O, connections, storage; a sizing claim needs at least 7 days
   of data with weekday and weekend reported separately; never attribute load by user name alone.
4. Find the previous audit `audits/<source>-*.md` (latest date). Build a then-vs-now table for the headline metrics.
5. Write `audits/<source>-<YYYY-MM-DD>.md`: verdict first, evidence per section tagged V or I, the then-vs-now
   table, recommended changes ranked by effort and risk. Any change is a plan (`plan` skill), never an inline fix.

## Output
```
Verdict: <one line: bottleneck layer and the action, or "no change needed">
R1..R4 (Postgres: workload / tables / locks+connections / settings; Redshift: config+infra / workload attribution /
pressure / inventory+hygiene): <2 lines each, numbers quoted>
Then vs now: <table>
Recommendations: <ranked, each a candidate plan>
File: audits/<source>-<date>.md
```

## Never
- Never recommend hardware before the pack's earlier layers are ruled out with numbers.
- Never run writes (VACUUM, ANALYZE, REINDEX, killing sessions, WLM or resize changes). Those go through a plan.
- Never claim a number you did not get from a query. If an extension or view is missing, say so.
- Redshift: never size from fewer than 7 days of data, never blame a user name you have not identified.
