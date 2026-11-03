# Read-only access for each source

The primary safety control is that the login de-agent uses **cannot write** ([SAFETY.md](SAFETY.md), layer 0). Every hook sits on top of that.
This page gives a copy-paste setup per system. A human with admin rights runs it once, in their own terminal (Claude cannot: creating a
login is a write, and `sql_guard` denies it).

After it runs:

1. Store the password outside the hub: `~/.pgpass` (mode 0600), an environment variable, or a file with mode 0600. Never in `de-agent.yaml`.
2. Reference it in the source block: `password: "env:ORDERS_DB_PASSWORD"`, `password: pgpass` (the Postgres and Redshift default) or
   `password: "file:/abs/path"`. A literal is a load error. A source run over `exec: ssh:<host>` can only use `pgpass` on that host.
3. If you used an environment variable, restart Claude Code so it sees it.
4. `dea doctor`, then `dea snapshot` (or `dea pack test <source>` for a pack source).

Names below (`dea_ro`, `orders`, `public`, `monitor_wh`) are examples. Replace them with yours. Grant only the schemas you want the
agent to read; the monitoring views work without any table grants.

## PostgreSQL 12-17 (`type: postgres` and the `postgres-lite` pack)

```sql
CREATE ROLE dea_ro LOGIN PASSWORD '<set it here, then store it in ~/.pgpass>';
ALTER ROLE dea_ro SET default_transaction_read_only = on;   -- the server refuses writes from this login
ALTER ROLE dea_ro SET statement_timeout = '60s';
GRANT pg_monitor TO dea_ro;                                 -- every session, lock and pg_stat_* row, including other users' query text
GRANT CONNECT ON DATABASE orders TO dea_ro;
-- only if you want `dea query` to read table data:
GRANT USAGE ON SCHEMA public TO dea_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dea_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO dea_ro;   -- tables created later (run as the role that creates them)
```

- `pg_monitor` exists from PostgreSQL 10. Without it, `pg_stat_activity` hides other users' queries and the locks section is incomplete.
- The top-queries section reads `pg_stat_statements` when the extension is installed; `pg_monitor` covers it. Without the extension the
  section says so and the rest of the page still renders.
- Point it at a read replica when you have one: the collector's queries are light, but a replica keeps them off the primary entirely.

## Amazon Redshift (`type: redshift`)

```sql
CREATE USER dea_ro PASSWORD '<…>';
ALTER USER dea_ro SYSLOG ACCESS UNRESTRICTED;     -- see every user's rows in SYS_* / STL_* (workload by user, top queries)
GRANT USAGE ON SCHEMA public TO dea_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dea_ro;   -- table health in SVV_TABLE_INFO only lists tables the user can select
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO dea_ro;
```

- Without `SYSLOG ACCESS UNRESTRICTED` the user sees only its own queries, so the workload page shows de-agent and nothing else.
  It also exposes other users' query text, which can contain literal values: decide that with whoever owns the cluster.
- With the Data API transport, give the IAM principal `redshift-data:ExecuteStatement`, `BatchExecuteStatement`, `DescribeStatement` and
  `GetStatementResult`, and connect as `dea_ro` (`db_user`). The Data API cannot open a read-only transaction, so the grants above are what
  keep it read-only.

## MySQL 8 / MariaDB (the `mysql` pack)

```sql
CREATE USER 'dea_ro'@'%' IDENTIFIED BY '<…>';
GRANT PROCESS, REPLICATION CLIENT ON *.* TO 'dea_ro'@'%';    -- processlist, replica status
GRANT SELECT ON performance_schema.* TO 'dea_ro'@'%';
GRANT SELECT, EXECUTE ON sys.* TO 'dea_ro'@'%';              -- sys.innodb_lock_waits
GRANT SELECT, SHOW VIEW ON orders.* TO 'dea_ro'@'%';         -- only if you want `dea query` on data
```

- MySQL has no per-user read-only switch; the pack runs every section in `START TRANSACTION READ ONLY`, and the grants above contain no write.
- A replica with `super_read_only = ON` is the safest target.

## Snowflake (the `snowflake` pack)

```sql
USE ROLE ACCOUNTADMIN;                                    -- IMPORTED PRIVILEGES on SNOWFLAKE needs it
CREATE ROLE dea_ro;
GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE dea_ro;   -- ACCOUNT_USAGE views
GRANT USAGE ON WAREHOUSE monitor_wh TO ROLE dea_ro;       -- an X-Small warehouse is plenty
GRANT MONITOR ON WAREHOUSE etl_wh TO ROLE dea_ro;         -- each warehouse you want watched
CREATE USER dea_ro_user DEFAULT_ROLE = dea_ro DEFAULT_WAREHOUSE = monitor_wh RSA_PUBLIC_KEY = '<public key>';
GRANT ROLE dea_ro TO USER dea_ro_user;
```

- Prefer key-pair authentication; keep the private key file at mode 0600 and reference it with `file:`.
- Snowflake has no read-only transaction mode, so the role is the whole guarantee: give it no `OWNERSHIP`, `INSERT`, `OPERATE` or `CREATE`.
- ACCOUNT_USAGE views lag by up to a few hours; the page says what it reads.

## BigQuery (the `bigquery` pack)

Give a service account (or your user, for a trial) these roles on the project:

- `roles/bigquery.jobUser`: run the monitoring queries.
- `roles/bigquery.resourceViewer`: read `INFORMATION_SCHEMA.JOBS` for every user's jobs.
- `roles/bigquery.metadataViewer`: table sizes and staleness, with no access to table data.

The connection sets `maximum_bytes_billed` (option `max_bytes_billed`, default 10 GB), so a bad query fails instead of costing money.

## SQL Server (the `mssql` pack)

```sql
CREATE LOGIN dea_ro WITH PASSWORD = '<…>';
GRANT VIEW SERVER STATE TO dea_ro;                        -- blocking chains, running requests
USE orders;  CREATE USER dea_ro FOR LOGIN dea_ro;  ALTER ROLE db_datareader ADD MEMBER dea_ro;
USE msdb;    CREATE USER dea_ro FOR LOGIN dea_ro;  ALTER ROLE SQLAgentReaderRole ADD MEMBER dea_ro;
```

The pack connects with `ApplicationIntent=ReadOnly`, so an Availability Group routes it to a readable secondary when one exists.

## Databricks (the `databricks` and `databricks-system` packs)

- **CLI pack:** a service principal or token with `CAN_VIEW` on the jobs, clusters and SQL warehouses you list. The pack itself refuses
  every non-read CLI verb, `databricks api`, and secrets or token commands.
- **System tables pack:**

```sql
GRANT USE CATALOG ON CATALOG system TO `dea_ro`;
GRANT USE SCHEMA, SELECT ON SCHEMA system.lakeflow TO `dea_ro`;
GRANT USE SCHEMA, SELECT ON SCHEMA system.billing  TO `dea_ro`;
GRANT USE SCHEMA, SELECT ON SCHEMA system.query    TO `dea_ro`;
```

## Everything else

| Source | Read-only access |
|---|---|
| AWS (`type: aws`) | The IAM policy in [aws-iam-readonly.json](aws-iam-readonly.json): `Describe*`, `List*`, `Get*` on the services the collector reads. Scope S3 to the prefixes you name. |
| Airflow (`type: airflow`) | A REST API user with the built-in **Viewer** role. It can read DAGs, runs and task logs and cannot trigger, pause or clear anything. |
| dbt (`type: dbt`) | Nothing: it reads `manifest.json`, `run_results.json` and `sources.json` from disk. |
| GCP (`gcp` pack) | `roles/composer.environmentAndStorageObjectViewer`, `roles/dataflow.viewer` and `roles/pubsub.viewer` on the project, plus `roles/storage.objectViewer` on the buckets you name. `roles/viewer` also works but is broader. |
| Kubernetes (`kubernetes` pack) | A ServiceAccount bound to the built-in `view` ClusterRole in each namespace you list (`view` excludes Secrets). |
| Dagster, Fivetran (http packs) | A read-only API key where the product offers one; the packs send GET or read-only GraphQL only. |

## Writable sources (for approved plans)

A plan that changes data needs a **separate** source with a login that can write, marked `writable: true`. Snapshots and `dea query` never
use it; only `dea apply`, running a plan a human signed, does. Keep it as narrow as the change (for example `INSERT, UPDATE, DELETE` on one
schema) and remove it when the work is done. The agent cannot add `writable: true` itself: `self_guard` denies that edit.
