---
name: add-source
description: Add a live system the hub does not monitor yet (Snowflake, BigQuery, Databricks, MySQL, SQL Server, GCP, Kubernetes, Dagster, Fivetran, or anything with SQL, a CLI or an HTTP API). Finds an existing pack or drafts one, checks it, proposes the de-agent.yaml source block with secret references only, tests it read-only and iterates. Use when the user says "we also use X" or "monitor X".
---

# add-source

## When to use
The user names a system that is not in `de-agent.yaml` `sources:` ("we also run a MySQL database for reviews", "we use Snowflake").
The system has SQL, a command-line client or an HTTP API. A *pack* (a small YAML file) describes what to read; no code is written.

## Inputs
- Which system, and which parts of it to watch (named resources only: warehouses, datasets, namespaces, job ids, groups).
- How the connection is made (host or URL, role or user) and HOW the secret is provided: `env:VAR`, `file:/abs/path` (mode 0600) or
  `none`. Never ask for the secret value; ask the human to put it in their environment or `.env` themselves.

## Steps
1. `dea pack list`. If a pack fits (`mysql`, `snowflake`, `bigquery`, `databricks`, `databricks-system`, `mssql`, `gcp`, `kubernetes`,
   `dagster`, `fivetran`, `postgres-lite`), use it: `dea pack show <name>` to see its params and sections. Note its `validated:` level
   (`fixtures` means it was never run against a live system: say so).
2. No pack fits: `dea pack new <name> --kind sql|cli|http`, then draft the sections in `packs/<name>.yaml` from the system's DOCUMENTED
   system views, CLI or API (use the explorer agent or the docs you already know; do not invent tables or flags). Every SQL is one
   `select`/`show`; every `run:` is a read verb of a known CLI; every http section is a `get:` or a GraphQL `query`. Scope each section
   to named resources through `params:` (a list the user supplies); no account-wide listings.
3. `dea pack check <name>`. Fix every reported problem; a pack that fails the check never runs.
4. Propose the `de-agent.yaml` source block (show the diff, then add it). Secret fields hold references only:
   ```yaml
   - name: reviews-db
     type: sql                   # or cli / http
     pack: mysql
     conn: {url: "mysql+pymysql://monitor@reviews-db.internal:3306/reviews", password: "env:REVIEWS_DB_PASSWORD"}
     options: {}                 # the pack's params
   ```
   Ask the human to export the variable (or put it in the hub's `.env`) and install the extra the pack needs
   (`pip install 'de-agent[mysql]'`; `dea doctor` names it).
5. `dea doctor`, then `dea pack test <source>`: it runs the source once, read-only, and prints the rendered page and the signals.
6. Iterate: fix a section that errors, tighten a threshold, add a signal. Re-run `dea pack check` and `dea pack test`. When it is
   good, `dea snapshot` and `dea lint`.

## Output
- The pack used or created (name, kind, `validated:` level), the source block added, `dea pack check` and `dea pack test` results,
  and anything the human still has to do (export a secret, install an extra, grant a read-only role: point to the matching block in
  `docs/read-only-access.md` in the de-agent plugin folder, online at
  https://github.com/Rafid-Bin-Sadeque/de-agent/blob/main/docs/read-only-access.md).

## Never
- Never put a write statement or a mutating verb in a pack (`insert`, `delete`, `create`, `kubectl delete`, `run-now`, a POST mutation).
  The runtime refuses them, but do not write them.
- Never write a password, token or key into any file or the transcript: `env:` / `file:` references only.
- Never list a whole account, cluster or project when named resources will do.
- Never mark `validated: live` unless the pack was really run against that system; fixtures-only packs say `fixtures`.
- Never run `dea approve` or `dea apply`; adding a source is read-only.
