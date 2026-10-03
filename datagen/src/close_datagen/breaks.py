"""Break injection by exact quota: for each break type, exactly ``round(rate x eligible)`` rows.

The eligible rows are ranked by a seeded hash of the break type and the row's key, and the first N are
chosen, so the count is exact (the gate's 10 percent band holds by construction), the choice does not
depend on partitioning, and a different seed chooses different rows. Breaks that change what an order's
capture looks like (split tender, partial capture, rounding, FX difference) are mutually exclusive, which
keeps the answer key unambiguous.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from close_datagen.plan import Order
from close_datagen.profiles import Profile
from close_datagen.rng import Stream, rank

CAPTURE_BREAKS = ("split_tender", "partial_capture", "rounding", "fx_difference")


def quota[T](seed: int, break_type: str, rate: float, eligible: Iterable[T], key: Callable[[T], str]) -> list[T]:
    items = list(eligible)
    count = round(rate * len(items))
    return sorted(items, key=lambda item: rank(seed, break_type, key(item)))[:count]


def apply_order_breaks(seed: int, profile: Profile, orders: Sequence[Order]) -> dict[str, int]:
    """Choose and record the order-level breaks on each order's ``breaks`` dict; returns counts by type."""
    live = [o for o in orders if not o.cancelled]
    counts: dict[str, int] = {}
    capture_taken: set[str] = set()
    for break_type in CAPTURE_BREAKS:
        eligible = [o for o in live if o.order_id not in capture_taken]
        if break_type == "fx_difference":
            eligible = [o for o in eligible if o.cross_border]
        chosen = quota(seed, break_type, profile.rate(break_type), eligible, lambda o: o.order_id)
        for o in chosen:
            s = Stream(seed, break_type, o.order_id)
            if break_type == "split_tender":
                o.store_credit_minor = max(100, round(o.total_minor * (0.1 + 0.4 * s.uniform())) // 100 * 100)
                o.breaks[break_type] = o.store_credit_minor
            elif break_type == "partial_capture":
                o.breaks[break_type] = max(100, round(o.card_amount_minor * (0.3 + 0.4 * s.uniform())))
            elif break_type == "rounding":
                o.breaks[break_type] = s.pick((-1, 1))
            else:  # fx_difference: the provider settles at a rate this many basis points off the reference
                o.breaks[break_type] = s.pick((-1, 1)) * s.between(40, 150)
            capture_taken.add(o.order_id)
        counts[break_type] = len(chosen)

    delivered = [o for o in live if o.delivery_date is not None]
    partial = quota(seed, "partial_refund", profile.rate("partial_refund"), delivered, lambda o: o.order_id)
    for o in partial:
        o.breaks["partial_refund"] = max(100, o.item_price_minor // 5)
    taken = {o.order_id for o in partial}
    claw = quota(
        seed,
        "clawback",
        profile.rate("clawback"),
        [o for o in delivered if o.order_id not in taken],
        lambda o: o.order_id,
    )
    for o in claw:
        o.breaks["clawback"] = o.card_amount_minor  # a full refund after the seller was paid
    counts["partial_refund"], counts["clawback"] = len(partial), len(claw)

    card = [o for o in live if o.card_amount_minor > 0]
    disputes = quota(seed, "chargeback", profile.rate("chargeback"), card, lambda o: o.order_id)
    wins = {o.order_id for o in quota(seed, "late_win", profile.rate("late_win"), disputes, lambda o: o.order_id)}
    for o in disputes:
        o.breaks["chargeback"] = "late_win" if o.order_id in wins else "lost"
        if o.order_id in wins:
            o.breaks["late_win"] = True
    counts["chargeback"], counts["late_win"] = len(disputes), len(wins)

    stripe = [o for o in live if o.psp == "stripe"]
    invoiced = quota(seed, "fees_invoiced", profile.rate("fees_invoiced"), stripe, lambda o: o.order_id)
    for o in invoiced:
        o.breaks["fees_invoiced"] = True
    counts["fees_invoiced"] = len(invoiced)

    # A duplicate must arrive under a new identity with the same content (the platform deduplicates by source
    # identity), which is what an upstream double-post in the seller-payable ledger looks like.
    with_sale = [o for o in live if o.trigger_date is not None]
    duplicates = quota(seed, "duplicate", profile.rate("duplicate"), with_sale, lambda o: o.order_id)
    for o in duplicates:
        o.breaks["duplicate"] = True
    counts["duplicate"] = len(duplicates)

    shipped = [o for o in live if o.ship_date is not None]
    corrections = quota(seed, "carrier_correction", profile.rate("carrier_correction"), shipped, lambda o: o.order_id)
    for o in corrections:
        s = Stream(seed, "carrier_correction", o.order_id)
        o.breaks["carrier_correction"] = (s.pick(("address_correction", "weight_adjustment")), s.between(300, 2500))
    counts["carrier_correction"] = len(corrections)
    return counts
