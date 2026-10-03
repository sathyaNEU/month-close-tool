# ADR-0004: job entrypoints and thin Databricks notebooks (spike S4, local half)

Status: accepted. Date: 2026-09-26.

## Context

The user asked for the matching and journaling logic to live in Databricks notebooks or SQL files, and the plan (D3) keeps the logic in a tested Python package with a thin notebook per job, so the same code has to start the same way in four places, which are a Databricks wheel task, a Databricks notebook, a local Kubernetes Job using `spark-submit`, and a test calling `python -m`.

## Decision

Every job is a `main(argv)` function in `close_engine.jobs.<kind>`, dispatched by `close_engine.jobs.__main__` (the `close-job` console script on Databricks). The `spark-submit` launcher is `engine/scripts/run_job.py`, which only forwards to the dispatcher, and each job prints exactly one result line starting with `CLOSE_JOB_RESULT ` followed by JSON, so the orchestrator can read the outcome without parsing logs. The notebooks in `engine/notebooks/` are Databricks source-format files that read widgets and call `main`, and nothing else.

Locally, the tests run every notebook as a plain script with a small `dbutils` widget stub (`engine/tests/testsupport/widget_stub.py`, test code only), and a test fails if a new notebook is added without widget values, so the notebooks are proven to work long before a Databricks workspace exists.

## Consequences

The S4 test starts the smoke job twice in separate JVMs, once through `spark-submit` with `--master local[2]` and once through `python -m` with `local[4]`, and both runs write 2,000 rows with the same content digest. Each result also reports its effective `spark.master`, and the test asserts that each launcher's own setting was used. That check caught a real problem while we built this stage, which was that `get_session` rebuilt the session with local options even under `spark-submit` and so quietly replaced the launcher's master (and would have ignored any jars a Kubernetes Job passes in). The fix is that a driver started by a JVM launcher (detected through `PYSPARK_GATEWAY_PORT`) now keeps the launcher's configuration and only gets the pinned settings applied. S4 does not show that the digest is independent of parallelism, because the smoke job fixes its own partition count, and that property is covered by the repartition tests in `engine/tests/spark` and later by the Gate 3 determinism checks.

We also test the wheels we will actually ship. `make wheel-smoke` builds both wheels, installs them into a fresh Python 3.12 environment with only the pinned runtime libraries, runs the `close-job` console script from there, and checks that it produces the same digest as the development environment, which proves the entry point, `py.typed` and the packaged test vectors are all inside the wheels. The Databricks half of S4 (running the same wheel and notebook in a workspace) moves to Stage 10, when the workspaces and the Databricks CLI are in place.
