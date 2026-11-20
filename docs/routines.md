# Routines: procedures, functions and views from your SQL engines

`dea snapshot` collects the code that lives inside a warehouse (procedures, functions, views, materialized views and triggers), `dea scan`
parses it into the same edge graph as your repos, and `dea diff` / `dea find` / `dea impact` work on it offline. Everything is read-only:
catalog `SELECT`s and `SHOW`s only. de-agent never calls `EXEC` or `CALL` to read a definition, never stores an unredacted body, and never
parses a non-SQL body (Python, JavaScript, Java) as SQL.

## What is collected per engine

| Source | Procedures / functions | Views | Triggers | Validated |
|---|---|---|---|---|
| `postgres` (and the `postgres-lite` pack) | `pg_proc`, bodies via `pg_get_functiondef` for text languages (`internal`/`c`: language only, no body); aggregates and window functions skipped | `pg_get_viewdef` (views and materialized views) | `pg_trigger` (non-internal) + `pg_get_triggerdef` | PG 12, 16 and 17 in containers; a live RDS PostgreSQL (500+ routines) |
| `redshift` | `pg_proc_info` (procedures, body in `prosrc`, header rebuilt, `SHOW PROCEDURE` when `prosrc` is empty); UDFs from `pg_proc` (SQL, Python, Lambda) | `pg_views` (late-binding views included) | none | a live RA3 cluster (160+ procedures, all bodies in `prosrc`); materialized views not yet seen live |
| `mysql` pack | `information_schema.routines` | `information_schema.views` | `information_schema.triggers` | MySQL 8.4 container |
| `snowflake` pack | `snowflake.account_usage.procedures` / `functions` for the databases you list | `snowflake.account_usage.views` | none | fixtures only |
| `mssql` pack | `sys.sql_modules` + `sys.objects` (`P`, `FN`, `IF`, `TF`) | same, type `V` | same, type `TR` | fixtures only |
| `bigquery` pack | `region-<r>.INFORMATION_SCHEMA.ROUTINES` for the datasets you list | `INFORMATION_SCHEMA.VIEWS` | none | fixtures only |
| `databricks-system` pack | `system.information_schema.routines` | `system.information_schema.views` | none | fixtures only |

System schemas and extension-owned objects are excluded. "Fixtures only" means the SQL was written from the documented catalog columns and
tested against hand-built result sets, not recorded from a live system. Treat the first live run as a check.

Redshift materialized views: the body comes from `pg_views` when it lists the view, else from `pg_get_viewdef`, else the row is kept by name
only (`parse: none`, counted in `meta.routines_mv_bodies_missing`). The first live cluster
had no materialized views, so which of these carries the body is still unconfirmed; the collector degrades instead of failing. Argument
names come through `array_to_string(proargnames, ',')`: Redshift cannot cast `text[]` to `varchar`.

Table edges leave out the temp tables a routine creates (`CREATE TEMP TABLE x`, `SELECT ... INTO TEMP x`, Redshift `#x`): they are
session-local, not platform tables.

## The row contract

One row per routine, keyed `[schema, name, args]` (so overloads stay apart; a trigger's `args` is `on <table>`):
`schema`, `name`, `kind` (`procedure`, `function`, `view`, `materialized_view`, `trigger`), `args` (types only, lower-case), `language`,
`returns`, `owner`, `body_hash`, `body_bytes`, `truncated`, `redacted`, `reads`, `writes`, `calls`, `parse` (`full`, `heuristic`, `none`).
`body_hash` is not volatile: a changed body is a changed row in `dea diff`. `owner` and the parse columns are volatile (ownership churn and
parser improvements are noise); the body hunk is what you read.

## Options

```yaml
sources:
  - name: warehouse
    type: postgres
    options:
      routines: true            # false skips the section entirely
      routine_kinds: [procedure, function, view, materialized_view, trigger]
      schemas: [analytics, ops] # postgres: the existing filter also applies to routines
      routines_max: 5000        # rows; over this the section errors (no silent cut)
      routines_max_bytes: 524288
      routines_ttl_h: 24        # redshift only: how long a snapshot reuses the previous routines (see below)
```

Pack sources accept the same four reserved options (`routines`, `routine_kinds`, `routines_max`, `routines_max_bytes`) next to the pack's own
params.

## Privileges (read-only logins)

| Engine | What the login needs |
|---|---|
| PostgreSQL | nothing beyond `CONNECT` and `USAGE` on the schemas (catalog definitions are readable by any role) |
| Redshift | `USAGE` on the schemas; `pg_proc_info`, `pg_views` and `svv_mv_info` are readable by any user, but a view or procedure owned by another user is only listed if the login may see it |
| MySQL | `SHOW_ROUTINE` (bodies of procedures and functions, else the row is kept without a body and a signal says so), `SHOW VIEW` on the schemas (view bodies). Triggers are only listed to a login with the `TRIGGER` privilege; that privilege can also create triggers, so grant it only if you accept that, or leave triggers out with `routine_kinds` |
| Snowflake | `IMPORTED PRIVILEGES` on the `SNOWFLAKE` database (the same ACCOUNT_USAGE grant the pack already needs; these views lag by up to a few hours) |
| SQL Server | `VIEW DEFINITION` on the database (or the schemas) |
| BigQuery | `bigquery.routines.get` / `bigquery.routines.list` (`roles/bigquery.metadataViewer` has both) |
| Databricks | `USE CATALOG` / `USE SCHEMA` and `SELECT` on `system.information_schema` |

## Redaction and the store

Bodies stay out of the snapshot JSON. Each body is normalized (CRLF to LF, trailing whitespace stripped, leading and trailing blank lines
dropped; comments and formatting stay), **redacted**, hashed, and written once to a content-addressed store:
`<hub>/.dea/routines/<hash[:2]>/<hash>.sql`. Snapshots carry only the hash, so an unchanged warehouse costs about nothing per snapshot, and
`_prune` deletes store files that no retained snapshot references.

Redaction runs before anything is written (store, pages, find index): passwords in connection strings, `password=`, quoted
`password = '...'` / `token: "..."` / `api_key` values, AWS keys, `CREDENTIALS '...'`, `IDENTIFIED BY '...'` and bearer tokens become
`<redacted>`, as does every entry of the file named by `DEA_DENYLIST_FILE` (one case-insensitive string per line). The hash covers the
redacted text and `redacted: true` marks the row. A body over `routines_max_bytes` is cut (default 512 KB, hard ceiling 4 MB), marked
`truncated: true`, and the cut text is still hashed and stored.

Redaction is pattern-based: it does not catch a secret written in an unusual form. Keep credentials out of routine bodies, and add anything
organisation-specific to the deny-list.

## Redshift: the TTL

`routines` is a heavy section for Redshift: a snapshot reuses the previous routines while they are younger than `routines_ttl_h`
(default 24), unless `dea snapshot --deep` is passed. Reused sections are marked on the page and skipped by `dea diff`.

## In the graph

`dea scan` reads the latest snapshot's routines, parses each stored body (cached by `body_hash` in `catalog/routines.json`, so an unchanged
routine is never re-parsed) and adds nodes `proc:<source>:<schema.name>`, `function:...`, `view:...`, `trigger:...` to the table graph
(`via: routine`) plus `edges.calls` for DAG to routine, routine to routine and trigger to function.

- Unqualified table names in a body are read as living in the routine's own schema, and those edges are flagged heuristic.
- A `CALL x.y` in a DAG resolves in this order: (1) the scan's `connections:` mapping (Airflow `conn_id` to de-agent source), (2) a unique
  live match, (3) a unique repo-scanned match, (4) unresolved, listing every candidate.

```yaml
scans:
  - name: pipelines
    type: airflow
    path: ../pipelines
    connections: {warehouse_conn: warehouse}   # Airflow conn_id -> de-agent source
```

- **Drift:** when a procedure or function exists both in a repo `.sql` scan and live, the normalized bodies are compared; a mismatch is
  reported by `dea scan` and listed in `catalog/routines.md` ("routine analytics.refresh_orders differs from sql/procs/refresh_orders.sql:12"),
  a cheap way to notice out-of-band edits without a migration tool. Views are not compared (engines rewrite view text).
- `dea impact shop.orders` lists the routines reading the table, the DAGs that call each on the way, and what they write onward.
  `dea impact proc:analytics.refresh_orders` (or `function:`, `view:`, `trigger:`) shows what a routine reads and writes, who calls it
  (transitively) and everything downstream.

## Pages, diff, find

`platform/<source>.md` gets a Routines summary (counts per schema and kind, parse coverage, truncated and redacted counts, routines changed
since the previous snapshot). Each routine has a page `platform/<source>/routines/<schema>.<name>.md` with its header, reads, writes, calls,
callers (after a `dea scan`) and the redacted body; names are sanitized for file names. `dea find` indexes the pages like any other.
`dea diff` lists added, removed and changed routines and prints a unified diff of a changed body (80 lines at most, then `... N more
lines`). A renamed routine shows as removed plus added. `dea diff` compares the last two snapshots, not the live warehouse: for "what
changed in this procedure since the last snapshot?", run `dea snapshot` first, then `dea diff --source <name>`.

A view whose base table is exclusively locked cannot be deparsed without waiting for the lock: the Postgres collector then lists views
without their text (`meta.routine_view_bodies_skipped`) instead of hanging. A body missing on either side is unknown, not a change:
`dea diff` does not report it.
