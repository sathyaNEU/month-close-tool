# ADR-0010: the engine's Unity Catalog code paths against Unity Catalog OSS 0.6.0

Status: accepted with Stage 1b. Date: 2026-09-27. Spike S1. The result is positive for Unity Catalog OSS, and Databricks Unity Catalog is still confirmed in Stage 10.

## Context

Locally the engine addresses Delta tables by path (`PathTableResolver`), and on Databricks it will use three-level Unity Catalog names (`CatalogTableResolver`). Stage 0 left the catalog branches untested: `DeltaTable.forName`, `saveAsTable`, and reads with time travel through `.table()`. Stage 1b added two more that depend on the resolver, the read path's `DESCRIBE HISTORY` and its DataFrame backend. The approved spike was time-boxed to two days, with "inconclusive, defer to Stage 10" allowed, because Unity Catalog OSS is not the same product as Databricks Unity Catalog.

## Decision

The spike (`make uc-spike`, `tools/run_uc_spike.py`) starts Unity Catalog OSS 0.6.0 (the pinned `unitycatalog/unitycatalog:v0.6.0` image, in its own Compose project) and runs `engine/tests/uc/uc_spike.py` in its own JVM with the `io.unitycatalog:unitycatalog-spark_4.1_2.13:0.6.0` connector and the catalog registered as `unity`. The first run failed at the first write, because the server had no managed storage location and so refused to create a managed table. The error named the server property to set (`storage-root.tables`). An external table would not work as a fallback either, because the connector only allows `REPLACE TABLE` on catalog-managed tables. With `storage-root.tables` pointing at a host directory mounted into the server at the same absolute path (the Spark client writes the files and the server records them), all eight operations work.

| Operation | Result |
|---|---|
| `CREATE SCHEMA unity.ops` | works |
| engine `save()`, a managed `saveAsTable` | works |
| a second commit (append) | works |
| `DeltaTable.forName(...).history()` | works, 2 versions |
| engine `current_version()` | works, version 1 |
| engine `read_table(..., version=0)`, time travel through `.table()` | works, the version-0 rows |
| read path `latest_version()`, `DESCRIBE HISTORY` on a three-level name | works, version 1 |
| read path DataFrame backend at version 0, scoped to one entity | works, that entity's rows only |

## Consequences

The catalog branches of the engine and the read path are exercised and work against Unity Catalog OSS, so nothing in the design has to change for catalog names. The spike also shows two operational facts for Stage 10: managed tables need a managed storage location on the catalog, schema or server, and a client can only replace catalog-managed tables. On Databricks both come from the workspace's Unity Catalog setup (managed storage on the catalog, with the platform's service principal owning the tables). Stage 10 repeats the same eight operations against a real workspace before the engine's production resolver is switched to catalog names. The spike runs locally on demand and in the Stage 1b gate. It is not part of the default stack, and its Compose project publishes the server on 127.0.0.1 only while the spike runs.
