"""Run rendered SQL on plain Spark 4.1 (no Delta extension, so named parameters bind), in its own JVM.

    python plain_spark_runner.py jobs.json results.json

``jobs.json`` is ``{"parquet": path, "jobs": [{"sql": ..., "params": {...}}]}``; the SQL refers to the
table as ``{table}``, which becomes ``parquet.`path```. Results are written as lists of row dicts, or
``{"error": ...}`` for a job that failed, in job order.
"""

from __future__ import annotations

import json
import sys

from pyspark.sql import SparkSession


def main(jobs_path: str, out_path: str) -> int:
    with open(jobs_path, encoding="utf-8") as fh:
        spec = json.load(fh)
    spark = (
        SparkSession.builder.master("local[2]")
        .appName("plain-spark-renderer-check")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.sql.ansi.enabled", "true")
        .config("spark.sql.shuffle.partitions", "2")
        .getOrCreate()
    )
    table = f"parquet.`{spec['parquet']}`"
    results: list[object] = []
    for job in spec["jobs"]:
        try:
            rows = spark.sql(job["sql"].replace("{table}", table), args=job["params"]).collect()
            results.append([r.asDict() for r in rows])
        except Exception as exc:  # recorded per job so one failure does not hide the others
            results.append({"error": f"{type(exc).__name__}: {exc}"[:2000]})
    spark.stop()
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
