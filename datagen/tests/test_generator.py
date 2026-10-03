"""The generator without Spark: determinism, exact break quotas, the answer key, identity keys, and the
default data sources evaluated by the DSL interpreter on every generated row."""

from __future__ import annotations

from collections import Counter, defaultdict
from functools import cache

import pytest

from close_contracts.binding import bind
from close_contracts.default_sources import DEFAULT_SOURCES, IDENTITY_KEYS, source_key
from close_contracts.dsl import DslEvalError, evaluate
from close_datagen.answer_check import check, passed
from close_datagen.breaks import apply_order_breaks, quota
from close_datagen.controls import expected
from close_datagen.generate import break_counts, generate
from close_datagen.landing import Builder
from close_datagen.plan import build_plan
from close_datagen.profiles import BREAK_TYPES, PROFILES
from close_datagen.world import BY_ID, CALENDARS


@cache
def small(seed: int) -> Builder:
    return generate(seed, PROFILES["small"], f"t-{seed}")


def _fingerprint(b: Builder) -> str:
    rows = sorted((t, source_key([r[k] for k in IDENTITY_KEYS[t]])) for t in IDENTITY_KEYS for r in b.rows.get(t, []))
    return source_key([list(x) for x in rows])


def test_the_plan_is_a_pure_function_of_the_seed() -> None:
    first, _ = build_plan(11, PROFILES["small"])
    again, _ = build_plan(11, PROFILES["small"])
    other, _ = build_plan(23, PROFILES["small"])
    assert [(o.order_id, o.card_amount_minor, o.seller.seller_id) for o in first] == [
        (o.order_id, o.card_amount_minor, o.seller.seller_id) for o in again
    ]
    assert [o.card_amount_minor for o in first] != [o.card_amount_minor for o in other]


def test_generation_is_repeatable_row_for_row() -> None:
    assert _fingerprint(generate(11, PROFILES["tiny"], "a")) == _fingerprint(generate(11, PROFILES["tiny"], "b"))
    assert expected(generate(11, PROFILES["tiny"], "a").rows) == expected(generate(11, PROFILES["tiny"], "b").rows)


def test_quota_is_exact_and_seeded() -> None:
    items = [f"k{i}" for i in range(1000)]
    chosen = quota(7, "x", 0.025, items, lambda k: k)
    assert len(chosen) == 25
    assert chosen == quota(7, "x", 0.025, list(reversed(items)), lambda k: k)  # independent of input order
    assert chosen != quota(8, "x", 0.025, items, lambda k: k)


def test_order_break_counts_equal_their_quotas() -> None:
    profile = PROFILES["small"]
    orders, _ = build_plan(11, profile)
    counts = apply_order_breaks(11, profile, orders)
    live = [o for o in orders if not o.cancelled]
    assert counts["split_tender"] == round(profile.rate("split_tender") * len(live))
    assert counts["chargeback"] == round(profile.rate("chargeback") * len([o for o in live if o.card_amount_minor > 0]))
    assert counts["late_win"] == round(profile.rate("late_win") * counts["chargeback"])


@pytest.mark.parametrize("seed", [11, 23, 47])
def test_small_has_every_break_type_at_least_25_times(seed: int) -> None:
    counts = break_counts(small(seed))
    assert set(counts) == set(BREAK_TYPES)
    assert min(counts.values()) >= 25, counts


@pytest.mark.parametrize("seed", [11, 23, 47])
def test_the_answer_key_passes_every_self_consistency_check(seed: int) -> None:
    report = check(small(seed))
    assert passed(report), {k: v for k, v in report.items() if v["failed"]}


def test_identity_keys_are_unique_in_every_landing_table() -> None:
    b = small(11)
    for table, keys in IDENTITY_KEYS.items():
        counts = Counter(tuple(r[k] for k in keys) for r in b.rows.get(table, []))
        assert not [k for k, n in counts.items() if n > 1], table


def test_bank_lines_use_each_format_and_business_days() -> None:
    b = small(11)
    formats = {r["format"]: r for r in b.rows["landing.bank_statement_lines"]}
    assert set(formats) == {"bai2", "camt053", "mt940"}
    assert formats["bai2"]["amount_text"].isdigit()
    assert "," in formats["mt940"]["amount_text"]
    assert "." in formats["camt053"]["amount_text"]
    for row in b.rows["landing.bank_statement_lines"]:
        calendar = CALENDARS[{"bai2": "US_FED", "camt053": "TARGET2", "mt940": "UK_BANK"}[row["format"]]]
        assert calendar.is_business_day(row["value_date"])


def test_every_default_source_maps_every_generated_row() -> None:
    b = small(11)
    mapped_totals: dict[tuple[str, str], int] = defaultdict(int)
    for source in DEFAULT_SOURCES:
        bound = bind(source)
        rows = b.rows.get(source.landing_table, [])
        assert rows, source.source_id
        for row in rows:
            if bound.filter is not None and evaluate(bound.filter, row) is not True:
                continue
            try:
                item = {field: evaluate(ir, row) for field, ir in bound.mapping.items()}
            except DslEvalError as exc:
                pytest.fail(f"{source.source_id} rejected a generated row: {exc}")
            assert all(item[f] is not None for f in ("entity_id", "currency", "amount_minor", "transaction_date"))
            assert item["entity_id"] in BY_ID, (source.source_id, item["entity_id"])
            mapped_totals[(source.landing_table, str(item["currency"]))] += int(item["amount_minor"])
    control = expected(b.rows)
    for (table, currency), total in mapped_totals.items():
        if table in {"landing.tax_documents"}:
            continue  # the default source reads committed documents only; the control total counts every document
        assert total == control[(table, currency)][1], (table, currency)


def test_fx_rates_cover_every_date_the_generator_emits() -> None:
    """Journaling converts each event at its date's rate; a date past the rates would fail the run."""
    from datetime import date, datetime

    for profile in ("tiny", "small"):
        b = generate(11, PROFILES[profile], "rates")
        last_rate = max(r["rate_date"] for r in b.rows["ref.fx_rates"])
        latest = max(
            (v.date() if isinstance(v, datetime) else v)
            for table, rows in b.rows.items()
            if table.startswith("landing.")
            for row in rows
            for v in row.values()
            if isinstance(v, date)
        )
        assert latest <= last_rate, (profile, latest, last_rate)
