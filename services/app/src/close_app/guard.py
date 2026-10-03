"""Request-level access checks: who is calling, and whether they may take an action on an object.

- **Invisible objects.** An object in none of the caller's visible entities is a 404, so its existence
  isn't revealed.
- **Visible objects.** A visible object without the needed action is a 403. A refused privileged action is a
  ``Denied``, which is audited; a refused read is a plain 403.
- **Global objects** (data sources, access administration) are never hidden; they are a 403 without the
  action at ``GLOBAL``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from fastapi import Depends, Request

from close_app.authz import READ_ACTIONS, Action, allowed, allowed_any, holds_anywhere
from close_app.errors import Denied, Problem
from close_app.identity import Principal, principal_from_request


def current_principal(request: Request) -> Principal:
    return principal_from_request(request, request.app.state.ctx.engine)


Caller = Annotated[Principal, Depends(current_principal)]


def not_found(what: str) -> Problem:
    return Problem(404, "Not found", f"{what} does not exist or is not visible to you", kind="not-found")


def deny(p: Principal, action: Action, target: str, entities: Sequence[str] = ()) -> Problem:
    if action in READ_ACTIONS:
        return Problem(403, "Forbidden", f"{action.value} is not granted to you here", kind="forbidden")
    return Denied(p.user_id, action.value, target=target, entity_ids=tuple(sorted(entities)))


def require(p: Principal, action: Action, entities: Sequence[str] | None, *, target: str, every: bool = True) -> None:
    """``entities`` None means a global object. Otherwise the action is needed in every one of them, or in any
    one of them when ``every`` is False (reading a multi-entity object)."""
    if entities is not None and not set(entities) & p.entities:
        raise not_found(target)
    check = allowed if every else allowed_any
    if not check(p.assignments, action, entities):
        raise deny(p, action, target, entities or ())


def require_anywhere(p: Principal, action: Action, *, target: str) -> None:
    if not holds_anywhere(p.assignments, action):
        raise deny(p, action, target)
