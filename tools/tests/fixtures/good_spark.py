# Clean code for tools/lint_spark_bans.py: nothing here may be reported.
from pyspark.sql import Window
from pyspark.sql import functions as F


def good(df):
    w = Window.partitionBy("k").orderBy(F.col("amount_minor").desc(), F.col("item_id"))
    ranked = df.withColumn("rn", F.row_number().over(w))
    agg = df.groupBy("k").agg(F.array_sort(F.collect_list("item_id")).alias("ids"))
    halved = df.withColumn("half", F.col("amount_minor").cast("decimal(18,0)") * F.lit(5).cast("decimal(2,1)"))
    ratio = df.withColumn("pct_bps", (F.col("a") * 10000).cast("bigint"))
    scaled = 10 // 3  # floor division is fine
    allowed = df.withColumn("share", F.col("a") / F.col("b"))  # spark-ban: allow SB008 metrics only, never stored
    return ranked, agg, halved, ratio, scaled, allowed
