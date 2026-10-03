"""Authorization: roles, scopes and the actions each role may take (policy as code, Stage 4).

A principal holds role assignments, each at one scope: an entity code, or ``GLOBAL``.

- **Entity objects.** Changing an object that belongs to entities (a match set) needs the action at the scope of
  **every** one of those entities. Reading it needs the read action in **any one** of them (you decided this
  at the Stage 4 sign-off: 8 of the 12 template match sets span several entities).
- **Global objects.** An action on a global object (a data source, access administration, audit events with
  no entity) needs it at ``GLOBAL``.
- **Visibility.** ``GLOBAL`` never grants data visibility: a principal sees an entity's data only through an
  assignment in that entity.

Every ``/api/v1`` route declares what it needs with :func:`access` (``openapi_extra``), and a test fails for
any route that declares nothing, so a new route is denied until someone decides.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Any, Final

GLOBAL: Final = "GLOBAL"
ACCESS_KEY: Final = "x-close-access"


class Role(StrEnum):
    PLATFORM_ADMIN = "platform_admin"
    CONFIG_AUTHOR = "config_author"
    CONFIG_APPROVER = "config_approver"
    MATCH_PREPARER = "match_preparer"
    MATCH_APPROVER = "match_approver"
    JOURNAL_PREPARER = "journal_preparer"
    JOURNAL_APPROVER_L1 = "journal_approver_l1"
    JOURNAL_APPROVER_L2 = "journal_approver_l2"
    JOURNAL_APPROVER_L3 = "journal_approver_l3"
    CONTROLLER = "controller"
    AUDITOR = "auditor"
    INTEGRATION_CLIENT = "integration_client"


class Action(StrEnum):
    SMOKE_RUN = "smoke.run"  # the platform self-test job
    CONFIG_READ = "config.read"
    CONFIG_AUTHOR = "config.author"  # create, edit and submit a draft
    CONFIG_APPROVE = "config.approve"  # approve or reject a submitted version
    CONFIG_ACTIVATE = "config.activate"  # activate an approved version, retire an active one
    AUDIT_READ = "audit.read"
    ACCESS_READ = "access.read"
    ACCESS_GRANT = "access.grant"  # grant and revoke role assignments
    JOURNAL_READ = "journal.read"  # Stage 7: journals, batches, attachments and periods in an entity
    JOURNAL_PREPARE = "journal.prepare"  # create, edit, submit, recall, cancel, reverse, import, attach
    JOURNAL_APPROVE_L1 = "journal.approve.l1"
    JOURNAL_APPROVE_L2 = "journal.approve.l2"
    JOURNAL_APPROVE_L3 = "journal.approve.l3"
    JOURNAL_DELEGATE = "journal.delegate"  # hand one's approval authority to a colleague for a while
    JOURNAL_EXPORT = "journal.export"  # the journal population and the exception report
    PERIOD_MANAGE = "period.manage"  # open, close, lock and reopen periods; cancel anyone's draft
    JOURNAL_POST = "journal.post"  # Stage 7b: start a posting run (never for one's own journals)
    LEDGER_FEED = "ledger.feed"  # read the ERP feed of posted journals (a machine client)
    JOURNAL_CONFIRM = "journal.confirm"  # write back ERP document numbers (a machine client)
    JOURNALING_RUN = "journaling.run"  # Stage 8a: journal an entity's sample data with the stand-in job
    ERP_EXTRACT = "erp.extract"  # upload the ERP's trial balance for an entity and period (a machine client)
    CLOSE_PREPARE = "close.prepare"  # Stage 8b: prepare close tasks assigned to you, run checks, explain flux
    CLOSE_REVIEW = "close.review"  # review close tasks assigned to you, and sign off the flux review


READ_ACTIONS: Final = frozenset(
    {
        Action.CONFIG_READ,
        Action.AUDIT_READ,
        Action.ACCESS_READ,
        Action.JOURNAL_READ,
        Action.JOURNAL_EXPORT,
        Action.LEDGER_FEED,
    }
)
APPROVE_LEVEL: Final = {1: Action.JOURNAL_APPROVE_L1, 2: Action.JOURNAL_APPROVE_L2, 3: Action.JOURNAL_APPROVE_L3}
_READ = frozenset({Action.CONFIG_READ})
_JOURNALS = frozenset({Action.CONFIG_READ, Action.JOURNAL_READ})
# Approval authority is hierarchical: an L2 approver may approve levels 1 and 2, an L3 approver any level.
_L1 = _JOURNALS | {Action.JOURNAL_APPROVE_L1, Action.JOURNAL_DELEGATE, Action.CLOSE_REVIEW}
_L2 = _L1 | {Action.JOURNAL_APPROVE_L2}
_L3 = _L2 | {Action.JOURNAL_APPROVE_L3}
POLICY: Final[dict[Role, frozenset[Action]]] = {
    Role.PLATFORM_ADMIN: frozenset({Action.CONFIG_READ, Action.AUDIT_READ, Action.ACCESS_READ, Action.ACCESS_GRANT}),
    Role.CONFIG_AUTHOR: frozenset({Action.CONFIG_READ, Action.CONFIG_AUTHOR}),
    Role.CONFIG_APPROVER: frozenset({Action.CONFIG_READ, Action.CONFIG_APPROVE, Action.CONFIG_ACTIVATE}),
    Role.MATCH_PREPARER: frozenset({Action.CONFIG_READ, Action.SMOKE_RUN}),
    Role.MATCH_APPROVER: _READ,
    Role.JOURNAL_PREPARER: _JOURNALS | {Action.JOURNAL_PREPARE, Action.CLOSE_PREPARE},
    Role.JOURNAL_APPROVER_L1: _L1,
    Role.JOURNAL_APPROVER_L2: _L2,
    Role.JOURNAL_APPROVER_L3: _L3,
    Role.CONTROLLER: _JOURNALS
    | {
        Action.AUDIT_READ,
        Action.ACCESS_READ,
        Action.PERIOD_MANAGE,
        Action.JOURNAL_EXPORT,
        Action.JOURNAL_POST,
        Action.JOURNALING_RUN,
        Action.CLOSE_PREPARE,
        Action.CLOSE_REVIEW,
    },
    Role.AUDITOR: _JOURNALS | {Action.AUDIT_READ, Action.ACCESS_READ, Action.JOURNAL_EXPORT},
    Role.INTEGRATION_CLIENT: frozenset(
        {Action.CONFIG_READ, Action.SMOKE_RUN, Action.LEDGER_FEED, Action.JOURNAL_CONFIRM, Action.ERP_EXTRACT}
    ),
}

Assignment = tuple[str, str]  # (role, scope)


def holds(assignments: Iterable[Assignment], action: Action, scope: str) -> bool:
    return any(s == scope and action in POLICY[Role(r)] for r, s in assignments)


def holds_anywhere(assignments: Iterable[Assignment], action: Action) -> bool:
    return any(action in POLICY[Role(r)] for r, _ in assignments)


def allowed(assignments: Iterable[Assignment], action: Action, entities: Iterable[str] | None) -> bool:
    """``entities`` None means a global object; otherwise the action is needed in every one of them."""
    held = list(assignments)
    if entities is None:
        return holds(held, action, GLOBAL)
    scopes = list(entities)
    return bool(scopes) and all(holds(held, action, e) for e in scopes)


def allowed_any(assignments: Iterable[Assignment], action: Action, entities: Iterable[str] | None) -> bool:
    """Like :func:`allowed`, but one of the entities is enough (reads of multi-entity objects)."""
    held = list(assignments)
    if entities is None:
        return holds(held, action, GLOBAL)
    return any(holds(held, action, e) for e in entities)


def access(requirement: str) -> dict[str, Any]:
    """Route metadata: ``public``, ``authenticated`` (reads filtered by entity visibility), or an action."""
    if requirement not in {"public", "authenticated"}:
        Action(requirement)  # raises for an unknown action
    return {ACCESS_KEY: requirement}
