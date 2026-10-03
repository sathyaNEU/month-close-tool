"""The close checklist and flux rules (Stage 8b): their shape, the business-day due-date vectors, and flux lines."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from close_contracts.calendar import BusinessCalendar
from close_contracts.config.close import (
    CloseChecklist,
    CloseTask,
    FluxRules,
    FluxThreshold,
    calendar_window,
    due_date,
    flux_hash,
    flux_line,
    measure_of,
    topological,
)
from close_contracts.samples.close import SAMPLE_CHECKLIST, SAMPLE_FLUX_RULES

US_FED = frozenset({date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25), date(2027, 1, 1)})


def _days(holidays: frozenset[date] = US_FED) -> dict[date, bool]:
    calendar = BusinessCalendar("TEST", holidays)
    day, out = date(2026, 6, 1), {}
    while day <= date(2027, 2, 28):
        out[day] = calendar.is_business_day(day)
        day += timedelta(days=1)
    return out


def _task(task_id: str, **fields: Any) -> CloseTask:
    return CloseTask.model_validate(
        {"task_id": task_id, "name": task_id, "due_day": 1, "preparer": "1001", "reviewer": "1004"} | fields
    )


# ---------------------------------------------------------------------------------------------- the checklist's shape


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"due_day": 0}, "there is no BD0"),
        ({"check": "gl_balanced"}, "either a preparer"),  # a preparer and a check
        ({"preparer": None}, "either a preparer"),  # neither
        ({"reviewer": None}, "a manual task needs a reviewer"),
        ({"reviewer": "1001"}, "can't be its preparer"),
        ({"depends_on": ["a"]}, "each once"),  # itself
        ({"depends_on": ["b", "b"]}, "each once"),
        ({"entities": ["us01"]}, "distinct entity ids"),
        ({"due_day": 21}, "less than or equal to 20"),
    ],
)
def test_a_task_is_manual_or_automatic_with_distinct_people(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _task("a", **fields)


def test_an_automatic_task_has_a_check_and_may_have_a_reviewer() -> None:
    task = _task("a", preparer=None, check="flux_reviewed", reviewer=None)
    assert (task.check, task.applies_to("EU01")) == ("flux_reviewed", True)
    assert _task("b", preparer=None, check="erp_ties").reviewer == "1004"
    assert not _task("c", entities=["US01"]).applies_to("EU01")


@pytest.mark.parametrize(
    ("tasks", "message"),
    [
        ([_task("a"), _task("a")], "unique"),
        ([_task("a", depends_on=["missing"])], "unknown tasks: missing"),
        ([_task("a", depends_on=["b"]), _task("b", depends_on=["a"])], "form a cycle"),
        ([_task("a", depends_on=["b"]), _task("b", depends_on=["c"]), _task("c", depends_on=["a"])], "form a cycle"),
        ([_task("all", depends_on=["us"]), _task("us", entities=["US01"])], "applies to entities"),
        ([_task("eu", entities=["EU01"], depends_on=["us"]), _task("us", entities=["US01"])], "applies to entities"),
    ],
)
def test_a_checklist_has_unique_tasks_known_dependencies_and_no_cycle(tasks: list[CloseTask], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        CloseChecklist(checklist_id="checklist", name="c", tasks=tuple(tasks))


def test_tasks_come_after_their_dependencies() -> None:
    tasks = [_task("c", depends_on=["b", "a"]), _task("b", depends_on=["a"]), _task("a"), _task("d")]
    assert topological(tasks) == ["a", "d", "b", "c"]
    assert topological(SAMPLE_CHECKLIST.tasks)[-1] == "controller_sign_off"
    us = [t.task_id for t in SAMPLE_CHECKLIST.tasks_for("US01")]
    eu = [t.task_id for t in SAMPLE_CHECKLIST.tasks_for("EU01")]
    assert (len(us), eu) == (9, ["journals_settled", "suspense_clear", "gl_balanced", "erp_ties", "flux_reviewed"])


# ---------------------------------------------------------------------------------------------- due dates

# (period end, due day, expected due date): the business-day convention's test vectors.
DUE_VECTORS = [
    (date(2026, 9, 30), -1, date(2026, 9, 30)),  # a Wednesday: BD-1 is the period end itself
    (date(2026, 9, 30), -2, date(2026, 9, 29)),
    (date(2026, 9, 30), 1, date(2026, 10, 1)),
    (date(2026, 9, 30), 3, date(2026, 10, 5)),  # over a weekend
    (date(2026, 10, 31), -1, date(2026, 10, 30)),  # a month that ends on a Saturday
    (date(2026, 10, 31), 1, date(2026, 11, 2)),
    (date(2026, 8, 31), 4, date(2026, 9, 4)),
    (date(2026, 8, 31), 5, date(2026, 9, 8)),  # over Labor Day
    (date(2026, 12, 31), 1, date(2027, 1, 4)),  # over New Year's Day and a weekend
    (date(2026, 7, 31), -20, date(2026, 7, 6)),  # the furthest back a task may be
    (date(2026, 11, 30), -3, date(2026, 11, 25)),  # back over Thanksgiving
]


@pytest.mark.parametrize(("ends_on", "day", "expected"), DUE_VECTORS)
def test_due_dates_count_business_days_from_the_period_end(ends_on: date, day: int, expected: date) -> None:
    assert due_date(ends_on, day, _days()) == expected


def test_a_month_that_ends_on_a_holiday() -> None:
    days = _days(US_FED | {date(2026, 11, 30)})  # say November 30th were a holiday (a Monday)
    assert due_date(date(2026, 11, 30), -1, days) == date(2026, 11, 27)  # Friday; Thanksgiving is the 26th
    assert due_date(date(2026, 11, 30), 1, days) == date(2026, 12, 1)


def test_a_calendar_that_stops_short_gives_no_date() -> None:
    days = {d: b for d, b in _days().items() if d <= date(2026, 10, 2)}
    assert due_date(date(2026, 9, 30), 2, days) == date(2026, 10, 2)
    assert due_date(date(2026, 9, 30), 3, days) is None
    assert due_date(date(2026, 9, 30), -1, {}) is None
    with pytest.raises(ValueError, match="no BD0"):
        due_date(date(2026, 9, 30), 0, days)


def test_the_calendar_window_holds_every_due_day() -> None:
    start, end = calendar_window(date(2026, 9, 30), [-3, 1, 5])
    assert (start, end) == (date(2026, 9, 10), date(2026, 10, 24))
    days = {d: b for d, b in _days().items() if start <= d <= end}
    assert all(due_date(date(2026, 9, 30), n, days) for n in (-3, 1, 5))


# ---------------------------------------------------------------------------------------------- flux


def _threshold(amount: int, percent: int, operator: str = "and") -> FluxThreshold:
    return FluxThreshold.model_validate({"amounts": {"USD": amount}, "percent": percent, "operator": operator})


def test_a_threshold_joins_its_amount_and_percent() -> None:
    both, either = _threshold(1_000, 10), _threshold(1_000, 10, "or")
    assert not both.exceeded(1_000, 100_000, "USD")  # the amount, not the percent
    assert not both.exceeded(500, 1_000, "USD")  # the percent, not the amount
    assert both.exceeded(2_000, 10_000, "USD")
    assert either.exceeded(1_000, 100_000, "USD")
    assert either.exceeded(500, 1_000, "USD")
    assert not either.exceeded(99, 100_000, "USD")
    assert both.exceeded(-2_000, -10_000, "USD")  # signs don't matter
    assert not both.exceeded(0, 0, "USD")
    assert _threshold(0, 0, "or").exceeded(1, 1_000_000, "USD")  # zero amounts: any change
    assert both.exceeded(1_000, 0, "USD")  # from zero: over any percent
    assert both.exceeded(1, 1_000, "EUR")  # no amount for the currency: any change


def test_flux_rules_cover_every_account_type() -> None:
    with pytest.raises(ValidationError, match="every account type needs a threshold: revenue, expense"):
        FluxRules.model_validate(
            {
                "rules_id": "rules",
                "by_type": {t: {"amounts": {"USD": 1}, "percent": 1} for t in ("asset", "liability", "equity")},
            }
        )
    with pytest.raises(ValidationError, match="amounts are per ISO currency"):
        FluxThreshold.model_validate({"amounts": {"usd": 1}, "percent": 1})
    with pytest.raises(ValidationError, match="can't be negative"):
        FluxThreshold.model_validate({"amounts": {"USD": -1}, "percent": 1})
    rules = SAMPLE_FLUX_RULES
    assert rules.threshold("9999", "asset") is rules.accounts["9999"]  # an override wins
    assert rules.threshold("1110", "asset") is rules.by_type["asset"]
    assert rules.threshold("0000", None) is None  # not in the chart
    assert rules.currencies() == {"USD", "EUR", "GBP"}


def test_a_flux_line_measures_by_account_type() -> None:
    assert (measure_of("asset"), measure_of("equity"), measure_of("revenue"), measure_of(None)) == (
        "closing",
        "closing",
        "activity",
        "closing",
    )
    line = flux_line("1110", "asset", 11_000, 10_000, threshold=_threshold(500, 5), currency="USD")
    assert (line.change_minor, line.percent, line.needs_explanation) == (1_000, "10.00", True)
    new = flux_line("4020", "revenue", -100, 0, threshold=_threshold(500, 50, "or"), currency="USD")
    assert (new.percent, new.needs_explanation) == (None, True)
    unknown = flux_line("0000", None, 1, 0, threshold=None, currency="USD")
    assert unknown.needs_explanation  # an account the rules don't cover: any change
    assert flux_line("1110", "asset", 3, 3, threshold=None, currency="USD").percent == "0.00"


def test_the_flux_hash_binds_lines_and_explanations() -> None:
    a = flux_line("1110", "asset", 11_000, 10_000, threshold=_threshold(500, 5), currency="USD")
    b = flux_line("4020", "revenue", -100, 0, threshold=_threshold(500, 50, "or"), currency="USD")
    base = flux_hash([a, b], {"1110": "x1", "4020": "x2"})
    assert flux_hash([b, a], {"4020": "x2", "1110": "x1"}) == base  # order doesn't matter
    assert flux_hash([a, b], {"1110": "x1", "4020": "x3"}) != base  # another explanation
    moved = flux_line("1110", "asset", 11_001, 10_000, threshold=_threshold(500, 5), currency="USD")
    assert flux_hash([moved, b], {"1110": "x1", "4020": "x2"}) != base  # another amount
