"""The general ledger in the control plane (Stage 8a, ADR-0019).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-01

- **Configuration snapshots.** ``config_snapshots`` keeps, by its SHA-256, what a job run depends on: the active
  journaling configuration and entity set, and the entity's period statuses. It is insert-only, and a job reads its
  snapshot through ``job_config_snapshots``, which shows only snapshots of runs in progress, the one other view the
  jobs' read-only role may select.
- **Journaling runs.** ``journaling_runs`` records each run of the journaling stand-in and its outcome, and
  ``ledger_pins`` the configuration and inputs the first successful run for an entity wrote its ledger from. A pin
  never changes, and later runs must match it.
- **The ERP's trial balance.** ``erp_trial_balances`` keeps every uploaded extract, accepted or rejected, by entity,
  period and version with its SHA-256, and ``erp_trial_balance_lines`` the accepted ones' balances: signed minor
  units in the entity's functional currency, debit positive. Both are insert-only.
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

HASH = "'^[0-9a-f]{64}$'"
UPGRADE = [
    f"""
    CREATE TABLE config_snapshots (
      snapshot_hash      text PRIMARY KEY CHECK (snapshot_hash ~ {HASH}),
      purpose            text NOT NULL CHECK (purpose IN ('journaling')),
      configuration_hash text NOT NULL CHECK (configuration_hash ~ {HASH}),
      entity_id          text NOT NULL CHECK (entity_id ~ '^[A-Z0-9]{{2,16}}$'),
      content            jsonb NOT NULL,
      created_by         text NOT NULL,
      created_at         timestamptz NOT NULL DEFAULT clock_timestamp()
    )""",
    "REVOKE UPDATE, DELETE, TRUNCATE ON config_snapshots FROM close_app",
    f"""
    CREATE TABLE journaling_runs (
      run_id                   uuid PRIMARY KEY,
      entity_id                text NOT NULL CHECK (entity_id ~ '^[A-Z0-9]{{2,16}}$'),
      requested_by             text NOT NULL,
      requested_at             timestamptz NOT NULL DEFAULT clock_timestamp(),
      snapshot_hash            text NOT NULL REFERENCES config_snapshots (snapshot_hash),
      configuration_hash       text NOT NULL CHECK (configuration_hash ~ {HASH}),
      pinned_inputs            text CHECK (pinned_inputs ~ {HASH}),
      status                   text NOT NULL CHECK (status IN ('running', 'reconciled', 'refused', 'failed')),
      inputs_digest            text CHECK (inputs_digest ~ {HASH}),
      events                   integer,
      lines                    integer,
      lines_written            integer,
      rollups                  integer,
      rollups_written          integer,
      suspense_lines           integer,
      prior_period_adjustments integer,
      commit_version           bigint,
      reason                   text,
      result                   jsonb,
      reconciled_at            timestamptz,
      row_version              integer NOT NULL DEFAULT 1 CHECK (row_version >= 1),
      CHECK (status = 'running' OR reconciled_at IS NOT NULL)
    )""",
    "CREATE INDEX journaling_runs_in_progress ON journaling_runs (entity_id) WHERE status = 'running'",
    "REVOKE DELETE, TRUNCATE ON journaling_runs FROM close_app",
    f"""
    CREATE TABLE ledger_pins (
      entity_id          text PRIMARY KEY CHECK (entity_id ~ '^[A-Z0-9]{{2,16}}$'),
      configuration_hash text NOT NULL CHECK (configuration_hash ~ {HASH}),
      inputs_digest      text NOT NULL CHECK (inputs_digest ~ {HASH}),
      run_id             uuid NOT NULL REFERENCES journaling_runs (run_id),
      pinned_at          timestamptz NOT NULL DEFAULT clock_timestamp()
    )""",
    "REVOKE UPDATE, DELETE, TRUNCATE ON ledger_pins FROM close_app",
    f"""
    CREATE TABLE erp_trial_balances (
      extract_id  uuid PRIMARY KEY,
      entity_id   text NOT NULL CHECK (entity_id ~ '^[A-Z0-9]{{2,16}}$'),
      period      text NOT NULL CHECK (period ~ '^[0-9]{{4}}-(0[1-9]|1[0-2])$'),
      version     integer NOT NULL CHECK (version >= 1),
      status      text NOT NULL CHECK (status IN ('accepted', 'rejected')),
      sha256      text NOT NULL CHECK (sha256 ~ {HASH}),
      currency    text NOT NULL CHECK (currency ~ '^[A-Z]{{3}}$'),
      line_count  integer NOT NULL CHECK (line_count >= 0),
      issues      jsonb NOT NULL DEFAULT '[]'::jsonb,
      uploaded_by text NOT NULL,
      uploaded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      UNIQUE (entity_id, period, version)
    )""",
    """
    CREATE TABLE erp_trial_balance_lines (
      extract_id    uuid NOT NULL REFERENCES erp_trial_balances (extract_id),
      account       text NOT NULL CHECK (length(account) BETWEEN 1 AND 32),
      balance_minor bigint NOT NULL,
      PRIMARY KEY (extract_id, account)
    )""",
    "REVOKE UPDATE, DELETE, TRUNCATE ON erp_trial_balances, erp_trial_balance_lines FROM close_app",
    """
    CREATE VIEW job_config_snapshots WITH (security_barrier) AS
    SELECT s.snapshot_hash, s.content
      FROM config_snapshots s
     WHERE EXISTS (SELECT 1 FROM journaling_runs r WHERE r.snapshot_hash = s.snapshot_hash AND r.status = 'running')""",
    """
    DO $$
    BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'close_ledger_reader') THEN
        EXECUTE 'GRANT SELECT ON job_config_snapshots TO close_ledger_reader';
      END IF;
    END
    $$""",
]

DOWNGRADE = [
    "DROP VIEW IF EXISTS job_config_snapshots",
    "DROP TABLE IF EXISTS erp_trial_balance_lines",
    "DROP TABLE IF EXISTS erp_trial_balances",
    "DROP TABLE IF EXISTS ledger_pins",
    "DROP TABLE IF EXISTS journaling_runs",
    "DROP TABLE IF EXISTS config_snapshots",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
