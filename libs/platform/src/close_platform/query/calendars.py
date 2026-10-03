"""The narrow business-calendar lookup (Stage 8b, ADR-0020): an entity and a date range in, its calendar's days out.

It is deliberately not a query spec, like the rate lookup (ADR-0018). ``ref.entities`` and ``ref.calendars`` are
global reference data, so the query engine's entity filter stays as Stage 1b signed it off, and no spec can reach
them. It is the one source of business days: the entity's calendar code comes from ``ref.entities``, as the
engine's does, not from a second copy in configuration.

- **Typed input.** An entity id that matches the entity pattern, and a range of at most :data:`MAX_DAYS` days.
- **Two fixed queries.** The entity's calendar code, then that calendar's days in the range, each table read at one
  pinned version (``F.lit`` conditions, never SQL text). The calendar's version is returned with its days.
- **Through the read path's session pool,** like every Spark Connect read. The app calls it before opening a
  database transaction, never inside one.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Protocol

from close_contracts.config.journaling import ENTITY
from close_contracts.resolver import PathTableResolver, TableResolver
from close_contracts.tables import LogicalTable, Tables
from close_platform.query.history import MissingTableError, latest_version

MAX_DAYS: Final = 400
SessionRunner = Callable[[Callable[[Any], Any]], Any]  # as the query engine's; pyspark is imported only when used


@dataclass(frozen=True)
class EntityCalendar:
    calendar_code: str
    days: Mapping[date, bool]  # each day the calendar has in the range, and whether it is a business day
    version: int | None  # the ref.calendars version read


class CalendarLookup(Protocol):
    def calendar(self, entity_id: str, start: date, end: date) -> EntityCalendar | None:
        """The entity's calendar over ``[start, end]``; None when the lake has no calendar code for the entity."""
        ...


def check_range(entity_id: str, start: date, end: date) -> None:
    if not re.fullmatch(ENTITY, entity_id):
        raise ValueError(f"{entity_id!r} is not an entity id")
    if not start <= end or (end - start).days >= MAX_DAYS:
        raise ValueError(f"a calendar range runs forward, at most {MAX_DAYS} days")


class DataFrameCalendars:
    """Calendars over Spark Connect (the app) or a local session (tests)."""

    def __init__(self, run: SessionRunner, resolver: TableResolver) -> None:
        self._run = run
        self._resolver = resolver

    def calendar(self, entity_id: str, start: date, end: date) -> EntityCalendar | None:
        check_range(entity_id, start, end)

        def work(session: Any) -> EntityCalendar | None:
            from pyspark.sql import functions as F

            def pinned(table: LogicalTable) -> tuple[Any, int]:
                version = latest_version(session, self._resolver, table)
                reader = session.read.format("delta").option("versionAsOf", version)
                if isinstance(self._resolver, PathTableResolver):
                    return reader.load(self._resolver.location(table)), version
                return reader.table(self._resolver.table(table)), version

            try:
                entities, _ = pinned(Tables.REF_ENTITIES)
                calendars, version = pinned(Tables.REF_CALENDARS)
            except MissingTableError:
                return None
            codes = entities.where(F.col("entity_id") == F.lit(entity_id)).select("calendar_code").toArrow().to_pylist()
            found = sorted({str(r["calendar_code"]) for r in codes if r["calendar_code"]})
            if len(found) != 1:
                return None  # no row, or rows that disagree: either way, no calendar to trust
            rows = (
                calendars.where(
                    (F.col("calendar_code") == F.lit(found[0])) & F.col("day").between(F.lit(start), F.lit(end))
                )
                .select("day", "is_business_day")
                .toArrow()
                .to_pylist()
            )
            return EntityCalendar(found[0], {r["day"]: bool(r["is_business_day"]) for r in rows}, version)

        found: EntityCalendar | None = self._run(work)
        return found


class ReferenceCalendars:
    """In-memory calendars for the app's tests: entity -> calendar code, and code -> {day: is a business day}."""

    def __init__(self, entities: Mapping[str, str], days: Mapping[str, Mapping[date, bool]], version: int = 1) -> None:
        self.entities = dict(entities)
        self.days = {code: dict(d) for code, d in days.items()}
        self.version = version
        self.calls: list[tuple[str, date, date]] = []

    def calendar(self, entity_id: str, start: date, end: date) -> EntityCalendar | None:
        check_range(entity_id, start, end)
        self.calls.append((entity_id, start, end))
        code = self.entities.get(entity_id)
        if code is None:
            return None
        days = {d: b for d, b in self.days.get(code, {}).items() if start <= d <= end}
        return EntityCalendar(code, days, self.version)
