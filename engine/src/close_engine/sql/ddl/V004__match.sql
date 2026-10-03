-- V004__match.sql: Matching outputs (Stage 3): item versions, item and group state, adjustments, logs.
-- Generated once from close_contracts.table_schemas; from here on it is history and never edited.

CREATE TABLE IF NOT EXISTS {match__item_versions} (
  data_source_id STRING NOT NULL,
  item_id STRING NOT NULL,
  item_version INT NOT NULL,
  entity_id STRING NOT NULL,
  currency STRING NOT NULL,
  amount_minor BIGINT NOT NULL,
  transaction_date DATE NOT NULL,
  value_date DATE,
  reference STRING,
  reference_2 STRING,
  reference_3 STRING,
  description STRING,
  counterparty STRING,
  text_1 STRING,
  text_2 STRING,
  text_3 STRING,
  text_4 STRING,
  text_5 STRING,
  text_6 STRING,
  number_1 BIGINT,
  number_2 BIGINT,
  number_3 BIGINT,
  number_4 BIGINT,
  date_1 DATE,
  date_2 DATE,
  source_table STRING NOT NULL,
  source_version BIGINT NOT NULL COMMENT 'the landing table\'s Delta version read',
  source_key STRING NOT NULL COMMENT 'canonical JSON of the identity key values',
  row_hash STRING NOT NULL COMMENT 'sha256 of the landing row',
  is_void BOOLEAN NOT NULL COMMENT 'the source row was deleted',
  run_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL
) USING DELTA
PARTITIONED BY (data_source_id)
TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS {match__items} (
  match_set_id STRING NOT NULL,
  item_id STRING NOT NULL,
  data_source_id STRING NOT NULL,
  role STRING NOT NULL,
  item_version INT NOT NULL,
  status STRING NOT NULL COMMENT 'open, matched, suggested, in_transit, voided',
  group_id STRING,
  suggestion_group_id STRING,
  updated_run_id STRING NOT NULL,
  updated_at TIMESTAMP NOT NULL
) USING DELTA
PARTITIONED BY (match_set_id)
COMMENT 'Current state per item and match set (MERGE on the literal match_set_id partition).';

CREATE TABLE IF NOT EXISTS {match__groups} (
  match_set_id STRING NOT NULL,
  group_id STRING NOT NULL,
  rule_id STRING,
  match_type STRING NOT NULL COMMENT 'automatic, suggested, manual, partial',
  status STRING NOT NULL COMMENT 'active, pending, confirmed, rejected, superseded, unmatched, broken',
  ambiguous BOOLEAN NOT NULL,
  currency STRING NOT NULL,
  difference_minor BIGINT NOT NULL,
  reason_code STRING,
  created_run_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  updated_run_id STRING NOT NULL,
  updated_at TIMESTAMP NOT NULL
) USING DELTA
PARTITIONED BY (match_set_id);

CREATE TABLE IF NOT EXISTS {match__group_items} (
  match_set_id STRING NOT NULL,
  group_id STRING NOT NULL,
  item_id STRING NOT NULL,
  item_version INT NOT NULL,
  role STRING NOT NULL,
  amount_minor BIGINT NOT NULL
) USING DELTA
PARTITIONED BY (match_set_id)
TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS {match__adjustment_requests} (
  match_set_id STRING NOT NULL,
  request_id STRING NOT NULL,
  action STRING NOT NULL COMMENT 'create or cancel',
  group_id STRING NOT NULL,
  reason_code STRING NOT NULL,
  entity_id STRING NOT NULL,
  currency STRING NOT NULL,
  amount_minor BIGINT NOT NULL,
  run_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL
) USING DELTA
PARTITIONED BY (match_set_id)
TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS {match__item_state_log} (
  match_set_id STRING NOT NULL,
  item_id STRING NOT NULL,
  run_id STRING NOT NULL,
  seq INT NOT NULL,
  from_status STRING,
  to_status STRING NOT NULL,
  group_id STRING,
  reason STRING NOT NULL,
  at TIMESTAMP NOT NULL
) USING DELTA
PARTITIONED BY (match_set_id)
TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS {match__timing_classifications} (
  match_set_id STRING NOT NULL,
  item_id STRING NOT NULL,
  rule_id STRING NOT NULL,
  expected_clear_date DATE NOT NULL,
  run_id STRING NOT NULL,
  classified_at TIMESTAMP NOT NULL
) USING DELTA
PARTITIONED BY (match_set_id)
TBLPROPERTIES ('delta.appendOnly' = 'true');
