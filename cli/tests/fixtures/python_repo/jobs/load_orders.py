"""Nightly order load (fictional Acme Shop): plain psycopg, one function per step."""
import psycopg
from lib import etl

STAGES = {"orders": "staging.orders_stage", "customers": "staging.customers_stage"}


def load_orders(conn):
    with conn.cursor() as cur:
        cur.execute("insert into raw.orders select * from staging.orders_stage")
        cur.execute("delete from staging.orders_stage")


def refresh_marts(conn):
    cur = conn.cursor()
    sql = """
        insert into analytics.daily_orders
        select order_date, count(*) from raw.orders group by 1
    """
    cur.execute(sql)


def stage_all(conn):
    cur = conn.cursor()
    for name, table in STAGES.items():
        cur.execute(f"truncate table {table}")
        cur.execute(f"insert into {table} select * from landing.{name}")


def broken_dynamic(conn, q):
    conn.cursor().execute(q)


def main():
    conn = psycopg.connect("dbname=warehouse")
    load_orders(conn)
    refresh_marts(conn)
    etl.run_step("etl.sp_rollup")
    etl.nightly()


if __name__ == "__main__":
    main()
