# Seeded violations for tools/lint_spark_bans.py. Each line below must be reported.
from pyspark.sql import functions as F
from pyspark.sql.types import DoubleType


def bad(df, spark):
    a = df.withColumn("id", F.monotonically_increasing_id())  # SB001
    b = df.withColumn("r", F.rand())  # SB001
    c = df.withColumn("t", F.current_timestamp())  # SB001
    d = df.dropDuplicates(["k"])  # SB002
    e = df.groupBy("k").agg(F.first("v"))  # SB003
    f = df.groupBy("k").agg(F.max_by("v", "ts"))  # SB003
    g = df.groupBy("k").agg(F.collect_list("v"))  # SB004
    h = df.withColumn("f", F.input_file_name())  # SB005
    i = df.rdd.map(lambda r: r)  # SB006
    j = spark.sparkContext  # SB006
    k = df.cache()  # SB006
    m = df.withColumn("x", F.col("a").cast("double"))  # SB007
    n = DoubleType()  # SB007
    o = df.withColumn("q", F.col("a") / F.col("b"))  # SB008
    p = df.select(F.expr("a + b"))  # SB009
    q = df.selectExpr("a", "b")  # SB009
    r = spark.sql("SELECT :x", args={"x": 1})  # SB010 + SB011
    s = spark.sql("SELECT 1")  # SB011
    return a, b, c, d, e, f, g, h, i, j, k, m, n, o, p, q, r, s
