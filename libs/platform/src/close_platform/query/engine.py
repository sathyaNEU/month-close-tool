"""The query engine: runs a QuerySpec for a caller over a pluggable backend.

For every query it validates the spec against the table registry, adds the caller's entity filter
(an empty grant set returns nothing and never reaches the backend), pins the whole paging session to
the table version read when the first page was requested, and hands out a signed cursor for the next
page. Backends only execute a fully resolved :class:`QueryPlan`; none of them decides anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from close_contracts.query import QueryableTable, QueryError, QuerySpec, Row, Scalar, validate
from close_contracts.query_cursor import CursorCodec
from close_contracts.query_reference import sort_key
from close_contracts.queryable import queryable


class TableMissingError(LookupError):
    """The table has never been written (so there is nothing to read yet)."""


class QueryTimeoutError(TimeoutError):
    """The backend did not answer in time and the query was interrupted."""


class QueryBusyError(RuntimeError):
    """Every query slot is taken; the caller should retry shortly (a 503)."""


class QueryUnavailableError(RuntimeError):
    """The backend cannot be reached right now (for example the Spark Connect server is restarting);
    the caller should retry shortly (a 503)."""


@dataclass(frozen=True)
class Caller:
    user_id: str
    entities: frozenset[str]


@dataclass(frozen=True)
class QueryPlan:
    """A spec with everything resolved: the entity scope, the snapshot version, where to resume, and
    how many rows to fetch (one more than the page, to know whether another page exists)."""

    spec: QuerySpec
    table: QueryableTable
    entities: frozenset[str]
    after: tuple[Scalar, ...] | None
    version: int | None
    fetch: int


class QueryBackend(Protocol):
    def current_version(self, table: QueryableTable) -> int | None:
        """The table's latest version (None when the backend has no versions); TableMissingError if absent."""
        ...

    def fetch(self, plan: QueryPlan) -> list[Row]: ...


@dataclass(frozen=True)
class Page:
    rows: tuple[Row, ...]
    next_cursor: str | None
    version: int | None


class QueryEngine:
    def __init__(self, backend: QueryBackend, codec: CursorCodec) -> None:
        self._backend = backend
        self._codec = codec

    def run(self, spec: QuerySpec, caller: Caller, cursor: str | None = None) -> Page:
        table = queryable(spec.table)
        validate(spec, table)
        if not caller.entities:
            return Page((), None, None)  # deny by default: no grants, no query
        fingerprint = spec.fingerprint()
        if cursor is not None:
            if spec.grouped:
                raise QueryError("grouped results are a single page")
            state = self._codec.open(cursor, spec=fingerprint, principal=caller.user_id)
            version, after = state.version, state.after
        else:
            try:
                version = self._backend.current_version(table)
            except TableMissingError:
                return Page((), None, None)
            after = None
        rows = self._backend.fetch(QueryPlan(spec, table, caller.entities, after, version, spec.limit + 1))
        if spec.grouped:
            if len(rows) > spec.limit:
                raise QueryError(f"more than {spec.limit} groups; narrow the filters")
            return Page(tuple(rows), None, version)
        page = rows[: spec.limit]
        more = len(rows) > spec.limit
        next_cursor = (
            self._codec.issue(
                spec=fingerprint, principal=caller.user_id, version=version, after=sort_key(page[-1], spec.sort)
            )
            if more
            else None
        )
        return Page(tuple(page), next_cursor, version)
