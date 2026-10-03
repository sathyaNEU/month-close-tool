-- V002__ref.sql: Reference data: currencies, entities, FX rates, calendars, periods, account mappings.
-- Generated once from close_contracts.table_schemas; from here on it is history and never edited.

CREATE TABLE IF NOT EXISTS {ref__currencies} (
  currency STRING NOT NULL,
  exponent INT NOT NULL,
  name STRING NOT NULL
) USING DELTA;

CREATE TABLE IF NOT EXISTS {ref__entities} (
  entity_id STRING NOT NULL,
  name STRING NOT NULL,
  functional_currency STRING NOT NULL,
  country STRING NOT NULL,
  timezone STRING NOT NULL,
  calendar_code STRING NOT NULL
) USING DELTA;

CREATE TABLE IF NOT EXISTS {ref__fx_rates} (
  rate_date DATE NOT NULL,
  from_currency STRING NOT NULL,
  to_currency STRING NOT NULL,
  rate_type STRING NOT NULL COMMENT 'daily_spot or closing',
  rate DECIMAL(19,12) NOT NULL,
  version INT NOT NULL,
  source STRING NOT NULL
) USING DELTA
TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS {ref__calendars} (
  calendar_code STRING NOT NULL,
  day DATE NOT NULL,
  is_business_day BOOLEAN NOT NULL,
  bd_index INT NOT NULL COMMENT 'business-day index (close_contracts.calendar)'
) USING DELTA
PARTITIONED BY (calendar_code);

CREATE TABLE IF NOT EXISTS {ref__periods} (
  entity_id STRING NOT NULL,
  period STRING NOT NULL COMMENT 'YYYY-MM',
  start_date DATE NOT NULL,
  end_date DATE NOT NULL,
  status STRING NOT NULL COMMENT 'open or closed; Postgres is authoritative, this is the engine\'s pinned copy',
  status_version INT NOT NULL
) USING DELTA;

CREATE TABLE IF NOT EXISTS {ref__account_mappings} (
  mapping_id STRING NOT NULL,
  version INT NOT NULL,
  entity_id STRING NOT NULL,
  match_key STRING NOT NULL COMMENT 'the attribute combination a posting rule looks up',
  account STRING NOT NULL,
  effective_from DATE NOT NULL,
  effective_to DATE
) USING DELTA
TBLPROPERTIES ('delta.appendOnly' = 'true');
