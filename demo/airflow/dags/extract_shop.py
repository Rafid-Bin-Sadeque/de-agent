"""extract_shop (hourly): incremental copy of each source table into warehouse.raw.<table> by updated_at.

Progress lives in raw._watermarks. Each table task is an outlet for the Dataset warehouse://raw/<table>.
Column lists are explicit on purpose: a renamed source column fails the task loudly (fault 01).
"""
import tempfile
from datetime import datetime, timedelta

from airflow import DAG
from airflow.datasets import Dataset
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook

TABLES = {
    "customers": "id, email, full_name, country, created_at, updated_at",
    "products": "id, sku, name, category, unit_price, created_at, updated_at",
    "orders": "id, customer_id, status, total_amount, created_at, updated_at",
    "order_items": "id, order_id, product_id, quantity, unit_price, created_at, updated_at",
    "events": "id, customer_id, event_type, payload, created_at, updated_at",
}


def extract(table: str) -> None:
    cols = TABLES[table]
    src = PostgresHook(postgres_conn_id="source_db").get_conn()
    wh = PostgresHook(postgres_conn_id="warehouse").get_conn()
    try:
        wcur = wh.cursor()
        wcur.execute("select high_watermark from raw._watermarks where table_name = %s", (table,))
        wm = wcur.fetchone()[0]
        with tempfile.TemporaryFile() as buf:
            scur = src.cursor()
            copy_sql = scur.mogrify(
                f"COPY (select {cols} from public.{table} where updated_at >= %s order by updated_at) TO STDOUT",
                (wm,)).decode()
            scur.copy_expert(copy_sql, buf)
            src.rollback()
            buf.seek(0)
            wcur.execute(f"create temp table _stage (like raw.{table}) on commit drop")
            wcur.copy_expert("COPY _stage FROM STDIN", buf)
        wcur.execute("select count(*), max(updated_at) from _stage")
        n, new_wm = wcur.fetchone()
        if n:
            wcur.execute(f"delete from raw.{table} where id in (select id from _stage)")
            wcur.execute(f"insert into raw.{table} select * from _stage")
            wcur.execute("update raw._watermarks set high_watermark = %s, rows_loaded = rows_loaded + %s, "
                         "updated_at = now() where table_name = %s", (new_wm, n, table))
        if table == "orders":
            # late-arrival check (orders only): recount landed rows per hour for the last 3 days. Each probe is a range
            # scan on raw.orders(updated_at); without that index every probe is a sequential scan.
            wcur.execute("select max(updated_at) from raw.orders")
            top = wcur.fetchone()[0]
            seen = 0
            for h in range(72):
                wcur.execute("select count(*) from raw.orders where updated_at > %s - make_interval(hours => %s + 1) "
                             "and updated_at <= %s - make_interval(hours => %s)", (top, h, top, h))
                seen += wcur.fetchone()[0]
            wcur.execute("update raw._watermarks set last_check_rows = %s where table_name = 'orders'", (seen,))
        wh.commit()
        print(f"{table}: {n} rows since {wm}")
    finally:
        src.close()
        wh.close()


with DAG(
    dag_id="extract_shop",
    schedule="@hourly",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args={"owner": "data-eng", "retries": 0, "execution_timeout": timedelta(minutes=30)},
    tags=["acme", "extract"],
    doc_md="Incremental copy source.shop -> warehouse.raw by updated_at.",
) as dag:
    for t in TABLES:
        PythonOperator(task_id=f"extract_{t}", python_callable=extract, op_args=[t],
                       outlets=[Dataset(f"warehouse://raw/{t}")])
