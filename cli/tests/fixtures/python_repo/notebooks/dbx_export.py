# Databricks notebook source
# MAGIC %md
# MAGIC # Revenue export (fictional)

# COMMAND ----------

# MAGIC %sql
# MAGIC create or replace table gold.revenue_daily as
# MAGIC select order_date, sum(total) as revenue from silver.orders_enriched group by order_date

# COMMAND ----------

df = spark.table("gold.revenue_daily")
df.write.mode("overwrite").format("delta").save("s3://acme-lake/export/revenue/")
