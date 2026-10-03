"""Journaling configuration kinds: event types, automated journal definitions, account mappings, the chart of
accounts and dimension sets join the maker-checker configuration versions (Stage 6, ADR-0015).

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-29

The kind list must equal ``close_contracts.config.envelope.ConfigKind`` (a test compares them).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
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
)
OLD_KINDS = KINDS[:3]


def _check(kinds: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{k}'" for k in kinds)
    return f"ALTER TABLE config_versions ADD CONSTRAINT config_versions_kind_check CHECK (kind IN ({listed}))"


def upgrade() -> None:
    op.execute("ALTER TABLE config_versions DROP CONSTRAINT config_versions_kind_check")
    op.execute(_check(KINDS))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM config_versions WHERE NOT (kind = ANY (:kinds))").bindparams(kinds=list(OLD_KINDS)))
    op.execute("ALTER TABLE config_versions DROP CONSTRAINT config_versions_kind_check")
    op.execute(_check(OLD_KINDS))
