"""Multi-currency manual journals' contracts (Gate 7c): typed rate keys, exact rates, the D9 conversion with one
rounding line per journal, the content hash functional-currency journals keep, and currency validation."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from close_contracts.canonical_json import content_sha256
from close_contracts.fx import MAX_KEYS, Rate, RateKey, RateKeyError, check_keys, latest, rate_text
from close_contracts.journals import (
    JournalDraft,
    content_hash,
    convert,
    in_functional,
    journal_issues,
    reversal_of,
    reverses_exactly,
    route,
)
from close_contracts.money import convert_minor
from close_contracts.samples.journal_workflow import SAMPLE_APPROVAL_MATRIX, SAMPLE_TEMPLATES
from close_contracts.samples.journaling import SAMPLE_CHART, SAMPLE_DIMENSIONS

TEMPLATES = {t.template_id: t for t in SAMPLE_TEMPLATES}
FX = TEMPLATES["sample_fx_journal"]
DAY = date(2026, 9, 15)


def draft(*lines: tuple[str, str, int], **fields: Any) -> JournalDraft:
    body: dict[str, Any] = {
        "entity_id": "US01",
        "template_id": "sample_fx_journal",
        "accounting_date": DAY.isoformat(),
        "description": "Euro carrier invoice accrued",
        "lines": [{"account": a, "side": s, "amount_minor": m} for a, s, m in lines],
    }
    return JournalDraft.model_validate(body | fields)


def rate(src: str = "EUR", dst: str = "USD", value: str = "1.084213000000", version: int = 1) -> Rate:
    return Rate(RateKey(src, dst, DAY), Decimal(value), version, "sample", 3)


# ---------------------------------------------------------------------------------------------- keys and rates


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("EUR", "XXQ", DAY), "not a currency"),
        (("EUR", "EUR", DAY), "not EUR to itself"),
        (("EUR", "USD", datetime(2026, 9, 15, 12, tzinfo=UTC)), "a day"),
        (("EUR", "USD", DAY, "monthly_average"), "not one of"),
    ],
)
def test_a_key_is_typed(args: tuple[Any, ...], message: str) -> None:
    with pytest.raises(RateKeyError, match=message):
        RateKey(*args)


def test_rates_are_exact_text_and_the_latest_version_wins() -> None:
    assert rate_text(Decimal("1.08")) == "1.080000000000"
    with pytest.raises(ValueError, match="more than 12 decimal places"):
        rate_text(Decimal("1.0000000000001"))
    r = rate()
    assert Rate.from_json(r.as_json()) == r
    assert r.as_json()["rate"] == "1.084213000000"  # a string, never a float
    rows = [
        {"rate_date": DAY, "from_currency": "EUR", "to_currency": "USD", "rate_type": "daily_spot",
         "rate": Decimal("1.08"), "version": 1, "source": "ecb"},
        {"rate_date": DAY, "from_currency": "EUR", "to_currency": "USD", "rate_type": "daily_spot",
         "rate": Decimal("1.09"), "version": 2, "source": "ecb"},
        {"rate_date": DAY, "from_currency": "EUR", "to_currency": "USD", "rate_type": "closing",
         "rate": Decimal("1.50"), "version": 9, "source": "ecb"},
        {"rate_date": DAY, "from_currency": "ZZZ", "to_currency": "USD", "rate_type": "daily_spot",
         "rate": Decimal("1"), "version": 1, "source": "junk"},
    ]  # fmt: skip
    found = latest(rows, [RateKey("EUR", "USD", DAY), RateKey("GBP", "USD", DAY)], table_version=7)
    assert list(found) == [RateKey("EUR", "USD", DAY)]  # no GBP row, and the unknown currency is ignored
    assert (found[RateKey("EUR", "USD", DAY)].rate, found[RateKey("EUR", "USD", DAY)].version) == (Decimal("1.09"), 2)
    assert found[RateKey("EUR", "USD", DAY)].table_version == 7
    with pytest.raises(RateKeyError, match=f"at most {MAX_KEYS}"):
        check_keys(RateKey("EUR", "USD", date.fromordinal(DAY.toordinal() + i)) for i in range(MAX_KEYS + 1))


# ---------------------------------------------------------------------------------------------- the conversion


def test_a_conversion_rounds_each_line_and_balances_on_one_rounding_line() -> None:
    d = draft(("5020", "DR", 1_001), ("5020", "DR", 1_001), ("2410", "CR", 2_002), currency="EUR")
    c = convert(d, functional="USD", rate=rate(value="1.333333000000"), rounding_account="7010")
    # 10.01 EUR x 1.333333 = 13.3466633 USD -> 1335 cents each; 20.02 EUR -> 2669 cents: one cent of rounding
    assert c.lines == (1_335, 1_335, 2_669)
    assert (c.rounding_side, c.rounding_minor, c.rounding_account, c.functional_total) == ("CR", 1, "7010", 2_670)
    assert c.as_json()["rounding"] == {"side": "CR", "amount_minor": 1, "account": "7010"}
    exact = convert(draft(("5020", "DR", 1_000), ("2410", "CR", 1_000), currency="EUR"), functional="USD",
                    rate=rate(value="1.500000000000"), rounding_account="7010")  # fmt: skip
    assert (exact.rounding_side, exact.as_json()["rounding"]) == (None, None)
    assert len(c.hash()) == 64
    assert (
        c.hash()
        != convert(d, functional="USD", rate=rate(value="1.333333000000", version=2), rounding_account="7010").hash()
    )
    with pytest.raises(ValueError, match="only a foreign-currency journal"):
        convert(draft(("5020", "DR", 1), ("2410", "CR", 1)), functional="USD", rate=rate(), rounding_account="7010")
    with pytest.raises(ValueError, match="can't convert GBP"):
        convert(draft(("5020", "DR", 1), ("2410", "CR", 1), currency="GBP"), functional="USD", rate=rate(),
                rounding_account="7010")  # fmt: skip


PAIRS = [("EUR", "USD"), ("JPY", "USD"), ("KWD", "EUR"), ("USD", "JPY"), ("GBP", "KWD"), ("EUR", "GBP")]


@settings(max_examples=10_000, deadline=None)
@given(
    pair=st.sampled_from(PAIRS),
    rate_value=st.decimals(min_value=Decimal("0.000001"), max_value=Decimal("2000"), places=6, allow_nan=False),
    debits=st.lists(st.integers(min_value=1, max_value=10**12), min_size=1, max_size=12),
    splits=st.integers(min_value=1, max_value=12),
)
def test_every_conversion_balances_within_half_a_minor_unit_per_line(
    pair: tuple[str, str], rate_value: Decimal, debits: list[int], splits: int
) -> None:
    total = sum(debits)
    credits = [total // splits] * (splits - 1) + [total - (total // splits) * (splits - 1)]
    lines = [("5020", "DR", m) for m in debits] + [("2410", "CR", m) for m in credits if m > 0]
    src, dst = pair
    d = draft(*lines, currency=src)
    r = Rate(RateKey(src, dst, DAY), rate_value, 1, "generated")
    c = convert(d, functional=dst, rate=r, rounding_account="7010")
    assert list(c.lines) == [convert_minor(ln.amount_minor, src, dst, rate_value) for ln in d.lines]
    dr = sum(a for a, ln in zip(c.lines, d.lines, strict=True) if ln.side == "DR")
    cr = sum(a for a, ln in zip(c.lines, d.lines, strict=True) if ln.side == "CR")
    rounding = {"DR": c.rounding_minor, "CR": 0} if c.rounding_side == "DR" else {"DR": 0, "CR": c.rounding_minor}
    assert dr + rounding["DR"] == cr + rounding["CR"] == c.functional_total
    assert 2 * c.rounding_minor <= len(d.lines)  # at most half a minor unit from each line


# ---------------------------------------------------------------------------------------------- hashes and checks


def test_a_functional_currency_draft_hashes_as_before_stage_7c() -> None:
    d = draft(("5020", "DR", 12_345), ("2410", "CR", 12_345), template_id="sample_standard_journal")
    before = {k: v for k, v in d.model_dump(mode="json").items() if k != "currency"}  # the 7a/7b body
    assert content_hash(d) == content_sha256(before)
    assert content_hash(in_functional(d.model_copy(update={"currency": "USD"}), "USD")) == content_hash(d)
    assert content_hash(d.model_copy(update={"currency": "EUR"})) != content_hash(d)


def test_currencies_are_checked_against_the_template_and_the_accounts() -> None:
    def codes(d: JournalDraft, functional: str = "USD") -> set[str]:
        template = TEMPLATES[d.template_id]
        found = journal_issues(
            d, template=template, chart=SAMPLE_CHART, dimensions=SAMPLE_DIMENSIONS, currency=functional
        )
        return {i.code for i in found}

    lines = (("5020", "DR", 100), ("2410", "CR", 100))
    assert codes(draft(*lines, currency="EUR")) == set()
    assert codes(draft(*lines, currency="JPY")) == {"currency_not_allowed"}
    assert codes(draft(*lines, currency="EUR", template_id="sample_standard_journal")) == {"currency_not_allowed"}
    assert codes(draft(*lines, currency="QQQ")) == {"unknown_currency"}
    usd_only = SAMPLE_CHART.model_copy(
        update={"accounts": tuple(a.model_copy(update={"currency": "USD"}) if a.code == "2410" else a
                                  for a in SAMPLE_CHART.accounts)}
    )  # fmt: skip
    found = journal_issues(
        draft(*lines, currency="EUR"), template=FX, chart=usd_only, dimensions=SAMPLE_DIMENSIONS, currency="USD"
    )
    assert [(i.code, i.path) for i in found] == [("account_currency", "/lines/1/account")]


def test_routing_uses_the_functional_total_it_is_given() -> None:
    d = draft(("5020", "DR", 900_000), ("2410", "CR", 900_000), currency="EUR")  # 9,000 EUR
    small = route(d, currency="USD", total_minor=900_000, matrix=SAMPLE_APPROVAL_MATRIX, risk=0)
    large = route(d, currency="USD", total_minor=1_000_000, matrix=SAMPLE_APPROVAL_MATRIX, risk=0)
    assert (small.amount_levels, large.amount_levels, large.total_minor) == (1, 2, 1_000_000)


def test_only_an_unedited_reversal_reuses_the_original_rate() -> None:
    original = draft(("5020", "DR", 700), ("2410", "CR", 700), currency="EUR")
    back = reversal_of(original, date(2026, 10, 1), "Reversal")
    assert back.currency == "EUR"
    assert reverses_exactly(back, original)
    edited = back.model_copy(update={"lines": (back.lines[0].model_copy(update={"amount_minor": 701}), back.lines[1])})
    assert not reverses_exactly(edited, original)
    assert not reverses_exactly(back.model_copy(update={"currency": "GBP"}), original)
