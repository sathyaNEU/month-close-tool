"""Generator profiles: how much data, over which dates, and how often each break is injected.

``tiny`` is for unit tests, ``small`` (about 10,000 orders over 30 days) is the end-to-end and determinism
profile, and ``medium`` (about 100,000 orders, roughly a million landing rows) is the performance profile.
``large`` runs on Databricks only and is not defined here.

Break rates are exact quotas over each break's eligible rows (``round(rate x eligible)``), so a profile's
break counts are fixed by the seed and the rates. The ``small`` rates are set so every break type appears
at least 25 times.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from typing import Final

BREAK_TYPES: Final = (
    "timing",  # a bank credit or debit lands outside its usual value-date window
    "partial_capture",  # one order captured in two parts
    "partial_refund",  # part of a delivered order refunded
    "split_tender",  # part of the order paid with store credit
    "fx_difference",  # a cross-border capture settles at a rate off the reference rate
    "fees_invoiced",  # processing fees invoiced monthly instead of netted from the payout
    "chargeback",  # a dispute debits the payout
    "late_win",  # a lost dispute is won back later
    "duplicate",  # the same source row arrives twice
    "rounding",  # a one-minor-unit difference between the order and its capture
    "clawback",  # a refund issued after the seller was paid
    "unclaimed_payout",  # a seller payout is returned or never collected
    "carrier_correction",  # the carrier bills a weight or address correction on top of the label
)


@dataclass(frozen=True)
class Profile:
    name: str
    start: date
    days: int
    orders_per_day: MappingProxyType[str, int]
    sellers_per_entity: int
    break_rates: MappingProxyType[str, float] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        unknown = set(self.break_rates) - set(BREAK_TYPES)
        if unknown:
            raise ValueError(f"unknown break types: {sorted(unknown)}")
        if any(not 0 <= r <= 1 for r in self.break_rates.values()):
            raise ValueError("break rates are fractions between 0 and 1")

    def rate(self, break_type: str) -> float:
        return self.break_rates.get(break_type, 0.0)


_RATES: Final = MappingProxyType(
    {
        "timing": 0.12,
        "partial_capture": 0.01,
        "partial_refund": 0.01,
        "split_tender": 0.03,
        "fx_difference": 0.30,
        "fees_invoiced": 0.01,
        "chargeback": 0.012,
        "late_win": 0.35,
        "duplicate": 0.005,
        "rounding": 0.005,
        "clawback": 0.008,
        "unclaimed_payout": 0.005,
        "carrier_correction": 0.01,
    }
)

PROFILES: Final = MappingProxyType(
    {
        "tiny": Profile("tiny", date(2026, 7, 1), 6, MappingProxyType({"US01": 8, "EU01": 6, "UK01": 4}), 12, _RATES),
        "small": Profile(
            "small", date(2026, 7, 1), 30, MappingProxyType({"US01": 180, "EU01": 100, "UK01": 60}), 250, _RATES
        ),
        "medium": Profile(
            "medium", date(2026, 7, 1), 90, MappingProxyType({"US01": 600, "EU01": 350, "UK01": 250}), 2000, _RATES
        ),
    }
)
