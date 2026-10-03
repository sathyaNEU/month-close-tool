# ADR-0002: portable Spark and Delta feature allowlist (spike S2)

Status: accepted. Date: 2026-09-26. Amended 2026-09-27 (Stage 1a): feature 18, commit `userMetadata`.

## Context

The engine has to run unchanged on local open-source Spark and on Databricks Runtime 18 LTS, so Stage 0 pinned pyspark 4.1.3 with delta-spark 4.4.0 (the `io.delta:delta-spark_4.1_2.13:4.4.0` build) and proved every Delta feature we plan to rely on against the local S3-compatible store (SeaweedFS, see ADR-0003). The suite lives in `engine/tests/parity/test_delta_parity.py`, and the gate runs it three times in a row, so a feature goes on the allowlist only if it passes 3 out of 3 runs.

The plan also listed `IDENTIFIER(:t)` with named parameters as an allowlist item, and that item failed. With the Delta extension loaded, Spark does not bind SQL parameters at all, not even for `SELECT :x` with `args={"x": 1}`, which fails with `UNBOUND_SQL_PARAMETER`, and `IDENTIFIER(:t)` fails with `NOT_A_CONSTANT_STRING.NOT_CONSTANT`. The same code binds parameters correctly in plain Spark 4.1.3 without Delta, and the failure is identical for pyspark 4.1.0, 4.1.1, 4.1.2, 4.1.3 and 4.2.0 combined with Delta builds 4.1.0, 4.2.0, 4.3.1 and 4.4.0. The cause is that Spark 4.1 added `ParserInterface.parsePlanWithParameters`, and Delta's `DeltaSqlParser` inherits the default implementation, which drops the parameter context. This is tracked upstream as [delta-io/delta#7640](https://github.com/delta-io/delta/issues/7640) (opened 2026-09-10, still open), with a proposed fix in [PR #7747](https://github.com/delta-io/delta/pull/7747) (opened 2026-09-26).

A second finding came out of the same probes. Wrapping a path-based table name in a literal, as in ``IDENTIFIER('delta.`s3a://...`')``, works for CREATE TABLE and INSERT but fails for SELECT, MERGE, UPDATE and DELETE with `UNSUPPORTED_DATASOURCE_FOR_DIRECT_QUERY`. Plain SQL with the path written directly (``delta.`s3a://...` ``) works for every statement, and so do the DataFrame and DeltaTable APIs.

## Decision

The allowlist is the 18 features below, each proven 3 out of 3 on SeaweedFS in the form the engine uses it. Feature 18 was added in Stage 1a, because every commit now records the run, attempt and fencing token that wrote it (ADR-0007).

| # | Feature | Form used by the engine |
|---|---|---|
| 1 | MERGE with matched delete, matched update, not-matched insert and not-matched-by-source delete | `DeltaTable.merge` with Column conditions |
| 2 | Append-only tables that accept insert-only MERGE and reject UPDATE and DELETE | `delta.appendOnly` + `whenNotMatchedInsertAll` |
| 3 | Unexpected columns rejected when auto-merge is off | DataFrame append |
| 4 | Time travel and version pins | reader `versionAsOf`, `DeltaTable.history(1)` |
| 5 | Change data feed | reader `readChangeFeed` |
| 6 | CHECK and NOT NULL constraints, duplicate constraint rejected | `run_ddl` ALTER TABLE + DataFrame append |
| 7 | Deletion vectors | `DeltaTable.delete` + history metrics |
| 8 | Row tracking, row IDs stable across updates | `_metadata.row_id` |
| 9 | DDL gateway renders only resolver identifiers | `close_engine.io.sql.run_ddl` |
| 10 | `applyInPandas` under ANSI mode | grouped pandas UDF |
| 11 | Exact DECIMAL(18,0) x DECIMAL(19,12) = DECIMAL(38,12), round trip through Delta | DataFrame arithmetic |
| 12 | `bround` is half-even | DataFrame function |
| 13 | Liquid clustering | `run_ddl` CLUSTER BY + `optimize().executeCompaction()` |
| 14 | Partition-scoped MERGE leaves other partitions untouched | MERGE with a literal partition predicate |
| 15 | History exposes audit columns (operation, metrics, engine info) | `DeltaTable.history()` |
| 16 | CREATE TABLE IF NOT EXISTS is idempotent and table properties stick | `run_ddl` + `DeltaTable.detail()` |
| 17 | Session time zone is UTC | pinned session configuration |
| 18 | Commit `userMetadata` recorded in history (run, attempt, fencing token) | writer option `userMetadata` + `DeltaTable.history()` |
| 19 | `ADD COLUMNS` evolves an append-only table: nullable columns, old rows read null, insert-only MERGE fills them (added with Stage 7c, ADR-0018) | `run_ddl` ALTER TABLE ADD COLUMNS + `whenNotMatchedInsertAll` |

The deny list is SQL parameter binding in any form (named, positional and `IDENTIFIER(:t)`), `IDENTIFIER('literal')` for path-based tables, and identity columns (which disable concurrent transactions, so we use deterministic hash IDs instead). As a result the engine never binds SQL parameters. Data operations go through the DataFrame and DeltaTable APIs, history, detail and optimize go through the DeltaTable API, and the few DDL statements go through one function, `run_ddl` in `close_engine/io/sql.py`, which only accepts `LogicalTable` placeholders rendered by a `TableResolver` and refuses parameter markers and anything that is not a single CREATE TABLE, ALTER TABLE or COMMENT ON TABLE statement.

I did not choose to disable Delta's required-configuration check so that a session without the Delta parser could read Delta tables, because that check exists to make sure Delta's own rules (for example the ones that honour deletion vectors) are active, and turning it off risks reads that silently return deleted rows.

## Consequences

The lint rules SB010 (`spark.sql` with `args`) and SB011 (`spark.sql` outside `close_engine/io/sql.py`) enforce the rule in pre-commit and CI, and the seeded-violation test proves the lint catches both. Three canary tests in `engine/tests/spark/test_known_incompatibilities.py` assert the bug is still there as strict expected failures, so when a Delta release with the fix is installed they start passing, the suite fails, and we revisit this ADR on purpose, while a fourth test runs plain Spark in its own process to show binding works without Delta.

The read path in the plan (D8) assumed parameterised SQL templates, so its direction changes in ADR-0005. On Databricks, the runtime's own Delta build is expected to bind parameters, but that is not verified yet, so it goes into the Stage 10 parity suite, and the engine design no longer depends on the answer either way.
