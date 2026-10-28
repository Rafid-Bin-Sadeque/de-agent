# Skills and agents

Skills are workflows Claude follows when your request matches their description. You do not type a command; say what you want.
Every skill is read-only until it hands a plan to a human. None can run `dea approve` (human only); only the `executor` agent runs `dea apply`.

| skill | say something like | what it does | never |
|---|---|---|---|
| `hub-init` | "set up de-agent", "create a hub" | Asks for your repos folder and data sources, drafts `de-agent.yaml` (secret references only), runs `dea init`, `dea doctor`, `dea scan`, `dea snapshot`, drafts `map.md` | write a secret value; overwrite a hub file; configure a writable source unasked |
| `onboard-project` | "add this repo to the hub", "onboard this project" | Adds a scan, writes the profile page with you, rescans, proposes an integration page per new edge | touch generated blocks; guess in the profile body (`TODO(human)` instead) |
| `hub-sync` | "sync the hub", end of session | Builds one proposed diff for the hub from `dea diff`, applied plans and git changes, for you to accept | write a claim without evidence; edit `catalog/` or `platform/` |
| `pipeline-check` | "is extract_shop healthy?", "did last night's run work?" | Catalog line, lineage, snapshot sections and `dea diff`, then healthy, degraded or broken with quoted evidence | give a verdict without an evidence line; change anything |
| `incident-triage` | "orders mart is stale", "this DAG is failing" | Fresh snapshot and diff, ranked hypotheses, cheap checks via `runner` (including indexes and stats), a cited root cause, a safe fix drafted as a plan | state a cause without evidence; fix by weakening a test, dropping constraints or truncating |
| `plan` | "write a plan to fix it" | Writes `.dea/plans/<id>.md`, runs `dea plan check`, hands you the exact `dea approve <id>` to run in your own terminal; after approval hands off to `executor` and reports the post-apply diff | approve, apply, edit an approved plan, put secrets in a plan, plan a write to a non-writable source |
| `backfill` | "backfill orders for last week", "rebuild the mart for these dates" | Finds writers and downstream, proves the writer idempotent from code, sizes the range, chunks it (~30 min each), one step plus verify plus rollback per chunk | `--full-refresh` unasked; reset watermarks or clear upstream tasks; run all chunks as one step |
| `schema-change` | "rename orders.customer_id", "widen amount to numeric(18,4)" | Expand, migrate, contract: lists every reader and writer with `file:line`, checks values fit before a type change, refuses tables with blocked sessions; the contract is a separate, later plan | drop and add in one plan; rename in place while a reader exists; skip the value-fit check |
| `new-pipeline` | "add an extract for X", "new dbt model" | Reads repo conventions and the nearest similar DAG, writes diff steps (DAG, dbt source and staging, SQL, parse test), verifies with a post-apply scan showing the new edges | credentials in code; schedule over a maintenance window; write to a table another DAG owns without saying so; commit |
| `audit` | "why is the DB slow?", "do we need a bigger instance?" | Postgres and Redshift packs: finds the bottleneck layer first and hardware last, writes `audits/<source>-<date>.md` with a then-versus-now table | recommend hardware before ruling out earlier layers; run VACUUM, resize or kill sessions; claim a number it did not query |

## Agents

| agent | model | tools | job |
|---|---|---|---|
| `runner` | sonnet | Bash, Read, Grep, Glob | Read-only lookups on the live platform (`dea query/find/edges/impact/snapshot/diff`, log tails). Returns a compact answer with the command used |
| `explorer` | sonnet | Read, Grep, Glob, Bash | Code and hub questions ("which DAG writes this table"): catalog first, then code. Returns conclusions with `file:line` |
| `executor` | sonnet | all | Carries out an approved plan: `dea plan check`, `dea apply`, post-apply `dea snapshot` and `dea diff`, only the code edits the plan names. Stops and reports on anything the plan does not cover. The only agent allowed to run `dea apply` |

Hooks apply to every agent, including the main session ([SAFETY.md](SAFETY.md)).
