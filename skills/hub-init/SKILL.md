---
name: hub-init
description: Create a new de-agent hub for a team. Asks for the folder of repos and the data sources, drafts de-agent.yaml (secret references only), then runs dea init, dea doctor, dea scan and dea snapshot and drafts map.md. Use when the user wants to set up de-agent or has no hub yet.
---

# hub-init

## When to use
The user says "set up de-agent", "create a hub", or `dea` reports "no hub found".

## Inputs (ask the human; do not guess)
- Hub folder (where the markdown hub will live; a new git repo is a good home).
- Folder that holds the code repos (Airflow, dbt, SQL) to scan, and which is which.
- Data sources to snapshot: for each, type (postgres, airflow, dbt), host/port/db/user, and HOW the secret is
  provided: `env:VAR`, `pgpass`, or `file:/abs/path` (mode 0600). Never ask for the secret value itself.
- Optional: an ssh host alias if a source is only reachable from a bastion (`exec: ssh:<alias>`).

## Steps
1. `dea init <hub-folder> --name "<platform name>"` (never overwrites existing files).
2. Draft `de-agent.yaml` in the hub: `sources:` and `scans:` from the answers. Secret fields hold references
   only (`env:`, `pgpass`, `file:`). A literal value is a config error; never write one, never echo one back.
3. `dea doctor`. Fix what it reports (config shape, unreachable source, missing psql). Ask the human to export
   env vars or fix pgpass themselves; do not read secret files.
4. `dea scan`, then `dea snapshot`. Report per-source status and any source that failed.
5. Draft `map.md` from the results: one line per project (what it is, where it runs), a platform line pointing at
   `platform/index.md`. Use `platform/index.md`, `catalog/` and `dea find` as the evidence, not guesses.
   Keep it under the token budget (`hub.map_budget_tokens`, default 1500).
6. For each scanned project, make sure a profile exists: `dea scan` creates the generated header; leave the human
   body for the `onboard-project` skill.
7. `dea lint`. Fix errors until it exits 0. Warnings may remain; list them.

## Output
- Hub path, sources (name, type, status), scans (name, files, DAGs/models found).
- The `map.md` you drafted, shown in full.
- `dea lint` result. Next step suggestion: run `onboard-project` for each repo.

## Never
- Never write a password, token or key value into any file or into the transcript.
- Never overwrite an existing hub file; `dea init` skips what exists. Show a diff and ask before editing it.
- Never configure a writable source (`writable: true`) unless the human asks for one for plans.
- Never run database writes. Never run `dea approve` or `dea apply`.
