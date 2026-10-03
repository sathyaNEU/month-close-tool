"""The journaling engine's edge cases, on a tiny hand-built event type: suspense, configuration failures, the
circuit breaker, FX and rounding, closed periods, corrections, reruns and voids, determinism and rollups."""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import timedelta

import pytest

from close_contracts.default_sources import col, fn, lit
from close_engine.reference.journal.ledger import CircuitBreakerError, all_lines, digest, rollup, run
from close_engine.reference.journal.posting import ReferenceDataError

from .kit import BALANCED, DAY, KEYED, REF, config, definition, event, line


def n(value: int) -> dict[str, object]:
    return lit(value, "bigint")


def _only(lines: list, template: str):  # type: ignore[no-untyped-def]
    return [ln for ln in lines if ln.template == template]


def test_a_balanced_event_posts_and_a_zero_line_is_skipped() -> None:
    r = run(config(BALANCED), [event(1, 500, 500), event(2, 0, 0)], ref=REF)
    assert r.counts["posted"] == 1
    assert r.counts["no_entry"] == 1
    assert [(ln.account, ln.side, ln.amount_minor) for ln in r.lines if ln.event_id == "e001"] in (
        [("1010", "DR", 500), ("2010", "CR", 500)],
        [("2010", "CR", 500), ("1010", "DR", 500)],
    )


def test_a_negative_amount_keeps_its_effect_in_suspense() -> None:
    r = run(config(BALANCED), [event(1, -300, -300)], ref=REF, breaker_bps=10_000)
    assert r.counts["suspended"] == 1
    got = sorted((ln.account, ln.side, ln.amount_minor, ln.intended_account, ln.reason) for ln in r.lines)
    assert got == [
        ("9999", "CR", 300, "1010", "negative amount"),
        ("9999", "DR", 300, "2010", "negative amount"),
    ]


def test_a_missing_mapping_row_records_the_key_it_looked_for() -> None:
    r = run(config(KEYED), [event(1, 100, 100, key="unheard_of")], ref=REF, breaker_bps=10_000)
    [cr] = [ln for ln in r.lines if ln.side == "CR"]
    assert (cr.account, cr.intended_account) == ("9999", "mapping:test_key_account:unheard_of")


def test_a_missing_required_dimension_goes_to_suspense() -> None:
    r = run(config(KEYED), [event(1, 100, 100, program=None)], ref=REF, breaker_bps=10_000)
    [cr] = [ln for ln in r.lines if ln.side == "CR"]
    assert (cr.account, cr.intended_account, cr.reason) == ("9999", "4010", "dimension program is required on 4010")


def test_a_broken_identity_is_a_data_problem_balanced_to_suspense() -> None:
    r = run(config(BALANCED), [event(1, 500, 480)], ref=REF, breaker_bps=10_000)
    [balance] = _only(r.lines, "suspense")
    assert (balance.account, balance.side, balance.amount_minor) == ("9999", "CR", 20)
    assert balance.reason == "identity broken: a_is_b"
    assert r.failed_types == {}


def test_an_imbalance_with_the_identities_holding_fails_the_event_type() -> None:
    lopsided = definition(
        line("dr", "DR", "amount_a"),
        line("cr", "CR", "amount_a", {"account": "2010"}),
        line("x", "CR", "amount_b", {"account": "2020"}),
    )
    r = run(config(lopsided), [event(1, 500, 500)], ref=REF)
    assert list(r.failed_types) == ["TEST_EVENT"]
    assert r.lines == []
    assert r.state.events == {}  # nothing booked, so the next run retries


def test_the_circuit_breaker_trips_above_half_a_percent() -> None:
    clean = [event(i, 100, 100) for i in range(1, 199)]
    broken = [event(900, 100, 90), event(901, 100, 90)]
    with pytest.raises(CircuitBreakerError):
        run(config(BALANCED), [*clean, *broken], ref=REF)  # 2 of 200: 1%
    run(config(BALANCED), [*clean, event(199, 5, 5), broken[0]], ref=REF)  # 1 of 200: exactly 0.5%, allowed


def test_fx_conversion_records_its_rate_and_rounds_per_event() -> None:
    split = definition(
        line("dr", "DR", "amount_a"),  # 7 EUR -> 7.58 -> 8 USD
        {"line_id": "cr1", "side": "CR", "account": {"account": "2010"}, "amount": fn("sub", col("amount_b"), n(2))},
        {"line_id": "cr2", "side": "CR", "account": {"account": "2020"}, "amount": n(2)},  # 5 -> 5 and 2 -> 2 USD
    )
    r = run(config(split), [event(1, 7, 7, currency="EUR")], ref=REF)
    converted = [ln for ln in r.lines if ln.template != "rounding"]
    rate = REF.rates[(DAY, "EUR", "USD")][0]
    assert all((ln.fx_rate, ln.fx_rate_version, ln.fx_rate_date) == (rate, 3, DAY) for ln in converted)
    assert sorted(ln.functional_amount_minor for ln in converted) == [2, 5, 8]
    [rounding] = _only(r.lines, "rounding")
    assert (rounding.account, rounding.side, rounding.amount_minor, rounding.functional_amount_minor) == (
        "7010",
        "CR",
        0,
        1,
    )
    dr = sum(ln.functional_amount_minor for ln in r.lines if ln.side == "DR")
    cr = sum(ln.functional_amount_minor for ln in r.lines if ln.side == "CR")
    assert dr == cr
    with pytest.raises(ReferenceDataError):
        run(config(BALANCED), [event(1, 5, 5, currency="EUR", day=DAY + timedelta(days=1))], ref=REF)


def test_a_closed_period_posts_to_the_first_open_one_as_a_prior_period_adjustment() -> None:
    closed = replace(REF, closed={"US01": frozenset({"2026-07", "2026-08"})})
    r = run(config(BALANCED), [event(1, 5, 5)], ref=closed)
    assert {(ln.period, ln.is_prior_period_adjustment, ln.original_period) for ln in r.lines} == {
        ("2026-09", True, "2026-07")
    }


def test_corrections_reruns_and_voids() -> None:
    first = run(config(BALANCED), [event(1, 500, 500), event(2, 70, 70)], ref=REF)
    again = run(config(BALANCED), [event(1, 500, 500), event(2, 70, 70)], ref=REF, state=first.state)
    assert again.lines == []
    assert again.counts["unchanged"] == 2
    corrected = run(config(BALANCED), [event(1, 650, 650), event(2, 70, 70)], ref=REF, state=first.state)
    assert corrected.counts["corrected"] == 1
    assert {ln.event_version for ln in corrected.lines} == {2}
    assert sum(1 for ln in corrected.lines if ln.is_reversal) == 2
    booked = all_lines(corrected.state)
    assert sorted(ln.amount_minor for ln in booked if ln.event_id == "e001") == [650, 650]
    voided = run(config(BALANCED), [event(2, 70, 70)], ref=REF, state=corrected.state)
    assert voided.counts["voided"] == 1
    assert [ln.event_id for ln in all_lines(voided.state)] == ["e002", "e002"]


def test_input_order_does_not_change_the_books() -> None:
    events = [event(i, i * 10, i * 10, key="known") for i in range(1, 60)]
    shuffled = list(events)
    random.Random(7).shuffle(shuffled)  # noqa: S311 - a seeded test shuffle
    assert digest(run(config(KEYED), events, ref=REF).lines) == digest(run(config(KEYED), shuffled, ref=REF).lines)


def test_rollups_sum_their_lines_and_every_line_links_to_one() -> None:
    r = run(config(BALANCED), [event(i, 10 * i, 10 * i) for i in range(1, 6)], ref=REF)
    summary, links = rollup(r.lines)
    assert set(links) == {ln.line_id for ln in r.lines}
    assert sum(x.debit_minor for x in summary) == sum(ln.amount_minor for ln in r.lines if ln.side == "DR") == 150
    assert {x.account for x in summary} == {"1010", "2010"}
