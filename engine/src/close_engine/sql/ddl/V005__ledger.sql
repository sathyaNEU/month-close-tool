-- V005__ledger.sql: Subledger and GL outputs (Stage 6).
-- Generated once from close_contracts.table_schemas; from here on it is history and never edited.

CREATE TABLE IF NOT EXISTS {ledger__accounting_events} (
  event_id STRING NOT NULL,
  event_version INT NOT NULL,
  event_type STRING NOT NULL,
  entity_id STRING NOT NULL,
  program STRING,
  order_id STRING,
  seller_id STRING,
  occurred_at TIMESTAMP NOT NULL,
  accounting_date DATE NOT NULL,
  currency STRING NOT NULL,
  amounts_minor MAP<STRING,BIGINT> NOT NULL COMMENT 'amount by role, for example gross, fee, tax',
  attributes MAP<STRING,STRING> NOT NULL,
  is_reversal BOOLEAN NOT NULL,
  source_table STRING NOT NULL,
  source_key STRING NOT NULL,
  source_version BIGINT NOT NULL,
  run_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL
) USING DELTA
PARTITIONED BY (entity_id)
TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS {ledger__subledger_lines} (
  line_id STRING NOT NULL,
  event_id STRING NOT NULL,
  event_version INT NOT NULL,
  posting_rule_id STRING NOT NULL,
  posting_rule_version INT NOT NULL,
  entity_id STRING NOT NULL,
  accounting_date DATE NOT NULL,
  period STRING NOT NULL,
  account STRING NOT NULL,
  side STRING NOT NULL COMMENT 'DR or CR',
  amount_minor BIGINT NOT NULL,
  currency STRING NOT NULL,
  functional_amount_minor BIGINT NOT NULL,
  functional_currency STRING NOT NULL,
  fx_rate DECIMAL(19,12),
  fx_rate_type STRING,
  fx_rate_date DATE,
  fx_rate_version INT,
  dimensions MAP<STRING,STRING> NOT NULL,
  description STRING NOT NULL,
  rollup_line_id STRING NOT NULL,
  is_suspense BOOLEAN NOT NULL,
  intended_account STRING,
  is_prior_period_adjustment BOOLEAN NOT NULL,
  original_period STRING,
  run_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL
) USING DELTA
PARTITIONED BY (entity_id)
TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS {ledger__gl_rollup_lines} (
  rollup_line_id STRING NOT NULL,
  entity_id STRING NOT NULL,
  period STRING NOT NULL,
  accounting_date DATE COMMENT 'set for daily rollups',
  account STRING NOT NULL,
  dimensions_key STRING NOT NULL,
  dimensions MAP<STRING,STRING> NOT NULL,
  currency STRING NOT NULL,
  debit_minor BIGINT NOT NULL,
  credit_minor BIGINT NOT NULL,
  line_count BIGINT NOT NULL,
  run_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL
) USING DELTA
PARTITIONED BY (entity_id)
TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS {ledger__gl_outbox} (
  outbox_id STRING NOT NULL,
  rollup_line_id STRING NOT NULL,
  entity_id STRING NOT NULL,
  period STRING NOT NULL,
  payload STRING NOT NULL COMMENT 'canonical JSON sent to the general ledger',
  run_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL
) USING DELTA
TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS {ledger__adjustment_consumption} (
  request_id STRING NOT NULL,
  match_set_id STRING NOT NULL,
  event_id STRING NOT NULL,
  run_id STRING NOT NULL,
  consumed_at TIMESTAMP NOT NULL
) USING DELTA
COMMENT 'Each adjustment request is booked exactly once.'
TBLPROPERTIES ('delta.appendOnly' = 'true');
