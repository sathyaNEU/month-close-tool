-- V009__gl_rollup_functional.sql: Functional amounts on the GL rollups (Stage 8a, ADR-0019).
-- Added columns are nullable; the journaling stand-in always fills them. From here on this file is history.

ALTER TABLE {ledger__gl_rollup_lines} ADD COLUMNS (
  functional_debit_minor BIGINT COMMENT 'debits in the entity functional currency',
  functional_credit_minor BIGINT COMMENT 'credits in the entity functional currency'
);
