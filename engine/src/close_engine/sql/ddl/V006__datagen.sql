-- V006__datagen.sql: The generator's run record, control totals and answer key.
-- Generated once from close_contracts.table_schemas; from here on it is history and never edited.

CREATE TABLE IF NOT EXISTS {ops__datagen_runs} (
  run_id STRING NOT NULL,
  profile STRING NOT NULL,
  seed BIGINT NOT NULL,
  config_hash STRING NOT NULL,
  start_date DATE NOT NULL,
  days INT NOT NULL,
  generator_version STRING NOT NULL,
  generated_at TIMESTAMP NOT NULL
) USING DELTA
TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS {ops__datagen_manifest} (
  run_id STRING NOT NULL,
  table_key STRING NOT NULL,
  currency STRING NOT NULL COMMENT '\'*\' for tables without amounts',
  row_count BIGINT NOT NULL,
  amount_total_minor BIGINT NOT NULL COMMENT 'signed control total in minor units',
  digest STRING NOT NULL
) USING DELTA
COMMENT 'Landing control totals the generator wrote; the gate checks them against the landing tables.'
TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS {ops__answer_key_groups} (
  run_id STRING NOT NULL,
  template INT NOT NULL,
  group_key STRING NOT NULL,
  expected_outcome STRING NOT NULL COMMENT 'auto, suggested or open',
  break_type STRING,
  entity_id STRING NOT NULL,
  currency STRING NOT NULL
) USING DELTA
TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS {ops__answer_key_members} (
  run_id STRING NOT NULL,
  template INT NOT NULL,
  group_key STRING NOT NULL,
  side STRING NOT NULL,
  source_table STRING NOT NULL,
  source_key STRING NOT NULL
) USING DELTA
TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS {ops__injected_breaks} (
  run_id STRING NOT NULL,
  break_type STRING NOT NULL,
  source_table STRING NOT NULL,
  source_key STRING NOT NULL,
  detail STRING NOT NULL
) USING DELTA
TBLPROPERTIES ('delta.appendOnly' = 'true');
