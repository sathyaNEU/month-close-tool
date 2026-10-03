"""Running a query spec on the read path, with its failures as problems (Stage 7b; shared from Stage 8a)."""

from __future__ import annotations

from collections.abc import Iterable

from close_app.context import AppContext
from close_app.errors import Problem
from close_app.identity import Principal
from close_contracts.query import QueryError, QuerySpec
from close_contracts.query_cursor import CursorError
from close_platform.query import Caller as ReadCaller
from close_platform.query import Page, QueryBusyError, QueryTimeoutError


def run_query(
    ctx: AppContext, spec: QuerySpec, p: Principal, entities: Iterable[str], cursor: str | None = None
) -> Page:
    """``spec`` for the caller, narrowed to ``entities`` (the query engine always applies them)."""
    if ctx.query is None:
        raise Problem(503, "The read path is not configured")
    try:
        return ctx.query.run(spec, ReadCaller(p.user_id, frozenset(entities)), cursor)
    except CursorError as exc:
        raise Problem(400, "Invalid cursor", str(exc)) from None
    except QueryError as exc:
        raise Problem(400, "Invalid query", str(exc)) from None
    except QueryBusyError:
        raise Problem(503, "The read path is busy", "retry shortly") from None
    except QueryTimeoutError:
        raise Problem(504, "The query timed out") from None
