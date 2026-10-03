from __future__ import annotations

import json
from datetime import date, timedelta
from importlib import resources

import pytest
from hypothesis import given
from hypothesis import strategies as st

from close_contracts.calendar import CALENDAR_END, EPOCH, BusinessCalendar, CalendarRangeError, Roll

CAL = BusinessCalendar.of(
    "TEST_US", holidays=[date(2026, 1, 1), date(2026, 1, 19), date(2026, 7, 3), date(2026, 12, 25)]
)


def _brute_is_bd(d: date) -> bool:
    return d.weekday() < 5 and d not in CAL.holidays


def _brute_roll(d: date, roll: Roll) -> date:
    step = timedelta(days=1 if roll is Roll.FOLLOWING else -1)
    while not _brute_is_bd(d):
        d += step
    return d


def _brute_index(d: date, roll: Roll) -> int:
    target = _brute_roll(d, roll)
    return sum(1 for i in range((target - EPOCH).days) if _brute_is_bd(EPOCH + timedelta(days=i)))


def test_weekends_and_holidays() -> None:
    assert CAL.is_business_day(date(2026, 9, 28))  # Monday
    assert not CAL.is_business_day(date(2026, 9, 26))  # Saturday
    assert not CAL.is_business_day(date(2026, 7, 3))  # holiday


def test_roll_following_and_preceding() -> None:
    assert CAL.roll(date(2026, 7, 3), Roll.FOLLOWING) == date(2026, 7, 6)  # Fri holiday -> Mon
    assert CAL.roll(date(2026, 7, 3), Roll.PRECEDING) == date(2026, 7, 2)
    assert CAL.roll(date(2026, 9, 28)) == date(2026, 9, 28)


def test_payout_to_bank_window_example() -> None:
    # A Saturday payout rolls to Monday; a Wednesday bank credit is 2 business days later.
    assert CAL.business_days_between(date(2026, 9, 26), date(2026, 9, 30)) == 2
    assert CAL.add_business_days(date(2026, 7, 2), 1) == date(2026, 7, 6)
    assert CAL.add_business_days(date(2026, 7, 6), -1) == date(2026, 7, 2)


def test_epoch_is_index_zero() -> None:
    assert CAL.bd_index(EPOCH) == 0


def test_range_errors() -> None:
    with pytest.raises(CalendarRangeError):
        CAL.bd_index(EPOCH - timedelta(days=1))
    with pytest.raises(CalendarRangeError):
        CAL.is_business_day(CALENDAR_END + timedelta(days=1))
    with pytest.raises(CalendarRangeError):
        CAL.add_business_days(CALENDAR_END - timedelta(days=3), 10)


@pytest.mark.parametrize(("code", "weekend"), [("", {5, 6}), ("bad code", {5, 6}), ("OK", set(range(7))), ("OK", {7})])
def test_invalid_calendar_definitions(code: str, weekend: set[int]) -> None:
    with pytest.raises(ValueError, match="invalid"):
        BusinessCalendar.of(code, weekend=weekend)


def test_index_rows_for_ref_table() -> None:
    rows = CAL.index_rows(date(2026, 7, 2), date(2026, 7, 6))
    assert [(d.isoformat(), bd) for d, bd, _ in rows] == [
        ("2026-07-02", True),
        ("2026-07-03", False),
        ("2026-07-04", False),
        ("2026-07-05", False),
        ("2026-07-06", True),
    ]
    assert rows[1][2] == rows[2][2] == rows[3][2] == rows[4][2] == rows[0][2] + 1


def test_vectors_file_matches_implementation() -> None:
    data = json.loads(
        resources.files("close_contracts").joinpath("test_vectors/calendar.json").read_text(encoding="utf-8")
    )
    cal = BusinessCalendar.of(
        data["code"], holidays=[date.fromisoformat(h) for h in data["holidays"]], weekend=data["weekend"]
    )
    for case in data["cases"]:
        d = date.fromisoformat(case["date"])
        assert cal.is_business_day(d) == case["is_business_day"]
        assert cal.bd_index(d, Roll.FOLLOWING) == case["bd_index_following"]
        assert cal.bd_index(d, Roll.PRECEDING) == case["bd_index_preceding"]


@given(st.dates(min_value=date(2025, 6, 1), max_value=date(2027, 6, 1)), st.sampled_from(list(Roll)))
def test_bd_index_matches_brute_force(d: date, roll: Roll) -> None:
    assert CAL.bd_index(d, roll) == _brute_index(d, roll)
    assert CAL.roll(d, roll) == _brute_roll(d, roll)
