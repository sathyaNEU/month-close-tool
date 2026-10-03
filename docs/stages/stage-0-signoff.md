# Stage 0 sign-off

Stage 0 was signed off on 2026-09-27, when the user approved the re-specified Stage 1 plan (the approval explicitly covered Stage 0). The gate result is in [stage-0-report.md](stage-0-report.md).

The sign-off accepted these changes to the approved plan: the engine never binds SQL parameters and the read path uses typed query specs ([ADR-0002](../adr/0002-spark-delta-parity-allowlist.md), [ADR-0005](../adr/0005-read-path-typed-query-specs.md)), SeaweedFS replaces MinIO ([ADR-0003](../adr/0003-local-object-store-seaweedfs.md)), the Databricks CLI waits until Stage 10 (it needs a `brew trust` for the databricks tap, with the user's go-ahead at that time), and the pyarrow waiver PYSEC-2026-113 is owned by the platform engineering lead (the default, since no other owner was named) and expires on 2026-12-31.

After the gate the user also moved the platform to Databricks (an App with Lakebase and Lakeflow jobs) and dropped Kubernetes and Temporal ([ADR-0006](../adr/0006-databricks-native-platform.md)), so Stage 1 was re-specified in two gated parts, 1a and 1b ([stage-1-spec.md](stage-1-spec.md)).
