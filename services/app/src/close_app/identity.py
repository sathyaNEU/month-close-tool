"""Who is calling: identity from the Databricks Apps proxy headers, entities from Postgres.

The app is reachable only through the proxy (locally ``devproxy``, in production the Databricks Apps
proxy), which sets these headers after SSO. As defence in depth the app still rejects anything a
well-behaved proxy would never send: missing, empty, duplicated or comma-joined identity values.
The principal is keyed on ``X-Forwarded-User`` (stable id); email and name are for display only.
Access is deny-by-default: a principal holds only the role assignments granted to it (``role_assignments``),
and sees only the entities it holds a role in (``close_app.authz``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from fastapi import Request
from sqlalchemy import Engine, text

from close_app.authz import GLOBAL, Assignment
from close_app.errors import Problem

USER_HEADER: Final = "x-forwarded-user"
EMAIL_HEADER: Final = "x-forwarded-email"
NAME_HEADER: Final = "x-forwarded-preferred-username"
_VALUE_RE: Final = re.compile(r"^[^\s,;][^,;]{0,254}$")


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str
    display_name: str
    entities: frozenset[str]  # the entities whose data the principal may see
    assignments: frozenset[Assignment] = frozenset()  # (role, scope)


def _single(request: Request, name: str, *, required: bool) -> str | None:
    values = request.headers.getlist(name)
    if not values:
        if required:
            raise Problem(401, "Not authenticated", f"missing {name} header")
        return None
    if len(values) != 1 or not _VALUE_RE.fullmatch(values[0].strip()):
        raise Problem(401, "Not authenticated", f"malformed {name} header")
    return values[0].strip()


def principal_from_request(request: Request, engine: Engine) -> Principal:
    user_id = _single(request, USER_HEADER, required=True)
    email = _single(request, EMAIL_HEADER, required=True)
    name = _single(request, NAME_HEADER, required=False)
    if user_id is None or email is None:  # unreachable: both are required above
        raise Problem(401, "Not authenticated")
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT role, scope FROM role_assignments WHERE user_id = :u AND revoked_at IS NULL"), {"u": user_id}
        ).all()
    assignments = frozenset((str(r.role), str(r.scope)) for r in rows)
    entities = frozenset(scope for _, scope in assignments if scope != GLOBAL)
    return Principal(user_id, email, name or email, entities, assignments)
