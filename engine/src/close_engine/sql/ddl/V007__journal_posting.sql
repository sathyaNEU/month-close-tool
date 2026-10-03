-- V007__journal_posting.sql: Posted manual journals and the ERP feed (Stage 7b, ADR-0017).
-- Generated once from close_contracts.table_schemas; from here on it is history and never edited.

CREATE TABLE IF NOT EXISTS {ledger__journal_lines} (
  journal_id STRING NOT NULL,
  entity_id STRING NOT NULL,
  number BIGINT NOT NULL COMMENT 'gapless journal number within the entity',
  revision INT NOT NULL,
  content_hash STRING NOT NULL COMMENT 'the content the approvals were bound to',
  line_no BIGINT NOT NULL,
  template_id STRING NOT NULL,
  source STRING NOT NULL COMMENT 'manual, import, recurring or reversal',
  period STRING NOT NULL,
  accounting_date DATE NOT NULL,
  account STRING NOT NULL,
  side STRING NOT NULL COMMENT 'DR or CR',
  amount_minor BIGINT NOT NULL,
  currency STRING NOT NULL,
  functional_amount_minor BIGINT NOT NULL,
  functional_currency STRING NOT NULL,
  debit_minor BIGINT NOT NULL COMMENT 'functional amount on the debit side, else 0',
  credit_minor BIGINT NOT NULL COMMENT 'functional amount on the credit side, else 0',
  dimensions MAP<STRING,STRING> NOT NULL,
  line_description STRING NOT NULL,
  journal_description STRING NOT NULL,
  preparer STRING NOT NULL,
  approvals STRING NOT NULL COMMENT 'level:approver per level, plus for <delegator> when delegated, separated by semicolons',
  approved_at TIMESTAMP NOT NULL,
  run_id STRING NOT NULL COMMENT 'the posting run',
  posted_at TIMESTAMP NOT NULL COMMENT 'when the posting run was requested (Postgres time), on every line'
) USING DELTA
PARTITIONED BY (entity_id)
COMMENT 'Posted manual journal lines (Stage 7b): the authoritative ledger, written in one commit per run.'
TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS {ledger__journal_postings} (
  journal_id STRING NOT NULL,
  entity_id STRING NOT NULL,
  number BIGINT NOT NULL,
  period STRING NOT NULL,
  content_hash STRING NOT NULL,
  payload STRING NOT NULL COMMENT 'canonical JSON of the journal and its lines, for the ERP',
  run_id STRING NOT NULL,
  posted_at TIMESTAMP NOT NULL COMMENT 'when the posting run was requested (Postgres time)'
) USING DELTA
PARTITIONED BY (entity_id)
COMMENT 'The ERP feed (Stage 7b): one row per posted journal, written after its lines.'
TBLPROPERTIES ('delta.appendOnly' = 'true');
