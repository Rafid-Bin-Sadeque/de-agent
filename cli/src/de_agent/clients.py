"""The ONE list of database clients the guards know about. Stdlib only, Python 3.9 compatible (hooks import it).

`extract.py` (sql_guard, query_log) reads SQL out of these clients' command lines; `secret_guard.py` uses the same set to
spot inline passwords. Add a client here and both layers pick it up.
"""
from __future__ import annotations

# executable basename -> sqlglot dialect used to classify the SQL it runs
SQL_CLIENTS = {
    "psql": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
    "snowsql": "snowflake",
    "sqlcmd": "tsql",
    "clickhouse-client": "clickhouse",
    "clickhouse-local": "clickhouse",
    "sqlite3": "sqlite",
    "duckdb": "duckdb",
    "trino": "trino",
    "trino-cli": "trino",
    "presto": "presto",
    "presto-cli": "presto",
    "spark-sql": "spark",
    "beeline": "hive",
}

# clients with their own non-generic argument handling in extract.py (dialects come from the source name)
CLOUD_CLIENTS = {"bq": "bigquery", "aws": "postgres"}

# database tools whose SQL we do not read, but whose command lines can carry credentials
OTHER_DB_CLIENTS = frozenset({
    "pg_dump", "pg_dumpall", "pg_restore", "mysqldump", "mysqladmin", "redshift-data", "pgcli", "mycli",
})

DB_CLIENTS = frozenset(SQL_CLIENTS) | {"bq"} | OTHER_DB_CLIENTS

# Python database drivers (import name prefixes): code that imports one is scanned for SQL (pyscan.py)
PY_DRIVERS = (
    "psycopg", "psycopg2", "psycopg_pool", "sqlalchemy", "pymysql", "MySQLdb", "mysql", "mysql.connector",
    "snowflake", "redshift_connector", "pyodbc", "pymssql", "duckdb", "sqlite3", "clickhouse_driver",
    "clickhouse_connect", "asyncpg", "pg8000", "oracledb", "cx_Oracle", "pyhive", "trino", "databricks",
)
PY_DRIVER_DIALECT = {
    "psycopg": "postgres", "psycopg2": "postgres", "psycopg_pool": "postgres", "asyncpg": "postgres",
    "pg8000": "postgres", "sqlalchemy": "postgres", "pymysql": "mysql", "MySQLdb": "mysql", "mysql": "mysql",
    "snowflake": "snowflake", "redshift_connector": "redshift", "pyodbc": "tsql", "pymssql": "tsql",
    "duckdb": "duckdb", "sqlite3": "sqlite", "clickhouse_driver": "clickhouse", "clickhouse_connect": "clickhouse",
    "pyhive": "hive", "trino": "trino", "oracledb": "oracle", "cx_Oracle": "oracle", "databricks": "spark",
}

_SOURCE_DIALECTS = {"redshift-data": "redshift", "bq": "bigquery", "athena": "athena"}
_SOURCE_DIALECTS.update(SQL_CLIENTS)
_SUFFIXES = ("-file", "-heredoc", "-herestring", "-stdin")


def dialect_for(source: str) -> str:
    """sqlglot dialect for an extract_sql source tag (`mysql`, `sqlite3-file`, `python:duckdb`, `redshift-data`, ...)."""
    if source.startswith("python:"):
        return source[len("python:"):] or "postgres"
    for suf in _SUFFIXES:
        if source.endswith(suf):
            source = source[: -len(suf)]
            break
    return _SOURCE_DIALECTS.get(source, "postgres")
