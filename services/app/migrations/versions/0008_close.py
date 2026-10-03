"""The period close in the control plane (Stage 8b, ADR-0020).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-02

- **Kinds.** ``close_checklist`` and ``flux_rules`` join the configuration kinds.
- **A soft close.** A period can also be ``soft_closed`` (``open -> soft_closed -> closed -> locked``), with who and
  when. A soft-closed period still takes manual entries and refuses automated ones.
- **Checklists.** ``close_checklists`` records, per entity and period, the checklist version its tasks came from
  and the calendar their due dates used. ``close_tasks`` holds the tasks, frozen when generated, and their state.
  ``close_task_events`` is their insert-only history.
- **Flux.** ``flux_explanations`` keeps every explanation with the amounts it explained, and ``flux_reviews`` every
  review with the hash of the lines it signed off. Both are insert-only.
- **Task notifications.** ``notifications`` takes ``task_due`` and ``task_escalation``, with the task they are
  about.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
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
    "close_checklist",
    "flux_rules",
)
OLD_KINDS = KINDS[:12]
ENTITY = "'^[A-Z0-9]{2,16}$'"
PERIOD = "'^[0-9]{4}-(0[1-9]|1[0-2])$'"
HASH = "'^[0-9a-f]{64}$'"
CHECKS = "('journals_settled', 'suspense_clear', 'gl_balanced', 'erp_ties', 'flux_reviewed')"
NOTICES = ("reminder", "escalation", "recurring_skipped", "task_due", "task_escalation")
OLD_NOTICES = NOTICES[:3]


def _kinds(kinds: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{k}'" for k in kinds)
    return f"ALTER TABLE config_versions ADD CONSTRAINT config_versions_kind_check CHECK (kind IN ({listed}))"


def _notices(kinds: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{k}'" for k in kinds)
    return f"ALTER TABLE notifications ADD CONSTRAINT notifications_kind_check CHECK (kind IN ({listed}))"


UPGRADE = [
    "ALTER TABLE config_versions DROP CONSTRAINT config_versions_kind_check",
    _kinds(KINDS),
    "ALTER TABLE accounting_periods DROP CONSTRAINT accounting_periods_status_check",
    "ALTER TABLE accounting_periods ADD CONSTRAINT accounting_periods_status_check "
    "CHECK (status IN ('open', 'soft_closed', 'closed', 'locked'))",
    "ALTER TABLE accounting_periods ADD COLUMN soft_closed_by text",
    "ALTER TABLE accounting_periods ADD COLUMN soft_closed_at timestamptz",
    "ALTER TABLE accounting_periods DROP CONSTRAINT accounting_periods_check1",
    "ALTER TABLE accounting_periods ADD CONSTRAINT accounting_periods_closed_check "
    "CHECK (status IN ('open', 'soft_closed') OR closed_at IS NOT NULL)",
    "ALTER TABLE accounting_periods ADD CONSTRAINT accounting_periods_soft_closed_check "
    "CHECK (status <> 'soft_closed' OR soft_closed_at IS NOT NULL)",
    f"""
    CREATE TABLE close_checklists (
      entity_id           text NOT NULL CHECK (entity_id ~ {ENTITY}),
      period              text NOT NULL CHECK (period ~ {PERIOD}),
      checklist_id        text NOT NULL,
      checklist_version   integer NOT NULL CHECK (checklist_version >= 1),
      calendar_code       text NOT NULL CHECK (length(calendar_code) BETWEEN 1 AND 32),
      calendar_version    bigint,
      escalate_after_days integer NOT NULL CHECK (escalate_after_days BETWEEN 1 AND 30),
      generated_by        text NOT NULL,
      generated_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
      PRIMARY KEY (entity_id, period),
      FOREIGN KEY (entity_id, period) REFERENCES accounting_periods (entity_id, period)
    )""",
    "REVOKE UPDATE, DELETE, TRUNCATE ON close_checklists FROM close_app",
    f"""
    CREATE TABLE close_tasks (
      entity_id    text NOT NULL,
      period       text NOT NULL,
      task_id      text NOT NULL CHECK (task_id ~ '^[a-z][a-z0-9_]{{0,40}}$'),
      position     integer NOT NULL CHECK (position >= 1),
      name         text NOT NULL,
      instructions text NOT NULL DEFAULT '',
      due_day      integer NOT NULL CHECK (due_day <> 0),
      due_on       date NOT NULL,
      depends_on   text[] NOT NULL DEFAULT '{{}}',
      preparer     text,
      reviewer     text,
      check_name   text CHECK (check_name IN {CHECKS}),
      status       text NOT NULL CHECK (status IN ('open', 'prepared', 'done')),
      prepared_by  text,
      prepared_at  timestamptz,
      note         text CHECK (length(note) <= 4000),
      reviewed_by  text,
      reviewed_at  timestamptz,
      checked_at   timestamptz,
      evidence     jsonb,
      row_version  integer NOT NULL DEFAULT 1 CHECK (row_version >= 1),
      PRIMARY KEY (entity_id, period, task_id),
      UNIQUE (entity_id, period, position),
      FOREIGN KEY (entity_id, period) REFERENCES close_checklists (entity_id, period),
      CHECK ((preparer IS NULL) <> (check_name IS NULL)),
      CHECK (reviewer IS NULL OR reviewer IS DISTINCT FROM preparer),
      CHECK (status = 'open' OR prepared_at IS NOT NULL),
      CHECK (status <> 'done' OR reviewer IS NULL OR reviewed_at IS NOT NULL),
      CHECK (reviewed_by IS NULL OR reviewed_by IS DISTINCT FROM prepared_by)
    )""",
    "REVOKE DELETE, TRUNCATE ON close_tasks FROM close_app",
    """
    CREATE TABLE close_task_events (
      event_id  uuid PRIMARY KEY,
      entity_id text NOT NULL,
      period    text NOT NULL,
      task_id   text NOT NULL,
      action    text NOT NULL CHECK (action IN ('generate', 'prepare', 'check', 'review', 'reject', 'reopen',
                                               'reassign')),
      actor     text NOT NULL,
      at        timestamptz NOT NULL DEFAULT clock_timestamp(),
      note      text,
      detail    jsonb,
      FOREIGN KEY (entity_id, period, task_id) REFERENCES close_tasks (entity_id, period, task_id)
    )""",
    "CREATE INDEX close_task_events_by_task ON close_task_events (entity_id, period, task_id, at)",
    "REVOKE UPDATE, DELETE, TRUNCATE ON close_task_events FROM close_app",
    """
    CREATE TABLE flux_explanations (
      explanation_id   uuid PRIMARY KEY,
      entity_id        text NOT NULL,
      period           text NOT NULL,
      account          text NOT NULL CHECK (length(account) BETWEEN 1 AND 32),
      measure          text NOT NULL CHECK (measure IN ('closing', 'activity')),
      current_minor    bigint NOT NULL,
      comparison_minor bigint NOT NULL,
      explanation      text NOT NULL CHECK (length(explanation) BETWEEN 1 AND 4000),
      explained_by     text NOT NULL,
      explained_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
      FOREIGN KEY (entity_id, period) REFERENCES accounting_periods (entity_id, period)
    )""",
    "CREATE INDEX flux_explanations_latest ON flux_explanations (entity_id, period, account, explained_at DESC)",
    "REVOKE UPDATE, DELETE, TRUNCATE ON flux_explanations FROM close_app",
    f"""
    CREATE TABLE flux_reviews (
      review_id     uuid PRIMARY KEY,
      entity_id     text NOT NULL,
      period        text NOT NULL,
      flux_hash     text NOT NULL CHECK (flux_hash ~ {HASH}),
      lines         integer NOT NULL CHECK (lines >= 0),
      explained     integer NOT NULL CHECK (explained >= 0),
      rules_id      text NOT NULL,
      rules_version integer NOT NULL CHECK (rules_version >= 1),
      reviewed_by   text NOT NULL,
      reviewed_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
      FOREIGN KEY (entity_id, period) REFERENCES accounting_periods (entity_id, period)
    )""",
    "CREATE INDEX flux_reviews_latest ON flux_reviews (entity_id, period, reviewed_at DESC)",
    "REVOKE UPDATE, DELETE, TRUNCATE ON flux_reviews FROM close_app",
    "ALTER TABLE notifications DROP CONSTRAINT notifications_kind_check",
    _notices(NOTICES),
    "ALTER TABLE notifications ADD COLUMN task_ref text CHECK (length(task_ref) <= 80)",
]

DOWNGRADE: list[str | sa.TextClause] = [
    """
    DO $$
    BEGIN
      IF EXISTS (SELECT FROM accounting_periods WHERE status = 'soft_closed') THEN
        RAISE EXCEPTION 'soft-closed periods exist; 0007 has no such status';
      END IF;
    END
    $$""",
    sa.text("DELETE FROM notifications WHERE NOT (kind = ANY (:kinds))").bindparams(kinds=list(OLD_NOTICES)),
    "ALTER TABLE notifications DROP COLUMN task_ref",
    "ALTER TABLE notifications DROP CONSTRAINT notifications_kind_check",
    _notices(OLD_NOTICES),
    "DROP TABLE IF EXISTS flux_reviews",
    "DROP TABLE IF EXISTS flux_explanations",
    "DROP TABLE IF EXISTS close_task_events",
    "DROP TABLE IF EXISTS close_tasks",
    "DROP TABLE IF EXISTS close_checklists",
    "ALTER TABLE accounting_periods DROP CONSTRAINT accounting_periods_soft_closed_check",
    "ALTER TABLE accounting_periods DROP CONSTRAINT accounting_periods_closed_check",
    "ALTER TABLE accounting_periods ADD CONSTRAINT accounting_periods_check1 "
    "CHECK (status = 'open' OR closed_at IS NOT NULL)",
    "ALTER TABLE accounting_periods DROP COLUMN soft_closed_at",
    "ALTER TABLE accounting_periods DROP COLUMN soft_closed_by",
    "ALTER TABLE accounting_periods DROP CONSTRAINT accounting_periods_status_check",
    "ALTER TABLE accounting_periods ADD CONSTRAINT accounting_periods_status_check "
    "CHECK (status IN ('open', 'closed', 'locked'))",
    sa.text("DELETE FROM config_versions WHERE NOT (kind = ANY (:kinds))").bindparams(kinds=list(OLD_KINDS)),
    "ALTER TABLE config_versions DROP CONSTRAINT config_versions_kind_check",
    _kinds(OLD_KINDS),
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
