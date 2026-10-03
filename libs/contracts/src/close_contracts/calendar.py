"""Business-day calendars and the business-day index used by date-window matching rules.

A matching rule such as "bank date within 0..+3 business days of the payout date" compares
*business-day indexes* instead of calendar dates. Every date gets an integer index: business days are
numbered consecutively from the epoch, and a non-business day takes the index of the business day it
rolls to. The rule then becomes simple integer arithmetic, which Spark can evaluate with an equi-join
on ``A.bd + k = B.bd`` (see the rule compiler in Stage 3).

The calendar data itself (which holidays apply to ``US_FED``, ``TARGET2``, ...) is configuration and
lands in ``ref.calendars``; this module only defines the semantics, so Python and Spark agree.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from typing import Final

EPOCH: Final = date(2000, 1, 3)
"""Index origin (a Monday). ``bd_index(EPOCH) == 0`` for calendars where it is a business day."""

CALENDAR_END: Final = date(2100, 12, 31)
"""Last date covered by an index table."""

WEEKEND_SAT_SUN: Final = frozenset({5, 6})  # date.weekday(): Monday == 0 ... Sunday == 6


class Roll(StrEnum):
    """How a non-business day maps onto a business day."""

    FOLLOWING = "following"
    PRECEDING = "preceding"


class CalendarRangeError(ValueError):
    """Raised for a date outside [EPOCH, CALENDAR_END]."""


@dataclass(frozen=True)
class BusinessCalendar:
    """A named set of non-working weekdays plus explicit holidays."""

    code: str
    holidays: frozenset[date] = frozenset()
    weekend: frozenset[int] = WEEKEND_SAT_SUN
    _business_days: tuple[date, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.code or not self.code.replace("_", "").isalnum():
            raise ValueError(f"invalid calendar code {self.code!r}")
        if not self.weekend <= set(range(7)) or len(self.weekend) == 7:
            raise ValueError(f"invalid weekend definition {sorted(self.weekend)}")
        days = []
        d = EPOCH
        one = timedelta(days=1)
        while d <= CALENDAR_END:
            if d.weekday() not in self.weekend and d not in self.holidays:
                days.append(d)
            d += one
        object.__setattr__(self, "_business_days", tuple(days))

    @classmethod
    def of(cls, code: str, holidays: Iterable[date] = (), weekend: Iterable[int] = WEEKEND_SAT_SUN) -> BusinessCalendar:
        return cls(code=code, holidays=frozenset(holidays), weekend=frozenset(weekend))

    @staticmethod
    def _check(d: date) -> None:
        if not EPOCH <= d <= CALENDAR_END:
            raise CalendarRangeError(f"{d} is outside the supported range {EPOCH}..{CALENDAR_END}")

    def is_business_day(self, d: date) -> bool:
        self._check(d)
        return d.weekday() not in self.weekend and d not in self.holidays

    def roll(self, d: date, roll: Roll = Roll.FOLLOWING) -> date:
        """Return ``d`` if it is a business day, else the next (FOLLOWING) or previous (PRECEDING) one."""
        self._check(d)
        days = self._business_days
        if roll is Roll.FOLLOWING:
            i = bisect.bisect_left(days, d)
            if i == len(days):
                raise CalendarRangeError(f"no business day on or after {d} before {CALENDAR_END}")
            return days[i]
        i = bisect.bisect_right(days, d) - 1
        if i < 0:
            raise CalendarRangeError(f"no business day on or before {d} after {EPOCH}")
        return days[i]

    def bd_index(self, d: date, roll: Roll = Roll.FOLLOWING) -> int:
        """Business-day index of ``d`` (non-business days take the index of their rolled date)."""
        rolled = self.roll(d, roll)
        return bisect.bisect_left(self._business_days, rolled)

    def add_business_days(self, d: date, n: int, roll: Roll = Roll.FOLLOWING) -> date:
        """Roll ``d`` to a business day, then move ``n`` business days (n may be negative)."""
        i = self.bd_index(d, roll) + n
        if not 0 <= i < len(self._business_days):
            raise CalendarRangeError(f"{d} {n:+d} business days falls outside the calendar range")
        return self._business_days[i]

    def business_days_between(self, start: date, end: date, roll: Roll = Roll.FOLLOWING) -> int:
        """``bd_index(end) - bd_index(start)``: the distance a date-window condition compares."""
        return self.bd_index(end, roll) - self.bd_index(start, roll)

    def index_rows(self, start: date, end: date, roll: Roll = Roll.FOLLOWING) -> list[tuple[date, bool, int]]:
        """Rows for the ``ref.calendars`` table: (date, is_business_day, bd_index) for every date."""
        self._check(start)
        self._check(end)
        rows = []
        d = start
        one = timedelta(days=1)
        while d <= end:
            rows.append((d, self.is_business_day(d), self.bd_index(d, roll)))
            d += one
        return rows
