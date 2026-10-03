## What Stage 0 delivers

Stage 0 sets up the toolchain, the repository and the shared contracts, and proves that the local open-source stack behaves like Databricks Runtime 18 LTS for everything later stages depend on. The contracts library (hash IDs, canonical JSON, money and FX rounding, business calendars, table names and resolvers) ships with shared test vectors that the Spark implementations reproduce exactly, the engine has its session, digest, ID and money helpers plus the smoke job, and the gate runs every check below from one command.

## Findings that change the approved plan

Four things came out of Stage 0 that need your sign-off, because they differ from the plan you approved.

1. Open-source Delta 4.1 to 4.4 drops SQL parameter markers once its extension is loaded (upstream [delta-io/delta#7640](https://github.com/delta-io/delta/issues/7640), fix proposed in PR #7747). So the engine never binds SQL parameters, and the read path (D8) moves to typed query specs compiled to DataFrame operations, with the final design in Stage 1 ([ADR-0002](../adr/0002-spark-delta-parity-allowlist.md), [ADR-0005](../adr/0005-read-path-typed-query-specs.md)). The plan's gate item for `IDENTIFIER` with named parameters is on the deny list with evidence, and the allowlist still has 17 features.
2. MinIO community edition was archived on 2026-04-25 and is source-only now, so the local S3-compatible store is SeaweedFS 4.47 ([ADR-0003](../adr/0003-local-object-store-seaweedfs.md)).
3. The Databricks CLI is not installed yet, because Homebrew 7 needs an explicit `brew trust` for the databricks tap and I did not want to change your Homebrew trust settings without asking. It is not needed before Stage 10.
4. pip-audit flags PYSEC-2026-113 in pyarrow 21.0.0, which we pin to mirror DBR 18. The advisory says the Python bindings are not vulnerable, so it is recorded as a reviewed waiver that expires on 2026-12-31 (`tools/security-waivers.toml`).

## Fixed during the stage

Review of the S4 evidence found that the engine rebuilt its Spark session with local options even when `spark-submit` had already configured it, so the launcher's `--master` (and, in Stage 1, the Kubernetes Job's jars and settings) would have been quietly replaced. Drivers started by a JVM launcher now keep the launcher's configuration, the smoke job reports its effective master, and the S4 test asserts it. The gate also gained a check that the built wheels work in a clean environment, and all 12 pre-commit hooks were run over every file so the first commit will not be blocked. The gate then caught two more problems on its own re-run, which were ruff reformatting a Python block inside the roadmap (so `docs/` is now excluded from ruff, since those files are kept verbatim) and the S3 gateway answering later than the SeaweedFS health check after a restart (so the check now probes the S3 port too, and the final gate run cold-started the store on purpose to prove it).

## Carried into Stage 1

The Unity Catalog branches of `close_engine.io.tables` (`DeltaTable.forName`, `saveAsTable`, and time travel through `.table()`) are not exercised yet, because Stage 0 uses path-based tables only, so they go into the Stage 1 `+uc` catalog spike.

## Before Stage 1

After the gate you told us the platform has 7 users, so we moved it to Databricks (an App with Lakebase and Lakeflow jobs) and dropped Kubernetes and Temporal ([ADR-0006](../adr/0006-databricks-native-platform.md)). That also withdraws the earlier request to give Docker 12 GB, because local development now runs on Docker Compose, which needs roughly 8 to 10 GB, and Stage 1 will measure the real number before asking you to change anything. Stage 1 itself is re-specified and comes to you for approval before we build it.
