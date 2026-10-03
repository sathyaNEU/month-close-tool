# Databricks notebook source
# MAGIC %md
# MAGIC # ddl_apply (thin wrapper)
# MAGIC
# MAGIC Reads widget values and calls `close_engine.jobs.ddl_apply.main`, which applies the packaged `V###`
# MAGIC Delta migrations to the lake exactly once and refuses to run if an applied migration was edited.
# MAGIC All logic lives in the `close_engine` wheel. Locally, tests run this file as a plain script with a
# MAGIC small `dbutils` widget stub.

# COMMAND ----------

dbutils.widgets.text("run_id", "")
dbutils.widgets.text("attempt", "1")
dbutils.widgets.text("lease_resource", "ddl:lake")
dbutils.widgets.text("fencing_token", "1")
dbutils.widgets.text("requested_at", "2000-01-01T00:00:00+00:00")
dbutils.widgets.text("request_id", "")
dbutils.widgets.text("root_uri", "")

# COMMAND ----------

from close_engine.jobs import ddl_apply

argv = [
    "--run-id",
    dbutils.widgets.get("run_id"),
    "--attempt",
    dbutils.widgets.get("attempt"),
    "--lease-resource",
    dbutils.widgets.get("lease_resource"),
    "--fencing-token",
    dbutils.widgets.get("fencing_token"),
    "--requested-at",
    dbutils.widgets.get("requested_at"),
    "--root-uri",
    dbutils.widgets.get("root_uri"),
]
if dbutils.widgets.get("request_id"):
    argv += ["--request-id", dbutils.widgets.get("request_id")]
exit_code = ddl_apply.main(argv)
if exit_code != 0:
    raise RuntimeError(f"ddl_apply failed with exit code {exit_code}")
