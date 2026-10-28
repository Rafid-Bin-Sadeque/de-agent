---
name: pipeline-check
description: Health verdict for one DAG, table or dbt model. Gathers the catalog line, lineage edges, snapshot sections (runs, failures, freshness, tests) and dea diff, then answers healthy, degraded or broken with evidence lines. Use for "is extract_shop healthy?" or "did last night's run work?".
---

# pipeline-check

## When to use
The user names one pipeline object (DAG id, table, dbt model) and asks whether it is OK.

## Inputs
- The name. If it is ambiguous, `dea find <name>` and ask which one.
- Optional: how fresh the answer must be (default: take a fresh snapshot if the platform pages are older than the hub's max age).

## Steps
1. Spawn `de-agent:runner` with: `dea find <name>`, `dea edges <table>` or `dea impact <name>`, `dea snapshot`
   (only the relevant source if it is large), `dea diff`, and the relevant sections of `platform/*.md`:
   latest runs and durations, failures_24h with log tails, freshness, import errors, dbt tests and results.
2. Read the runner's evidence. Tag each claim V (a command output) or I (inferred).
3. Check upstream first: if the object reads from something failing or stale, that is the cause, not the object.
4. Verdict rules:
   - **broken**: latest run failed, or a blocking signal (import error, failing tests, stale beyond its schedule).
   - **degraded**: succeeding but a warning signal (slower than usual, retries, a dependency stale, paused).
   - **healthy**: latest run succeeded, fresh within schedule, no signals, upstream OK.
5. If broken or degraded and the cause is not obvious, say "run incident-triage" instead of guessing.

## Output
```
Verdict: healthy | degraded | broken   (snapshot <id>, generated <ts>)
Evidence:
- <line quoted from snapshot/diff/query>  [V]
Upstream: <ok | what is wrong>
Next: <one line>
```

## Never
- Never give a verdict without at least one quoted evidence line.
- Never change anything: read-only. Never run `dea apply` or `dea approve`. Never print secrets.
