"""Writer leases: exactly one writer per resource (a match set, an entity ledger, the smoke table).

This replaces the single-writer guarantee Temporal workflow IDs used to give us (ADR-0006, Stage 1
design rule 1):

* One ``leases`` row per resource, never deleted. The holder is a *run attempt*.
* Every grant or re-grant increments ``fencing_token``. Jobs carry the token and check it before and
  after they commit (``close_engine.io.fence``), so a stale writer is detected.
* A lease is released only in the same transaction that records its attempt as terminal, and never
  during a post-failure quarantine. There is no time-based expiry, and an unknown job status never
  releases a lease.
* ``force_release`` is the audited break-glass path. It bumps the token, so any straggler is fenced.
* Every timestamp comparison uses Postgres ``now()``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy import Connection, text

RESOURCE_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}:[A-Za-z0-9_.-]{1,96}$")


@dataclass(frozen=True)
class Grant:
    resource: str
    run_id: UUID
    attempt: int
    fencing_token: int


class LeaseBusyError(RuntimeError):
    def __init__(self, resource: str, holder: Grant | None) -> None:
        self.resource = resource
        self.holder = holder
        super().__init__(f"lease {resource!r} is held by {holder}")


class LeaseStateError(RuntimeError):
    """The lease is not in the state the caller assumed (not the holder, attempt not terminal, ...)."""


def validate_resource(resource: str) -> str:
    if not RESOURCE_RE.fullmatch(resource):
        raise ValueError(f"invalid lease resource {resource!r}")
    return resource


class LeaseManager:
    def __init__(self, actor: str) -> None:
        self.actor = actor

    def _event(
        self,
        tx: Connection,
        *,
        resource: str,
        event: str,
        token: int,
        run_id: UUID | None,
        attempt: int | None,
        reason: str | None,
        actor: str | None = None,
    ) -> None:
        tx.execute(
            text(
                "INSERT INTO lease_events (resource, event, fencing_token, run_id, attempt, actor, reason) "
                "VALUES (:r, :e, :t, :run, :a, :actor, :reason)"
            ),
            {
                "r": resource,
                "e": event,
                "t": token,
                "run": run_id,
                "a": attempt,
                "actor": actor or self.actor,
                "reason": reason,
            },
        )

    def holder(self, conn: Connection, resource: str) -> Grant | None:
        row = conn.execute(
            text("SELECT holder_run_id, holder_attempt, fencing_token FROM leases WHERE resource = :r"),
            {"r": resource},
        ).first()
        if row is None or row.holder_run_id is None:
            return None
        return Grant(resource, row.holder_run_id, int(row.holder_attempt), int(row.fencing_token))

    def current_token(self, conn: Connection, resource: str) -> int:
        token = conn.execute(text("SELECT fencing_token FROM leases WHERE resource = :r"), {"r": resource}).scalar()
        return int(token or 0)

    def acquire(self, tx: Connection, resource: str, run_id: UUID, *, attempt: int = 1) -> Grant:
        """Grant the lease to (run_id, attempt) if nobody holds it; otherwise raise LeaseBusyError.

        Concurrent callers serialize on the row lock taken by ``ON CONFLICT DO UPDATE``; the loser
        re-evaluates the ``WHERE`` against the committed holder and gets no row back.
        """
        validate_resource(resource)
        row = tx.execute(
            text(
                """
                INSERT INTO leases AS l (resource, fencing_token, holder_run_id, holder_attempt, acquired_at)
                VALUES (:r, 1, :run, :a, now())
                ON CONFLICT (resource) DO UPDATE
                   SET fencing_token = l.fencing_token + 1, holder_run_id = EXCLUDED.holder_run_id,
                       holder_attempt = EXCLUDED.holder_attempt, acquired_at = now(),
                       released_at = NULL, release_reason = NULL
                 WHERE l.holder_run_id IS NULL
                RETURNING fencing_token
                """
            ),
            {"r": resource, "run": run_id, "a": attempt},
        ).first()
        if row is None:
            raise LeaseBusyError(resource, self.holder(tx, resource))
        grant = Grant(resource, run_id, attempt, int(row.fencing_token))
        self._event(
            tx,
            resource=resource,
            event="granted",
            token=grant.fencing_token,
            run_id=run_id,
            attempt=attempt,
            reason=None,
        )
        return grant

    def regrant(self, tx: Connection, grant: Grant, *, next_attempt: int) -> Grant:
        """Move a held lease to the next attempt of the same run, with a new fencing token."""
        if next_attempt <= grant.attempt:
            raise LeaseStateError("the next attempt must be greater than the current one")
        row = tx.execute(
            text(
                "UPDATE leases SET fencing_token = fencing_token + 1, holder_attempt = :next, acquired_at = now() "
                "WHERE resource = :r AND holder_run_id = :run AND holder_attempt = :cur RETURNING fencing_token"
            ),
            {"next": next_attempt, "r": grant.resource, "run": grant.run_id, "cur": grant.attempt},
        ).first()
        if row is None:
            raise LeaseStateError(f"{grant} is not the current holder")
        new = Grant(grant.resource, grant.run_id, next_attempt, int(row.fencing_token))
        self._event(
            tx,
            resource=grant.resource,
            event="regranted",
            token=new.fencing_token,
            run_id=grant.run_id,
            attempt=next_attempt,
            reason=None,
        )
        return new

    def release(self, tx: Connection, grant: Grant, *, reason: str) -> None:
        """Release the lease. The holder attempt must already be terminal (and out of quarantine)."""
        attempt = tx.execute(
            text(
                "SELECT state, quarantine_until IS NOT NULL AND quarantine_until > now() AS quarantined "
                "FROM run_attempts WHERE run_id = :run AND attempt = :a FOR UPDATE"
            ),
            {"run": grant.run_id, "a": grant.attempt},
        ).first()
        if attempt is None or attempt.state not in {"terminated", "submit_failed"}:
            raise LeaseStateError(f"attempt {grant.run_id}/{grant.attempt} is not terminal")
        if attempt.quarantined:
            raise LeaseStateError(f"attempt {grant.run_id}/{grant.attempt} is still in quarantine")
        row = tx.execute(
            text(
                "UPDATE leases SET holder_run_id = NULL, holder_attempt = NULL, released_at = now(), "
                "release_reason = :reason WHERE resource = :r AND holder_run_id = :run AND holder_attempt = :a "
                "RETURNING fencing_token"
            ),
            {"reason": reason, "r": grant.resource, "run": grant.run_id, "a": grant.attempt},
        ).first()
        if row is None:
            raise LeaseStateError(f"{grant} is not the current holder")
        self._event(
            tx,
            resource=grant.resource,
            event="released",
            token=int(row.fencing_token),
            run_id=grant.run_id,
            attempt=grant.attempt,
            reason=reason,
        )

    def force_release(self, tx: Connection, resource: str, *, actor: str, reason: str) -> int:
        """Break-glass release (audited). Bumps the token so any in-flight writer is fenced."""
        if not reason.strip():
            raise ValueError("a force release needs a reason")
        row = tx.execute(
            text(
                "UPDATE leases SET fencing_token = fencing_token + 1, holder_run_id = NULL, holder_attempt = NULL, "
                "released_at = now(), release_reason = :reason WHERE resource = :r RETURNING fencing_token"
            ),
            {"r": validate_resource(resource), "reason": f"force: {reason}"},
        ).first()
        if row is None:
            raise LeaseStateError(f"lease {resource!r} does not exist")
        token = int(row.fencing_token)
        self._event(
            tx,
            resource=resource,
            event="force_released",
            token=token,
            run_id=None,
            attempt=None,
            reason=reason,
            actor=actor,
        )
        return token
