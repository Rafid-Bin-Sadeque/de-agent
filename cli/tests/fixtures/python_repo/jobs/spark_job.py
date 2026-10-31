"""pyspark batch job (fictional)."""
from delta.tables import DeltaTable
from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()
RAW = "s3a://acme-lake/raw/orders/"


def build_silver():
    orders = spark.read.format("parquet").load(RAW)
    customers = spark.table("bronze.customers")
    joined = orders.join(customers, "customer_id")
    joined.write.mode("overwrite").saveAsTable("silver.orders_enriched")
    joined.write.format("delta").save("s3://acme-lake/silver/orders_enriched/")


def upsert(updates):
    target = DeltaTable.forName(spark, "gold.customer_ltv")
    target.alias("t").merge(updates.alias("u"), "t.id = u.id").whenMatchedUpdateAll().execute()


def sql_step():
    spark.sql("insert into gold.daily_revenue select order_date, sum(total) from silver.orders_enriched group by 1")
    spark.read.option("dbtable", "public.refunds").format("jdbc").load()
    spark.read.parquet(RAW + "late/")
