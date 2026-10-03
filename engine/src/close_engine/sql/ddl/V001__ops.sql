-- V001__ops.sql: Platform bookkeeping. The Stage 1 tables keep the exact definitions the smoke job and the fence create.
-- Generated once from close_contracts.table_schemas; from here on it is history and never edited.

CREATE TABLE IF NOT EXISTS {ops__schema_migrations} (
  version INT NOT NULL,
  name STRING NOT NULL,
  checksum STRING NOT NULL COMMENT 'sha256 of the migration file',
  statements INT NOT NULL,
  applied_at TIMESTAMP NOT NULL,
  run_id STRING NOT NULL,
  engine_version STRING NOT NULL
) USING DELTA
COMMENT 'Applied V### migrations; ddl_apply fails if an applied file has changed.'
TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS {ops__smoke_results} (
  run_id STRING,
  seq BIGINT,
  row_id STRING,
  entity_id STRING,
  currency STRING,
  amount_minor BIGINT,
  amount_eur_minor BIGINT
) USING DELTA
PARTITIONED BY (run_id)
COMMENT 'Smoke job output (Stage 1), replaced per run_id.';

CREATE TABLE IF NOT EXISTS {ops__run_manifests} (
  run_id STRING NOT NULL,
  attempt BIGINT NOT NULL,
  fencing_token BIGINT NOT NULL,
  job STRING NOT NULL,
  entity_id STRING NOT NULL,
  row_count BIGINT NOT NULL,
  digest STRING NOT NULL
) USING DELTA
TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS {ops__writer_claims} (
  resource STRING NOT NULL,
  fencing_token BIGINT NOT NULL,
  run_id STRING NOT NULL,
  attempt INT NOT NULL,
  job STRING NOT NULL,
  claimed_at TIMESTAMP NOT NULL
) USING DELTA
PARTITIONED BY (resource)
TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS {ops__run_metrics} (
  run_id STRING NOT NULL,
  attempt INT NOT NULL,
  fencing_token BIGINT NOT NULL,
  job STRING NOT NULL,
  metric STRING NOT NULL COMMENT 'for example control_total.rows, control_total.amount, digest, rule.auto',
  dimension STRING NOT NULL COMMENT 'what the metric is about: a table, a source and currency, a rule',
  value BIGINT,
  text STRING,
  recorded_at TIMESTAMP NOT NULL
) USING DELTA
COMMENT 'Control totals, per-rule metrics, invariants and digests of every job run (long format).'
TBLPROPERTIES ('delta.appendOnly' = 'true');
