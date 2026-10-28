---
name: incident-triage
description: Take a symptom in plain words ("orders mart is stale", "extract_shop is failing") and find the root cause with evidence. Fresh snapshot and diff, ranked hypotheses from signals and lineage, cheap runner checks, a cited root cause, a safe fix drafted as a plan, and the unsafe fixes rejected. Use when something is broken and the cause is unknown.
---

# incident-triage

## When to use
A symptom, not a diagnosis: stale data, a failing DAG, a slow job, a hanging run.

## Inputs
- The symptom in the human's words, and when it started if known.

## Steps
1. **What changed.** Spawn `de-agent:runner`: `dea snapshot`, then `dea diff`. New signals, new failures, config
   drift and added rows are your first leads. Read the matching `platform/*.md` sections and failure log tails.
2. **Hypotheses, ranked.** From the signals and from lineage (`dea edges`, `dea impact`), list candidates, UPSTREAM
   first: a source schema change, a paused DAG, a dataset that never fires, a failing test, a lock or bloat.
   Rank by (evidence you already have) then (cost to check). Always include "missing index / stale table stats"
   for the tables on the failing path.
3. **Check, cheapest first.** Give the runner one check per hypothesis (`dea query`, a log tail, `dea find`). Stop at
   the first hypothesis the evidence confirms; do not run every check for the sake of it.
4. **Root cause.** One paragraph, every claim cited to a command output or a snapshot line, tagged V or I.
5. **Safe fix.** Invoke the `plan` skill to draft the fix as a plan file. Put the evidence lines in `## Why`.
6. **Unsafe fixes you rejected.** In the plan's `## Not doing`, list each tempting shortcut and why it is unsafe
   (editing data or tests to make the symptom disappear, destructive DDL, resetting watermarks, changing a
   source owned by another team without their sign-off).

## Output
```
Symptom: ...
Root cause: ... (evidence: [V] <quoted line>, [V] <quoted line>)
Ruled out: <hypothesis> because <evidence>
Fix: plan <id> drafted (risk <low|medium|high>)
Not doing: <rejected fix> because ...
```

## Never
- Never state a root cause without a cited evidence line. If you cannot confirm one, say what is still unknown.
- Never run a write. Never "fix" by editing tests, dropping constraints or truncating tables.
- Removing or weakening a test (deleting it, `severity: warn`, `where:`, `enabled: false`, a looser `error_if`) is
  NEVER a fix for a failing test or a failing build. The failing test is the symptom detector: find why it fails.
  If you believe the test itself is wrong, say so with evidence and write a plan with a `diff` step for a human to
  approve; do not edit test files or test YAML yourself (a hook denies it).
- Never run `dea approve` (human only) or `dea apply` (executor only).
