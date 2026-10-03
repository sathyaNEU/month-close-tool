# ADR-0003: SeaweedFS as the local S3-compatible store

Status: accepted. Date: 2026-09-26. Amends D2 in the roadmap.

## Context

The plan used MinIO as the local stand-in for S3, but when Stage 0 started we checked the project and found that the MinIO community repository was archived on 2026-04-25 and that the community edition is now distributed as source code only, with the README saying "We will no longer provide pre-compiled binary releases for the community version." The old images still exist but get no security updates, which would also fail the container scan we run from Stage 1.

## Decision

We use SeaweedFS (`chrislusf/seaweedfs:4.47`, released 2026-09-14) as the local S3-compatible store. It is actively maintained (releases every one to two weeks), publishes signed multi-architecture images that run natively on this arm64 Mac, and supports the S3 operations Delta needs through the Hadoop S3A connector. It runs as a single `weed server -s3` process in `compose.test.yaml` (and later in kind), with two identities generated at bootstrap into `.local/secrets/seaweedfs-s3.json`, an admin identity for the jobs and a read-only identity that the query service will use from Stage 1.

On the Spark side we use `org.apache.hadoop:hadoop-aws:3.4.2`, which must match the Hadoop version bundled in pyspark 4.1.3, with path-style access and `SimpleAWSCredentialsProvider`. That connector pulls in the AWS SDK bundle 2.29.52 (about 600 MB, fetched once into the local Ivy cache).

We did not choose to keep the frozen MinIO image, because it will never get security fixes, and we did not choose to build MinIO from source ourselves, because we would be maintaining a fork of an archived project.

## Consequences

The Stage 0 allowlist (ADR-0002) passed 17 out of 17 features, three runs in a row, on SeaweedFS, so the Delta behaviour we depend on is proven on this store. SeaweedFS is still an emulator of the S3 API and not AWS S3 itself, so the Stage 10 parity suite repeats the allowlist on real S3 and Unity Catalog, and presigned upload URLs (needed for journal attachments in Stage 7) are checked when that stage starts.
