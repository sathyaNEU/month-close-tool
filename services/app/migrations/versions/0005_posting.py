"""Posting to the Databricks ledger and the ERP write-back (Stage 7b, ADR-0017).

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-30

- **Statuses.** A journal can also be ``posting`` (frozen into a posting run), ``posted`` (its lines are in
  ``ledger.journal_lines``) and ``erp_confirmed`` (an ERP wrote back its document number).
- **Posting runs.** ``posting_runs`` records each run: its journals, any skipped for separation of duties, and
  the reconciled outcome.
- **The posting job's only window.** ``posting_queue`` shows the lines of journals frozen into running posting
  runs, and ``close_ledger_reader`` can select it and nothing else. It is a ``security_barrier`` view, so a
  predicate the reader adds can't see rows outside it. The role itself comes from the deploy step (``pg-roles``,
  ``deploy/postgres/initdb/20-ledger-reader.sh``), so the grant is skipped where the role doesn't exist.
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

STATUSES = ("draft", "pending_approval", "approved", "posting", "posted", "erp_confirmed", "cancelled")
OLD_STATUSES = ("draft", "pending_approval", "approved", "cancelled")


def _status_check(statuses: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{s}'" for s in statuses)
    return f"ALTER TABLE journals ADD CONSTRAINT journals_status_check CHECK (status IN ({listed}))"


UPGRADE = [
    """
    CREATE TABLE posting_runs (
      run_id          uuid PRIMARY KEY,
      entity_id       text NOT NULL CHECK (entity_id ~ '^[A-Z0-9]{2,16}$'),
      requested_by    text NOT NULL,
      requested_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
      journal_count   integer NOT NULL CHECK (journal_count >= 1),
      skipped         jsonb NOT NULL,
      status          text NOT NULL CHECK (status IN ('posting', 'reconciled', 'failed')),
      written         integer,
      already_present integer,
      commit_version  bigint,
      result          jsonb,
      reconciled_at   timestamptz,
      row_version     integer NOT NULL DEFAULT 1 CHECK (row_version >= 1),
      CHECK (status = 'posting' OR reconciled_at IS NOT NULL)
    )""",
    "ALTER TABLE journals DROP CONSTRAINT journals_status_check",
    _status_check(STATUSES),
    "ALTER TABLE journals ADD COLUMN posting_run_id uuid REFERENCES posting_runs (run_id)",
    "ALTER TABLE journals ADD COLUMN posted_at timestamptz",
    "ALTER TABLE journals ADD COLUMN posted_version bigint",
    "ALTER TABLE journals ADD COLUMN erp_document_id text CHECK (length(erp_document_id) BETWEEN 1 AND 50)",
    "ALTER TABLE journals ADD COLUMN erp_confirmed_at timestamptz",
    "ALTER TABLE journals ADD COLUMN erp_confirmed_by text",
    "ALTER TABLE journals ADD CONSTRAINT journals_posting_has_run "
    "CHECK (status <> 'posting' OR posting_run_id IS NOT NULL)",
    "ALTER TABLE journals ADD CONSTRAINT journals_posted_has_time "
    "CHECK (status NOT IN ('posted', 'erp_confirmed') OR (posted_at IS NOT NULL AND posting_run_id IS NOT NULL))",
    "ALTER TABLE journals ADD CONSTRAINT journals_confirmed_has_document "
    "CHECK (status <> 'erp_confirmed' OR (erp_document_id IS NOT NULL AND erp_confirmed_at IS NOT NULL))",
    "CREATE INDEX journals_by_posting_run ON journals (posting_run_id) WHERE posting_run_id IS NOT NULL",
    """
    CREATE VIEW posting_queue WITH (security_barrier) AS
    SELECT j.posting_run_id, j.journal_id, j.entity_id, j.number, j.revision, j.content_hash, j.template_id,
           j.period, j.accounting_date, j.currency, j.description AS journal_description, j.source,
           j.created_by AS preparer, j.approved_at,
           (SELECT string_agg(a.level || ':' || a.approver || COALESCE(' for ' || a.on_behalf_of, ''), '; '
                              ORDER BY a.level)
              FROM journal_approvals a WHERE a.journal_id = j.journal_id AND a.submission = j.submission) AS approvals,
           l.line_no, l.account, l.side, l.amount_minor, l.dimensions, l.description AS line_description
      FROM journals j
      JOIN journal_lines l ON l.journal_id = j.journal_id AND l.revision = j.revision
     WHERE j.status = 'posting'""",
    """
    DO $$
    BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'close_ledger_reader') THEN
        EXECUTE 'GRANT SELECT ON posting_queue TO close_ledger_reader';
      END IF;
    END
    $$""",
    "REVOKE DELETE, TRUNCATE ON posting_runs FROM close_app",
]

DOWNGRADE = [
    "DROP VIEW IF EXISTS posting_queue",
    "UPDATE journals SET status = 'approved' WHERE status IN ('posting', 'posted', 'erp_confirmed')",
    "ALTER TABLE journals DROP CONSTRAINT journals_confirmed_has_document",
    "ALTER TABLE journals DROP CONSTRAINT journals_posted_has_time",
    "ALTER TABLE journals DROP CONSTRAINT journals_posting_has_run",
    "ALTER TABLE journals DROP COLUMN erp_confirmed_by",
    "ALTER TABLE journals DROP COLUMN erp_confirmed_at",
    "ALTER TABLE journals DROP COLUMN erp_document_id",
    "ALTER TABLE journals DROP COLUMN posted_version",
    "ALTER TABLE journals DROP COLUMN posted_at",
    "ALTER TABLE journals DROP COLUMN posting_run_id",
    "ALTER TABLE journals DROP CONSTRAINT journals_status_check",
    _status_check(OLD_STATUSES),
    "DROP TABLE IF EXISTS posting_runs",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
