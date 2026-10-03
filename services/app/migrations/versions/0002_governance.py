"""Governance kernel: role assignments, the hash-chained audit log, configuration versions.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28

- ``role_assignments`` replaces the Stage 1 entity grants. Each grant becomes a ``match_preparer`` assignment
  in its entity, which keeps what those users could do (start and read smoke runs), and
  ``principal_entities`` becomes a view of the entities a principal holds any role in. Deny-by-default holds:
  no assignment, no entity. Revoking sets ``revoked_*``; the application role cannot delete assignments.
- ``audit_events`` is append-only for the application role, and ``audit_chain_head`` holds the position and
  hash of the last event, which every append locks and advances.
- ``config_versions`` holds maker-checker configuration versions; at most one version of a configuration is
  active, and the application role cannot delete one.

The role catalog in the CHECK constraint must equal ``close_app.authz.Role`` (a test compares them).
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

ROLES = (
    "platform_admin",
    "config_author",
    "config_approver",
    "match_preparer",
    "match_approver",
    "journal_preparer",
    "journal_approver_l1",
    "journal_approver_l2",
    "journal_approver_l3",
    "controller",
    "auditor",
    "integration_client",
)
_ROLE_LIST = ", ".join(f"'{r}'" for r in ROLES)

UPGRADE = [
    f"""
    CREATE TABLE role_assignments (
      assignment_id uuid PRIMARY KEY,
      user_id       text NOT NULL CHECK (length(user_id) BETWEEN 1 AND 255),
      role          text NOT NULL CHECK (role IN ({_ROLE_LIST})),
      scope         text NOT NULL CHECK (scope = 'GLOBAL' OR scope ~ '^[A-Z0-9]{{2,16}}$'),
      granted_by    text NOT NULL,
      granted_at    timestamptz NOT NULL DEFAULT now(),
      revoked_by    text,
      revoked_at    timestamptz,
      CHECK ((revoked_by IS NULL) = (revoked_at IS NULL))
    )""",
    "CREATE UNIQUE INDEX role_assignments_active ON role_assignments (user_id, role, scope) WHERE revoked_at IS NULL",
    """
    INSERT INTO role_assignments (assignment_id, user_id, role, scope, granted_by, granted_at)
    SELECT gen_random_uuid(), user_id, 'match_preparer', entity_id, granted_by, granted_at FROM principal_entities""",
    "DROP TABLE principal_entities",
    """
    CREATE VIEW principal_entities AS
    SELECT DISTINCT user_id, scope AS entity_id FROM role_assignments WHERE revoked_at IS NULL AND scope <> 'GLOBAL'""",
    "REVOKE DELETE, TRUNCATE ON role_assignments FROM close_app",
    "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON principal_entities FROM close_app",
    """
    CREATE TABLE audit_chain_head (
      id        smallint PRIMARY KEY CHECK (id = 1),
      last_seq  bigint NOT NULL CHECK (last_seq >= 0),
      last_hash bytea NOT NULL CHECK (length(last_hash) = 32)
    )""",
    "INSERT INTO audit_chain_head (id, last_seq, last_hash) VALUES (1, 0, decode(repeat('00', 32), 'hex'))",
    """
    CREATE TABLE audit_events (
      seq         bigint PRIMARY KEY CHECK (seq > 0),
      recorded_at timestamptz NOT NULL,
      actor       text NOT NULL,
      action      text NOT NULL,
      outcome     text NOT NULL CHECK (outcome IN ('ok', 'denied')),
      entity_ids  text[] NOT NULL,
      target      text,
      request_id  text,
      payload     jsonb NOT NULL,
      prev_hash   bytea NOT NULL CHECK (length(prev_hash) = 32),
      hash        bytea NOT NULL UNIQUE CHECK (length(hash) = 32)
    )""",
    "CREATE INDEX audit_events_entities ON audit_events USING gin (entity_ids)",
    "REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM close_app",
    "REVOKE INSERT, DELETE, TRUNCATE ON audit_chain_head FROM close_app",
    """
    CREATE TABLE config_versions (
      kind           text NOT NULL CHECK (kind IN ('data_source', 'match_set', 'accounting_policy')),
      config_id      text NOT NULL CHECK (config_id ~ '^[a-z][a-z0-9_]{2,63}$'),
      version        integer NOT NULL CHECK (version >= 1),
      status         text NOT NULL CHECK (status IN ('draft', 'submitted', 'approved', 'active', 'retired')),
      effective_from date NOT NULL,
      effective_to   date,
      spec           jsonb NOT NULL,
      content_hash   text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
      entity_ids     text[] NOT NULL,
      authors        text[] NOT NULL CHECK (cardinality(authors) >= 1),
      created_by     text NOT NULL,
      created_at     timestamptz NOT NULL DEFAULT now(),
      submitted_by   text,
      submitted_at   timestamptz,
      approved_by    text,
      approved_at    timestamptz,
      activated_by   text,
      activated_at   timestamptz,
      retired_by     text,
      retired_at     timestamptz,
      row_version    integer NOT NULL DEFAULT 1 CHECK (row_version >= 1),
      PRIMARY KEY (kind, config_id, version),
      CHECK (effective_to IS NULL OR effective_to > effective_from),
      CHECK (approved_by IS NULL OR NOT (approved_by = ANY (authors))),
      CHECK (activated_by IS NULL OR NOT (activated_by = ANY (authors)))
    )""",
    "CREATE UNIQUE INDEX config_versions_one_active ON config_versions (kind, config_id) WHERE status = 'active'",
    "REVOKE DELETE, TRUNCATE ON config_versions FROM close_app",
]

DOWNGRADE = [
    "DROP TABLE IF EXISTS config_versions",
    "DROP TABLE IF EXISTS audit_events",
    "DROP TABLE IF EXISTS audit_chain_head",
    "DROP VIEW IF EXISTS principal_entities",
    """
    CREATE TABLE principal_entities (
      user_id    text NOT NULL,
      entity_id  text NOT NULL,
      granted_by text NOT NULL,
      granted_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (user_id, entity_id)
    )""",
    """
    INSERT INTO principal_entities (user_id, entity_id, granted_by, granted_at)
    SELECT DISTINCT ON (user_id, scope) user_id, scope, granted_by, granted_at
    FROM role_assignments WHERE revoked_at IS NULL AND scope <> 'GLOBAL'
    ORDER BY user_id, scope, granted_at""",
    "DROP TABLE IF EXISTS role_assignments",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
