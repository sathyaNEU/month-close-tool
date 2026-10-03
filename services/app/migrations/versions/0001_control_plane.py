"""Control-plane schema: runs, attempts, writer leases, outbox, idempotency keys, scheduler claims.

Revision ID: 0001
Revises:
Create Date: 2026-09-27

All objects live in schema ``close`` (owned by close_migrator). close_app receives row privileges
through default privileges, except that lease_events and alembic_version are read/insert-only for it,
so the lease audit trail cannot be edited by the application role.
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UPGRADE = [
    """
    CREATE TABLE principal_entities (
      user_id    text NOT NULL,
      entity_id  text NOT NULL,
      granted_by text NOT NULL,
      granted_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (user_id, entity_id)
    )""",
    """
    CREATE TABLE runs (
      run_id          uuid PRIMARY KEY,
      job_key         text NOT NULL,
      lease_resource  text NOT NULL,
      requested_by    text NOT NULL,
      request_id      text,
      params          jsonb NOT NULL,
      state           text NOT NULL CHECK (state IN ('requested','submitted','running','cancel_requested',
                                                     'succeeded','failed','cancelled','submit_failed')),
      current_attempt smallint NOT NULL DEFAULT 1,
      result          jsonb,
      error           text,
      state_version   integer NOT NULL DEFAULT 0,
      created_at      timestamptz NOT NULL DEFAULT now(),
      updated_at      timestamptz NOT NULL DEFAULT now(),
      terminal_at     timestamptz
    )""",
    "CREATE INDEX runs_active ON runs (updated_at) "
    "WHERE state IN ('requested','submitted','running','cancel_requested')",
    """
    CREATE TABLE run_attempts (
      run_id             uuid NOT NULL REFERENCES runs,
      attempt            smallint NOT NULL CHECK (attempt BETWEEN 1 AND 99),
      fencing_token      bigint NOT NULL CHECK (fencing_token > 0),
      idempotency_token  text NOT NULL UNIQUE CHECK (length(idempotency_token) <= 64),
      external_run_id    bigint UNIQUE,
      state              text NOT NULL CHECK (state IN ('pending','submitted','running','terminated','submit_failed')),
      last_status        jsonb,
      first_submitted_at timestamptz,
      terminal_at        timestamptz,
      quarantine_until   timestamptz,
      PRIMARY KEY (run_id, attempt)
    )""",
    """
    CREATE TABLE leases (
      resource        text PRIMARY KEY CHECK (resource ~ '^[a-z][a-z0-9_]{0,31}:[A-Za-z0-9_.-]{1,96}$'),
      fencing_token   bigint NOT NULL DEFAULT 0,
      holder_run_id   uuid REFERENCES runs,
      holder_attempt  smallint,
      acquired_at     timestamptz,
      released_at     timestamptz,
      release_reason  text,
      CHECK ((holder_run_id IS NULL) = (holder_attempt IS NULL))
    )""",
    """
    CREATE TABLE lease_events (
      id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      resource      text NOT NULL,
      event         text NOT NULL CHECK (event IN ('granted','regranted','released','force_released')),
      fencing_token bigint NOT NULL,
      run_id        uuid,
      attempt       smallint,
      actor         text NOT NULL,
      reason        text,
      at            timestamptz NOT NULL DEFAULT now()
    )""",
    """
    CREATE TABLE outbox (
      id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      event_id         uuid NOT NULL UNIQUE,
      topic            text NOT NULL,
      aggregate_id     text NOT NULL,
      payload          jsonb NOT NULL,
      payload_version  smallint NOT NULL DEFAULT 1,
      created_at       timestamptz NOT NULL DEFAULT now(),
      available_at     timestamptz NOT NULL DEFAULT now(),
      attempts         integer NOT NULL DEFAULT 0,
      claimed_by       text,
      claim_expires_at timestamptz,
      delivered_at     timestamptz,
      dead_at          timestamptz,
      last_error       text
    )""",
    "CREATE INDEX outbox_ready ON outbox (aggregate_id, id) WHERE delivered_at IS NULL AND dead_at IS NULL",
    """
    CREATE TABLE outbox_receipts (
      consumer   text NOT NULL,
      event_id   uuid NOT NULL,
      applied_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (consumer, event_id)
    )""",
    """
    CREATE TABLE idempotency_keys (
      principal       text NOT NULL,
      method          text NOT NULL,
      route           text NOT NULL,
      key             text NOT NULL CHECK (length(key) BETWEEN 8 AND 128),
      request_sha256  bytea NOT NULL,
      response_status smallint NOT NULL,
      response_body   jsonb NOT NULL,
      resource_ref    text,
      created_at      timestamptz NOT NULL DEFAULT now(),
      expires_at      timestamptz NOT NULL DEFAULT now() + interval '24 hours',
      PRIMARY KEY (principal, method, route, key)
    )""",
    """
    CREATE TABLE scheduler_tasks (
      task             text PRIMARY KEY,
      interval_s       integer NOT NULL CHECK (interval_s > 0),
      next_due_at      timestamptz NOT NULL DEFAULT now(),
      claim_seq        bigint NOT NULL DEFAULT 0,
      last_claimed_by  text,
      last_claimed_at  timestamptz,
      last_finished_at timestamptz,
      last_error       text
    )""",
    # The application role may read and append to the lease audit trail, never change it.
    "REVOKE UPDATE, DELETE, TRUNCATE ON lease_events FROM close_app",
    "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON alembic_version FROM close_app",
]

DOWNGRADE = [
    "DROP TABLE IF EXISTS scheduler_tasks",
    "DROP TABLE IF EXISTS idempotency_keys",
    "DROP TABLE IF EXISTS outbox_receipts",
    "DROP TABLE IF EXISTS outbox",
    "DROP TABLE IF EXISTS lease_events",
    "DROP TABLE IF EXISTS leases",
    "DROP TABLE IF EXISTS run_attempts",
    "DROP TABLE IF EXISTS runs",
    "DROP TABLE IF EXISTS principal_entities",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
