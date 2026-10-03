-- V008__journal_fx.sql: The rate on each posted line, and the rounding line's kind (Stage 7c, ADR-0018).
-- Added columns are nullable, so rows posted before Stage 7c keep nulls. From here on this file is history.

ALTER TABLE {ledger__journal_lines} ADD COLUMNS (
  fx_rate DECIMAL(19,12) COMMENT 'the rate a foreign-currency line was converted at (D9)',
  fx_rate_date DATE,
  fx_rate_type STRING,
  fx_rate_version INT,
  line_kind STRING COMMENT 'entry, or fx_rounding for the rounding line of a foreign-currency journal'
);
