"""Multi-currency manual journals (Stage 7c, ADR-0018).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-01

- **A journal's currency** (``journals.currency``) is now the currency it was entered in. ``functional_currency``
  is its entity's, backfilled from ``currency`` (every earlier journal was in its functional currency).
- **Functional totals** (``functional_debit_minor``, ``functional_credit_minor`` and ``ledger_line_count``, which
  counts a rounding line) are set at submit and cleared when a journal returns to draft. They are backfilled for
  every live journal, so routing records, the tie-out and the evidence read one set of columns.
- **Conversions.** ``journal_conversions`` freezes each submission of a foreign-currency journal: the rate with
  its date, type, version, source and Delta table version, the canonical conversion (``detail``, each line's
  functional amount and the rounding line) and its hash. It is insert-only. ``journals.conversion_hash`` names
  the current submission's, and every approval records the one it approved.
- **The posting job's view** gains each line's functional amount and rate, and the rounding line as one more
  line. ``CREATE OR REPLACE`` keeps the view's grant to ``close_ledger_reader``.
- **The downgrade** refuses while any conversion exists, rather than leave foreign-currency journals without one.
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

LIVE = "('pending_approval', 'approved', 'posting', 'posted', 'erp_confirmed')"
_APPROVALS = """(SELECT string_agg(a.level || ':' || a.approver || COALESCE(' for ' || a.on_behalf_of, ''), '; '
                              ORDER BY a.level)
              FROM journal_approvals a WHERE a.journal_id = j.journal_id AND a.submission = j.submission)"""
# 0005's view, which the downgrade restores.
OLD_VIEW = f"""
    CREATE VIEW posting_queue WITH (security_barrier) AS
    SELECT j.posting_run_id, j.journal_id, j.entity_id, j.number, j.revision, j.content_hash, j.template_id,
           j.period, j.accounting_date, j.currency, j.description AS journal_description, j.source,
           j.created_by AS preparer, j.approved_at,
           {_APPROVALS} AS approvals,
           l.line_no, l.account, l.side, l.amount_minor, l.dimensions, l.description AS line_description
      FROM journals j
      JOIN journal_lines l ON l.journal_id = j.journal_id AND l.revision = j.revision
     WHERE j.status = 'posting'"""  # noqa: S608 (fixed SQL text, no input)
NEW_VIEW = f"""
    CREATE OR REPLACE VIEW posting_queue WITH (security_barrier) AS
    SELECT j.posting_run_id, j.journal_id, j.entity_id, j.number, j.revision, j.content_hash, j.template_id,
           j.period, j.accounting_date, j.currency, j.description AS journal_description, j.source,
           j.created_by AS preparer, j.approved_at,
           {_APPROVALS} AS approvals,
           l.line_no, l.account, l.side, l.amount_minor, l.dimensions, l.description AS line_description,
           COALESCE((c.detail #>> ARRAY['lines', (l.line_no - 1)::text, 'functional_amount_minor'])::bigint,
                    l.amount_minor) AS functional_amount_minor,
           j.functional_currency, c.rate AS fx_rate, c.rate_date AS fx_rate_date, c.rate_type AS fx_rate_type,
           c.rate_version AS fx_rate_version, 'entry'::text AS line_kind
      FROM journals j
      JOIN journal_lines l ON l.journal_id = j.journal_id AND l.revision = j.revision
      LEFT JOIN journal_conversions c ON c.journal_id = j.journal_id AND c.submission = j.submission
     WHERE j.status = 'posting'
    UNION ALL
    SELECT j.posting_run_id, j.journal_id, j.entity_id, j.number, j.revision, j.content_hash, j.template_id,
           j.period, j.accounting_date, j.currency, j.description, j.source, j.created_by, j.approved_at,
           {_APPROVALS},
           j.line_count + 1, c.rounding_account, c.rounding_side, 0::bigint, '{{}}'::jsonb, 'FX rounding',
           c.rounding_minor, j.functional_currency, c.rate, c.rate_date, c.rate_type, c.rate_version,
           'fx_rounding'::text
      FROM journals j
      JOIN journal_conversions c ON c.journal_id = j.journal_id AND c.submission = j.submission
     WHERE j.status = 'posting' AND c.rounding_minor > 0"""  # noqa: S608 (fixed SQL text, no input)
GRANT_READER = """
    DO $$
    BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'close_ledger_reader') THEN
        EXECUTE 'GRANT SELECT ON posting_queue TO close_ledger_reader';
      END IF;
    END
    $$"""

UPGRADE = [
    "ALTER TABLE journals ADD COLUMN functional_currency text",
    "UPDATE journals SET functional_currency = currency",
    "ALTER TABLE journals ALTER COLUMN functional_currency SET NOT NULL",
    "ALTER TABLE journals ADD CONSTRAINT journals_functional_currency_code CHECK (functional_currency ~ '^[A-Z]{3}$')",
    "ALTER TABLE journals ADD COLUMN functional_debit_minor bigint CHECK (functional_debit_minor >= 0)",
    "ALTER TABLE journals ADD COLUMN functional_credit_minor bigint CHECK (functional_credit_minor >= 0)",
    "ALTER TABLE journals ADD COLUMN ledger_line_count integer CHECK (ledger_line_count >= 2)",
    "ALTER TABLE journals ADD COLUMN conversion_hash text CHECK (conversion_hash ~ '^[0-9a-f]{64}$')",
    f"""
    UPDATE journals SET functional_debit_minor = total_debit_minor, functional_credit_minor = total_credit_minor,
           ledger_line_count = line_count
     WHERE status IN {LIVE}""",  # noqa: S608 (a fixed status list, no input)
    f"""
    ALTER TABLE journals ADD CONSTRAINT journals_live_has_functional_totals CHECK (
      status NOT IN {LIVE} OR (functional_debit_minor IS NOT NULL AND functional_credit_minor IS NOT NULL
                               AND functional_debit_minor = functional_credit_minor
                               AND ledger_line_count IS NOT NULL))""",
    f"""
    ALTER TABLE journals ADD CONSTRAINT journals_foreign_has_conversion CHECK (
      status NOT IN {LIVE} OR currency = functional_currency OR conversion_hash IS NOT NULL)""",
    "ALTER TABLE journal_approvals ADD COLUMN conversion_hash text CHECK (conversion_hash ~ '^[0-9a-f]{64}$')",
    """
    CREATE TABLE journal_conversions (
      journal_id             uuid NOT NULL REFERENCES journals (journal_id),
      submission             integer NOT NULL CHECK (submission >= 0),  -- 0: approved by a bypass, never submitted
      revision               integer NOT NULL CHECK (revision >= 1),
      currency               text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
      functional_currency    text NOT NULL CHECK (functional_currency ~ '^[A-Z]{3}$'),
      rate                   numeric(19, 12) NOT NULL CHECK (rate > 0),
      rate_date              date NOT NULL,
      rate_type              text NOT NULL CHECK (rate_type IN ('daily_spot', 'closing')),
      rate_version           integer NOT NULL CHECK (rate_version >= 1),
      rate_source            text NOT NULL,
      rate_table_version     bigint,
      reused_from            uuid REFERENCES journals (journal_id),
      detail                 jsonb NOT NULL,
      conversion_hash        text NOT NULL CHECK (conversion_hash ~ '^[0-9a-f]{64}$'),
      functional_total_minor bigint NOT NULL CHECK (functional_total_minor >= 0),
      rounding_side          text CHECK (rounding_side IN ('DR', 'CR')),
      rounding_minor         bigint NOT NULL CHECK (rounding_minor >= 0),
      rounding_account       text,
      created_at             timestamptz NOT NULL DEFAULT clock_timestamp(),
      PRIMARY KEY (journal_id, submission),
      CHECK (functional_currency <> currency),
      CHECK ((rounding_minor = 0) = (rounding_side IS NULL)),
      CHECK (rounding_side IS NULL OR rounding_account IS NOT NULL)
    )""",
    "REVOKE UPDATE, DELETE, TRUNCATE ON journal_conversions FROM close_app",
    NEW_VIEW,
    GRANT_READER,  # CREATE OR REPLACE keeps the grant; this restates it for a reader role created since 0005
]

DOWNGRADE = [
    """
    DO $$
    BEGIN
      IF EXISTS (SELECT FROM journal_conversions) THEN
        RAISE EXCEPTION 'foreign-currency journals exist; 0005 has no place for their conversions';
      END IF;
    END
    $$""",
    "DROP VIEW IF EXISTS posting_queue",
    OLD_VIEW,
    GRANT_READER,
    "DROP TABLE IF EXISTS journal_conversions",
    "ALTER TABLE journal_approvals DROP COLUMN conversion_hash",
    "ALTER TABLE journals DROP CONSTRAINT journals_foreign_has_conversion",
    "ALTER TABLE journals DROP CONSTRAINT journals_live_has_functional_totals",
    "ALTER TABLE journals DROP COLUMN conversion_hash",
    "ALTER TABLE journals DROP COLUMN ledger_line_count",
    "ALTER TABLE journals DROP COLUMN functional_credit_minor",
    "ALTER TABLE journals DROP COLUMN functional_debit_minor",
    "ALTER TABLE journals DROP CONSTRAINT journals_functional_currency_code",
    "ALTER TABLE journals DROP COLUMN functional_currency",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
