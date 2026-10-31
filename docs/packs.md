# Packs: monitor any system with a YAML file

A **pack** is a small YAML file that says what to read from a system that has SQL, a command-line client or an HTTP API. A generic
collector runs it read-only; the result goes through the same render, diff, snapshot, `dea find` and session-hook path as the hand-written
collectors (Postgres, Redshift, Airflow, dbt, AWS), so a pack-based source gets a `platform/<source>.md` page with a signals list first,
`dea diff` between snapshots, and so on.

Try a pack before writing a collector: [writing-a-collector.md](writing-a-collector.md) is for systems that need real code.

```bash
dea pack list                         # built-in and hub packs
dea pack new reviews --kind sql       # scaffold packs/reviews.yaml in the hub, with comments
dea pack check reviews                # schema, expressions, every SQL / argv / request checked as a read
dea pack test reviews-db              # run a configured source once, read-only: page + signals, no snapshot written
```

Or tell Claude "we also use X" and the `add-source` skill does the steps for you ([skills.md](skills.md)).

## Using a pack: the source block

A source whose `type` is `sql`, `cli` or `http` takes `pack: <name>`. The pack's `params` go in `options:`. Secrets are references only
(`env:NAME`, `file:/abs/path` mode 0600, `none`); a literal, or a password inside a URL, is a load error.

```yaml
sources:
  # sql: any SQLAlchemy 2.x URL WITHOUT a password; the driver comes from the pack's extra (pip install 'de-agent[mysql]')
  - name: reviews-db
    type: sql
    pack: mysql
    conn: {url: "mysql+pymysql://monitor@reviews-db.internal:3306/reviews", password: "env:REVIEWS_DB_PASSWORD"}

  # cli: runs the pack's argv with shell=False, locally or over ssh
  - name: k8s
    type: cli
    pack: kubernetes
    exec: local                      # or ssh:<host-alias>
    options: {namespaces: [shop, data]}

  # http: GET (and read-only GraphQL queries); auth none | bearer | basic | header
  - name: fivetran
    type: http
    pack: fivetran
    base_url: https://api.fivetran.com
    auth: basic
    user: my-api-key                 # not a secret
    password: "env:FIVETRAN_API_SECRET"
    options: {group_ids: [grp_example], stale_hours: 24}
```

| kind | where it runs | notes |
|---|---|---|
| `sql` | local only | needs `sqlalchemy>=2` and a driver: extras `pg`, `mysql`, `snowflake`, `bigquery`, `databricks`, `mssql` (`pip install 'de-agent[mysql]'`). A missing driver says exactly which extra to install. The URL scheme must match the pack's dialect. |
| `cli` | local, or `exec: ssh:<host>` | the CLI must be on PATH (locally or on the host). Over ssh each argument is `shlex.quote`d and sent after `ssh <host> --`. |
| `http` | local, or `exec: ssh:<host>` | over ssh a stdlib script runs on the host and resolves the secret THERE (`env:`/`file:` refs only); the local side never sees it. |

`dea doctor` covers pack sources: driver or CLI present, credentials resolve and the source answers, and `pack check` passes for every hub pack.

## The format

```yaml
pack: snowflake              # [a-z0-9][a-z0-9_-]*; hub-local packs/<name>.yaml overrides a built-in of the same name
version: 1
kind: sql                    # sql | cli | http
dialect: snowflake           # sql only: postgres | redshift | mysql | snowflake | bigquery | tsql | databricks
requires: {extra: snowflake} # or {cli: databricks}
validated: fixtures          # fixtures | live (shown by `dea pack list` and on the rendered page)
description: One sentence.
params:                      # optional: values the source supplies under options:
  warehouses: {type: list, required: true, description: "warehouse names"}
  stale_hours: {type: int, default: 24}
sections:
  - name: failed_queries_24h
    sql: |                   # sql kind: exactly one statement that classifies as a read in `dialect`
      select query_id, user_name, error_message, start_time
      from snowflake.account_usage.query_history
      where execution_status = 'FAIL' and warehouse_name in :warehouses
        and start_time > dateadd(hour, -24, current_timestamp())
      order by start_time desc limit 50
    key: query_id            # identity of a row, so `dea diff` compares rows, not positions (string or list)
    columns: [query_id, user_name, error_message, start_time]   # optional projection and order
    volatile: [start_time]   # fields `dea diff` ignores (ages, timestamps, free text)
    limit: 200               # max rows kept (default 200, max 1000); the true count still feeds `rows:` rules
    signals:
      - rows: "count > 0"    # aggregate rule over the section
        message: "{count} failed queries in 24h"
        severity: warn       # info | warn | error  (info and error are prefixed on the page: "[info] ...", "[error] ...")
      - when: "error_message contains 'quota'"   # per-row rule
        message: "query {query_id}: quota error"
    render: table            # table | list
    title: Failed queries (24 h)   # optional page heading
```

Section keys by kind:

- **sql:** `sql`. Bind parameters are written `:name` (a list is `in :name`); `{{name}}` is allowed only for a param of `type: ident`
  (a validated identifier such as a region or dataset name, for views like ``region-{{region}}`.INFORMATION_SCHEMA.JOBS``).
- **cli:** `run: [argv...]` (a YAML list, never a shell string), `select: <JMESPath>` over the JSON the command prints, `fields: {out_name: <JMESPath>}`
  applied to each selected element, plus `key`, `columns`, `signals`, `render`. The command must print JSON (add the CLI's JSON flag).
- **http:** `get: /path?query` relative to the source's `base_url`, or `graphql: "query ..."` (optionally `path: /graphql`), plus `select` / `fields`.
  GraphQL documents must start with `query` or `{` and may not contain `mutation` or `subscription` anywhere.

Params and templating:

- `params:` entries: `type` is `str`, `ident`, `int`, `bool` or `list`; `default`, `required`, `description`. Unknown `options:` keys are an error. Values are
  validated (letters, digits and `_ . : / @ = + , -`, no leading `-`), so a value can never become a flag or a second command.
- `{{name}}` in a `run:` element, an http path or a GraphQL text is replaced by the param value; `{{item}}` by the current `for_each` value.
- `for_each: <list param>` runs the section once per value and concatenates the rows; `item_as: namespace` adds the value to each row under that name.
  Use it to scope a pack to named resources instead of listing a whole account.
- Built-in time windows: `{{since_<N><h|d>_<epoch|epoch_ms|iso>}}`, for example `{{since_24h_epoch}}` (cli and http only; in SQL write the window in SQL).
- In rules, a param is available as `p_<name>` (`age_h(succeeded_at) > p_stale_hours`).
- Messages use `{field}` for row fields (`{count}` in a `rows:` rule); an unknown field renders as `-`.

## Safe expressions

`when:` and `rows:` use a tiny evaluator built on Python `ast` with a whitelist. Nothing is compiled or `eval`ed: the tree is walked by an interpreter
that knows only:

- comparisons (`== != < <= > >=`), `and` / `or` / `not`, number arithmetic (`+ - * / // % **`), `in` / `not in`;
- the word operators `contains`, `startswith`, `endswith` (`error_message contains 'quota'`, `lower(name) startswith 'tmp_'`), which are
  rewritten to method-style checks;
- names: row fields (or `count` for `rows:`, and `p_<param>`), literals (`'text'`, numbers, `True`, `False`, `None`), lists and tuples for `in`,
  and `field['odd name']` for fields that are not valid names;
- the functions `len(x)`, `lower(x)`, `age_h(ts)`, `age_d(ts)` and `now()`. `ts` is an ISO string, a datetime or epoch seconds/milliseconds.

No attribute access, no other calls, no subscripts beyond `field['x']`, no lambdas, comprehensions, f-strings, conditional expressions, walrus, `is`,
bit operators or comments. Anything else is a pack error, found by `dea pack check` and again at run time. Missing or null values never raise:
ordering a `None` is false, arithmetic with `None` is `None`. Limits: 400 characters, 120 nodes, depth 12, a step and a time budget per evaluation,
exponents capped (`2 ** 100000` is an error), and arithmetic only on numbers (no string repetition). The test suite throws `__import__`, attribute chains,
lambdas, comprehensions, huge exponents and deep nesting at it: all of them are errors, none runs.

## Read-only enforcement

The primary control is still a read-only role or credential for the agent's systems. On top of it, each kind enforces read-only in code, and a pack that
fails `dea pack check` never runs (the source reports the check failure instead). Every check below also runs again right before each execution,
so editing a pack cannot skip it.

| kind | before anything runs | while it runs |
|---|---|---|
| `sql` | every statement is classified with the SQL classifier in the pack's dialect: exactly one statement, and it must be a read (a write, a side-effect function such as `pg_terminate_backend`, `SELECT INTO`, `FOR UPDATE`, an unparseable statement: refused, no connection is made) | inside `BEGIN READ ONLY` (PostgreSQL, Redshift) or `START TRANSACTION READ ONLY` (MySQL, MariaDB) with a statement timeout (`statement_timeout`, `max_execution_time`, Snowflake `STATEMENT_TIMEOUT_IN_SECONDS`) and a lock timeout on PostgreSQL; SQL Server connections ask for a read-intent route (`ApplicationIntent=ReadOnly`) and a query timeout; BigQuery gets a `maximum_bytes_billed` cap; the connection pool is disabled, and on PostgreSQL, Redshift and MySQL every statement ends with `ROLLBACK`. Snowflake, BigQuery and Databricks have no read-only transaction: for them classification and a read-only role are the controls. |
| `cli` | the final argv (placeholders substituted) must be a recognised READ command: a bare executable name from a fixed table (kubectl, helm, gcloud, gsutil, bq, databricks, aws, az, terraform/tofu, airflow, dbt, snow) and a read sub-command for it (`kubectl get|describe|logs`, `aws describe-*|list-*|get-*` minus secret-returning operations, `gcloud ... list|describe`, `databricks ... list|get|list-runs`, `terraform plan|show|output`, ...). Unknown CLI, unknown verb, a path-qualified executable (`/tmp/kubectl`), a shell, `env`, `sudo`: refused, no process is started | `subprocess` with `shell=False` and a list argv, stdin closed, a timeout, output capped at 5 MB; over ssh every argument is `shlex.quote`d |
| `http` | only `get:` sections and GraphQL `query` documents exist; the path must be relative to `base_url` (no scheme, no `//`, no `..`); a GraphQL document containing `mutation` or `subscription` (outside strings and comments) is refused | the single function that opens a socket accepts GET, or POST of a validated GraphQL query, and raises for DELETE, PUT, PATCH and everything else before connecting; redirects are not followed (so a token never travels to another host); responses are capped at 5 MB |

What this does not do: a read-verb CLI that is itself unsafe (for example a `get` that returns secrets) is only as safe as the allow-list, which refuses the
well-known secret-returning verbs; give the agent's credentials least privilege anyway. A separate hook layer (the CLI write guard) covers what an agent types in a terminal rather than what a pack runs; see [SAFETY.md](SAFETY.md).

## Writing and testing a pack

1. `dea pack new mysystem --kind sql|cli|http` (writes `packs/mysystem.yaml` in the hub, commented, passing `dea pack check` as scaffolded).
2. Draft the sections from the system's **documented** views, CLI or API. Start with the one signal you care about; add `key` and `volatile` so `dea diff` is quiet.
3. `dea pack check mysystem` until it is clean. It validates the schema, every expression, classifies every SQL as a read in the dialect, runs the argv read-verb
   check, and checks http paths and GraphQL.
4. Add a source block (secret references only) and run `dea pack test <source>`: one read-only run, the rendered page and the signals, no snapshot written.
5. Iterate; when it is right, `dea snapshot`.

Tests without the real system: inject the raw results. `RunContext(pack_fn=fn)` replaces the final step of every section (`fn(section, item, payload) -> rows | stdout text | parsed body`),
so the checks, shaping, signals and render all still run. The built-in packs are tested this way, with fixtures in `cli/tests/packs_fixtures.py` and golden pages in `cli/tests/golden/pack_<name>.md`
(`UPDATE_GOLDEN=1 pytest cli/tests/test_packs_builtin.py` regenerates them; read the diff).

## Built-in packs

`validated: live` means the pack was run against a real system in the test suite (a throwaway container). `fixtures` means it was **not** run against a live system: its
queries, argv and response shapes were hand-built from the vendor's documentation with fictional values. Fixture packs may need small fixes on your system; say so when you hit one.

| pack | kind | sections | validated |
|---|---|---|---|
| `postgres-lite` | sql | long-running sessions, blocked sessions, dead-tuple %, estimated table sizes (no relation locks) | **live**: PostgreSQL 16 container |
| `mysql` | sql | processlist > 5 min, InnoDB lock waits, replica status (`SHOW REPLICA STATUS`), top tables | **live**: mysql:8.4 container |
| `snowflake` | sql | warehouses, failed and queued queries 24 h, credit use 24 h, task failures, pipe load failures, top storage tables | fixtures |
| `bigquery` | sql | job failures 24 h, top bytes billed, slot use per hour, largest tables, stale tables | fixtures |
| `databricks` | cli | failed and long job runs, cluster state, SQL warehouse state (`databricks ... -o json`) | fixtures |
| `databricks-system` | sql | `system.lakeflow` job failures, `system.billing.usage` 24 h, `system.query.history` failures | fixtures |
| `mssql` | sql | blocking chains, long requests, SQL Agent job failures (`msdb`), top tables | fixtures |
| `gcp` | cli | Composer environments, Dataflow failures 24 h, Pub/Sub subscriptions (list and config), newest object per bucket prefix | fixtures |
| `kubernetes` | cli | pods not Running or Completed, CrashLoopBackOff, failed Jobs, CronJobs that have not succeeded, in the namespaces you list | fixtures |
| `dagster` | http (GraphQL) | failed runs 24 h, stopped schedules and sensors | fixtures |
| `fivetran` | http | connectors with sync failures, paused, setup incomplete, or no successful sync for N hours | fixtures |

Permissions the packs assume are in each pack's `description` (`dea pack show <name>`). Scoping: sections that would list a whole account take named resources
(`warehouses`, `datasets`, `namespaces`, `job_ids`, `group_ids`, ...) as params.

## Contributing a pack

A pack is one YAML file in `cli/src/de_agent/packs/` plus its test data. A good contribution has:

1. the pack, passing `dea pack check` (CI runs it on every built-in);
2. fixtures in `cli/tests/packs_fixtures.py` built from the vendor's documented shapes with **fictional** values (no real names, hosts, ids, emails),
   exercising each signal, and a golden page (`UPDATE_GOLDEN=1`, then review the diff);
3. `validated: fixtures` unless you ran it against a real system in a test (a container, never a company system) and the test is in the suite;
4. a row in the table above, and the extra it needs declared in `cli/pyproject.toml`.

Never in a pack: a write statement or mutating verb, an inline credential, an account-wide listing when named resources would do.

## Not in packs

Pagination of large HTTP responses, JSON-lines output, cross-section joins and per-row secrets are out of scope; a pack that needs them is a collector
([writing-a-collector.md](writing-a-collector.md)). The hand-written collectors (Postgres, Redshift, Airflow, dbt, AWS) stay as the deep tier.
