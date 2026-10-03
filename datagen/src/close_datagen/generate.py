"""The whole generation, in memory: plan, breaks, landing rows, settlement, reference data, answer key."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import date, timedelta

from close_contracts.calendar import CALENDAR_END
from close_datagen.breaks import apply_order_breaks
from close_datagen.landing import (
    Builder,
    Row,
    add_dispute,
    add_labels,
    add_order,
    add_sellers,
    add_stored_value,
    add_tax,
)
from close_datagen.plan import build_plan
from close_datagen.profiles import Profile
from close_datagen.settlement import (
    adyen_flows,
    bank_flows,
    carrier_flows,
    fee_invoices,
    seller_flows,
    stripe_flows,
    tax_flows,
)
from close_datagen.world import CALENDARS, CURRENCIES, ENTITIES, fx_rates


def reference_tables(b: Builder, profile: Profile, rates: dict[tuple[date, str, str], object]) -> None:
    for code, exponent, name in CURRENCIES:
        b.add("ref.currencies", {"currency": code, "exponent": exponent, "name": name})
    for e in ENTITIES:
        b.add(
            "ref.entities",
            {
                "entity_id": e.entity_id,
                "name": e.name,
                "functional_currency": e.currency,
                "country": e.country,
                "timezone": e.timezone,
                "calendar_code": e.calendar,
            },
        )
    for (day, src, dst), rate in sorted(rates.items()):
        b.add(
            "ref.fx_rates",
            {
                "rate_date": day,
                "from_currency": src,
                "to_currency": dst,
                "rate_type": "daily_spot",
                "rate": rate,
                "version": 1,
                "source": "generator",
            },
        )
    first, last = (
        profile.start - timedelta(days=10),
        min(profile.start + timedelta(days=profile.days + 120), CALENDAR_END),
    )
    for code, calendar in sorted(CALENDARS.items()):
        for day, is_business, index in calendar.index_rows(first, last):
            b.add(
                "ref.calendars", {"calendar_code": code, "day": day, "is_business_day": is_business, "bd_index": index}
            )
    months = sorted({(profile.start + timedelta(days=d)).replace(day=1) for d in range(profile.days)})
    for e in ENTITIES:
        for month in months:
            following = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
            b.add(
                "ref.periods",
                {
                    "entity_id": e.entity_id,
                    "period": f"{month:%Y-%m}",
                    "start_date": month,
                    "end_date": following - timedelta(days=1),
                    "status": "open",
                    "status_version": 1,
                },
            )


ORDER_BATCH = 20_000


def generate(seed: int, profile: Profile, run_id: str, sink: Callable[[str, list[Row]], None] | None = None) -> Builder:
    """Generate a profile. With a ``sink``, each table is handed over and released as soon as nothing later reads
    it (the order below is chosen for that); without one, every row stays in the returned builder."""
    orders, pools = build_plan(seed, profile)
    apply_order_breaks(seed, profile, orders)
    rates = fx_rates(seed, profile.start, profile.days)
    b = Builder(seed, run_id, sink)
    add_sellers(b, pools, profile.start)
    write_only = ("landing.orders", "landing.order_events", "landing.disputes", "landing.stored_value_ledger")
    for n, o in enumerate(orders, start=1):
        add_order(b, o)
        add_labels(b, o)
        add_tax(b, o)
        add_dispute(b, o)
        add_stored_value(b, o)
        if n % ORDER_BATCH == 0:
            b.flush(*write_only)  # nothing later reads these, so they leave memory in batches
    b.flush("landing.sellers", *write_only)
    payouts = stripe_flows(b, orders, rates)
    fee_invoices(b)
    b.flush("landing.stripe_balance_transactions", "landing.stripe_payouts", "landing.psp_fee_invoices")
    payouts += adyen_flows(b, orders, rates, profile.start)
    b.flush("landing.adyen_settlement_details")
    fundings = seller_flows(b, orders, rates, profile.rate("unclaimed_payout"))
    b.flush("landing.seller_payables", "landing.payout_items", "landing.payout_funding")
    carrier_flows(b, orders)
    b.flush("landing.shipping_labels", "landing.carrier_invoice_lines")
    tax_flows(b)
    b.flush("landing.tax_documents", "landing.tax_remittances")
    bank_flows(b, payouts, fundings, profile.rate("timing"))
    b.flush("landing.bank_statement_lines")
    reference_tables(b, profile, rates)  # type: ignore[arg-type]
    return b


def break_counts(b: Builder) -> dict[str, int]:
    """Injected breaks by type, counting each affected business object once."""
    seen = {(r["break_type"], r["source_key"]) for r in b.rows["ops.injected_breaks"]}
    return dict(Counter(kind for kind, _ in seen))
