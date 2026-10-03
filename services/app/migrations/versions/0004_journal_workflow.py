"""The journal workflow (Stage 7a, ADR-0016): periods, manual journals and their approvals, batches, delegations,
attachments, imports and notifications, plus four configuration kinds (entities, journal templates, the approval
matrix, recurring journals).

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-30

- **Nothing is deleted.** The app role has no ``DELETE`` or ``TRUNCATE`` on any table here. A journal ends as
  ``cancelled``. Editing a draft adds a new revision of its lines, and the old revisions stay.
- **Append-only records.** Lines, approvals, the journal history and imports can't be updated either.
- **Gapless numbers.** ``journal_counters`` gives each entity's journals consecutive numbers, updated in the
  journal's own transaction (a sequence would leave gaps).
- **The kind list** must equal ``close_contracts.config.envelope.ConfigKind`` (a test compares them).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

KINDS = (
    "data_source",
    "match_set",
    "accounting_policy",
    "event_type",
    "auto_journal_definition",
    "account_mapping",
    "chart_of_accounts",
    "dimension_set",
    "entity_set",
    "journal_template",
    "approval_matrix",
    "recurring_journal",
)
OLD_KINDS = KINDS[:8]
ENTITY = "'^[A-Z0-9]{2,16}$'"
PERIOD = "'^[0-9]{4}-(0[1-9]|1[0-2])$'"
HASH = "'^[0-9a-f]{64}$'"
TABLES = (
    "accounting_periods",
    "journal_counters",
    "journal_batches",
    "journal_imports",
    "journals",
    "journal_lines",
    "journal_approvals",
    "journal_history",
    "delegations",
    "attachments",
    "notifications",
)
APPEND_ONLY = ("journal_lines", "journal_approvals", "journal_history", "journal_imports")


def _check(kinds: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{k}'" for k in kinds)
    return f"ALTER TABLE config_versions ADD CONSTRAINT config_versions_kind_check CHECK (kind IN ({listed}))"


UPGRADE = [
    "ALTER TABLE config_versions DROP CONSTRAINT config_versions_kind_check",
    _check(KINDS),
    f"""
    CREATE TABLE accounting_periods (
      entity_id     text NOT NULL CHECK (entity_id ~ {ENTITY}),
      period        text NOT NULL CHECK (period ~ {PERIOD}),
      starts_on     date NOT NULL,
      ends_on       date NOT NULL,
      status        text NOT NULL CHECK (status IN ('open', 'closed', 'locked')),
      opened_by     text NOT NULL,
      opened_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
      closed_by     text,
      closed_at     timestamptz,
      locked_by     text,
      locked_at     timestamptz,
      reopened_by   text,
      reopened_at   timestamptz,
      reopen_reason text,
      row_version   integer NOT NULL DEFAULT 1 CHECK (row_version >= 1),
      PRIMARY KEY (entity_id, period),
      CHECK (ends_on >= starts_on),
      CHECK (status = 'open' OR closed_at IS NOT NULL),
      CHECK (status <> 'locked' OR locked_at IS NOT NULL)
    )""",
    f"""
    CREATE TABLE journal_counters (
      entity_id   text PRIMARY KEY CHECK (entity_id ~ {ENTITY}),
      last_number bigint NOT NULL CHECK (last_number >= 0)
    )""",
    f"""
    CREATE TABLE journal_batches (
      batch_id        uuid PRIMARY KEY,
      entity_id       text NOT NULL CHECK (entity_id ~ {ENTITY}),
      description     text NOT NULL,
      status          text NOT NULL CHECK (status IN ('draft', 'pending_approval', 'approved', 'cancelled')),
      created_by      text NOT NULL,
      created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
      submission      integer NOT NULL DEFAULT 0,
      submitted_by    text,
      submitted_at    timestamptz,
      required_levels integer CHECK (required_levels BETWEEN 1 AND 3),
      current_level   integer CHECK (current_level BETWEEN 1 AND 3),
      level_since     timestamptz,
      approved_at     timestamptz,
      cancelled_by    text,
      cancelled_at    timestamptz,
      row_version     integer NOT NULL DEFAULT 1 CHECK (row_version >= 1)
    )""",
    f"""
    CREATE TABLE journal_imports (
      import_id     uuid PRIMARY KEY,
      entity_id     text NOT NULL CHECK (entity_id ~ {ENTITY}),
      sha256        text NOT NULL CHECK (sha256 ~ {HASH}),
      row_count     integer NOT NULL CHECK (row_count >= 0),
      journal_count integer NOT NULL CHECK (journal_count >= 0),
      status        text NOT NULL CHECK (status IN ('created', 'rejected')),
      issues        jsonb NOT NULL,
      imported_by   text NOT NULL,
      imported_at   timestamptz NOT NULL DEFAULT clock_timestamp()
    )""",
    f"""
    CREATE TABLE journals (
      journal_id          uuid PRIMARY KEY,
      entity_id           text NOT NULL CHECK (entity_id ~ {ENTITY}),
      number              bigint NOT NULL CHECK (number >= 1),
      template_id         text NOT NULL,
      template_version    integer NOT NULL CHECK (template_version >= 1),
      period              text NOT NULL CHECK (period ~ {PERIOD}),
      accounting_date     date NOT NULL,
      currency            text NOT NULL CHECK (currency ~ '^[A-Z]{{3}}$'),
      description         text NOT NULL,
      header              jsonb NOT NULL,
      status              text NOT NULL CHECK (status IN ('draft', 'pending_approval', 'approved', 'cancelled')),
      source              text NOT NULL CHECK (source IN ('manual', 'import', 'recurring', 'reversal')),
      auto_reverse        boolean NOT NULL,
      revision            integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
      content_hash        text NOT NULL CHECK (content_hash ~ {HASH}),
      total_debit_minor   bigint NOT NULL CHECK (total_debit_minor >= 0),
      total_credit_minor  bigint NOT NULL CHECK (total_credit_minor >= 0),
      line_count          integer NOT NULL CHECK (line_count >= 0),
      batch_id            uuid REFERENCES journal_batches (batch_id),
      import_id           uuid REFERENCES journal_imports (import_id),
      recurring_id        text,
      recurring_period    text CHECK (recurring_period ~ {PERIOD}),
      reverses_journal_id uuid REFERENCES journals (journal_id),
      created_by          text NOT NULL,
      created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
      submission          integer NOT NULL DEFAULT 0 CHECK (submission >= 0),
      submitted_by        text,
      submitted_at        timestamptz,
      matrix_id           text,
      matrix_version      integer,
      routing             jsonb,
      risk_score          integer,
      risk_version        integer,
      risk_factors        jsonb,
      required_levels     integer CHECK (required_levels BETWEEN 1 AND 3),
      current_level       integer CHECK (current_level BETWEEN 1 AND 3),
      level_since         timestamptz,
      approved_at         timestamptz,
      bypass_reason       text,
      cancelled_by        text,
      cancelled_at        timestamptz,
      row_version         integer NOT NULL DEFAULT 1 CHECK (row_version >= 1),
      UNIQUE (entity_id, number),
      CHECK (status = 'draft' OR status = 'cancelled' OR total_debit_minor = total_credit_minor),
      CHECK (status <> 'approved' OR approved_at IS NOT NULL),
      CHECK (status <> 'pending_approval' OR (required_levels IS NOT NULL AND current_level IS NOT NULL)),
      CHECK (status <> 'cancelled' OR cancelled_at IS NOT NULL),
      CHECK ((recurring_id IS NULL) = (recurring_period IS NULL)),
      CHECK (source <> 'reversal' OR (reverses_journal_id IS NOT NULL AND NOT auto_reverse))
    )""",
    "CREATE UNIQUE INDEX journals_once_per_recurring_period ON journals (recurring_id, recurring_period) "
    "WHERE recurring_id IS NOT NULL",
    "CREATE UNIQUE INDEX journals_one_live_reversal ON journals (reverses_journal_id) "
    "WHERE reverses_journal_id IS NOT NULL AND status <> 'cancelled'",
    "CREATE INDEX journals_by_entity_status ON journals (entity_id, status)",
    "CREATE INDEX journals_by_batch ON journals (batch_id) WHERE batch_id IS NOT NULL",
    """
    CREATE TABLE journal_lines (
      journal_id    uuid NOT NULL REFERENCES journals (journal_id),
      revision      integer NOT NULL CHECK (revision >= 1),
      line_no       integer NOT NULL CHECK (line_no >= 1),
      account       text NOT NULL,
      side          text NOT NULL CHECK (side IN ('DR', 'CR')),
      amount_minor  bigint NOT NULL CHECK (amount_minor > 0),
      dimensions    jsonb NOT NULL,
      description   text NOT NULL,
      column_values jsonb NOT NULL,
      PRIMARY KEY (journal_id, revision, line_no)
    )""",
    "CREATE INDEX journal_lines_by_account ON journal_lines (account)",
    f"""
    CREATE TABLE journal_approvals (
      journal_id   uuid NOT NULL REFERENCES journals (journal_id),
      submission   integer NOT NULL CHECK (submission >= 1),
      level        integer NOT NULL CHECK (level BETWEEN 1 AND 3),
      approver     text NOT NULL,
      on_behalf_of text,
      content_hash text NOT NULL CHECK (content_hash ~ {HASH}),
      batch_id     uuid REFERENCES journal_batches (batch_id),
      approved_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
      PRIMARY KEY (journal_id, submission, level),
      UNIQUE (journal_id, submission, approver),
      CHECK (on_behalf_of IS NULL OR on_behalf_of <> approver)
    )""",
    """
    CREATE TABLE journal_history (
      history_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      journal_id   uuid REFERENCES journals (journal_id),
      batch_id     uuid REFERENCES journal_batches (batch_id),
      action       text NOT NULL,
      from_status  text,
      to_status    text,
      submission   integer NOT NULL,
      level        integer,
      actor        text NOT NULL,
      on_behalf_of text,
      reason       text,
      at           timestamptz NOT NULL DEFAULT clock_timestamp(),
      CHECK (journal_id IS NOT NULL OR batch_id IS NOT NULL)
    )""",
    "CREATE INDEX journal_history_by_journal ON journal_history (journal_id)",
    """
    CREATE TABLE delegations (
      delegation_id uuid PRIMARY KEY,
      delegator     text NOT NULL,
      delegate      text NOT NULL,
      entity_ids    text[] NOT NULL CHECK (cardinality(entity_ids) >= 1),
      starts_on     date NOT NULL,
      ends_on       date NOT NULL,
      created_by    text NOT NULL,
      created_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
      revoked_by    text,
      revoked_at    timestamptz,
      CHECK (delegator <> delegate),
      CHECK (ends_on >= starts_on)
    )""",
    f"""
    CREATE TABLE attachments (
      attachment_id uuid PRIMARY KEY,
      journal_id    uuid NOT NULL REFERENCES journals (journal_id),
      file_name     text NOT NULL CHECK (length(file_name) BETWEEN 1 AND 255),
      content_type  text NOT NULL,
      size_bytes    integer NOT NULL CHECK (size_bytes BETWEEN 1 AND 10485760),
      sha256        text NOT NULL CHECK (sha256 ~ {HASH}),
      content       bytea NOT NULL,
      uploaded_by   text NOT NULL,
      uploaded_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
      removed_by    text,
      removed_at    timestamptz,
      CHECK (octet_length(content) = size_bytes)
    )""",
    """
    CREATE TABLE notifications (
      notification_id uuid PRIMARY KEY,
      user_id         text NOT NULL,
      kind            text NOT NULL CHECK (kind IN ('reminder', 'escalation', 'recurring_skipped')),
      dedup_key       text NOT NULL UNIQUE,
      entity_id       text NOT NULL,
      journal_id      uuid REFERENCES journals (journal_id),
      batch_id        uuid REFERENCES journal_batches (batch_id),
      message         text NOT NULL,
      created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
      read_at         timestamptz
    )""",
    "CREATE INDEX notifications_by_user ON notifications (user_id, created_at)",
    *(f"REVOKE DELETE, TRUNCATE ON {t} FROM close_app" for t in TABLES),
    *(f"REVOKE UPDATE ON {t} FROM close_app" for t in APPEND_ONLY),
]

DOWNGRADE: list[str | sa.TextClause] = [
    *(f"DROP TABLE IF EXISTS {t}" for t in reversed(TABLES)),
    sa.text("DELETE FROM config_versions WHERE NOT (kind = ANY (:kinds))").bindparams(kinds=list(OLD_KINDS)),
    "ALTER TABLE config_versions DROP CONSTRAINT config_versions_kind_check",
    _check(OLD_KINDS),
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
