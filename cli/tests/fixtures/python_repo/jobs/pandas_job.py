"""pandas export job (fictional)."""
import pandas as pd
from sqlalchemy import create_engine, text

engine = create_engine("postgresql://localhost/warehouse")


def export_daily():
    df = pd.read_sql_table("daily_orders", engine, schema="analytics")
    df = df.merge(pd.read_sql("select id, plan from raw.customers", engine), how="left")
    df.to_sql("daily_orders_export", engine, schema="exports", if_exists="replace")


def archive(conn):
    conn.execute(text("insert into archive.orders select * from raw.orders where created_at < now() - interval '1 year'"))
