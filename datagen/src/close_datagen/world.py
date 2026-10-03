"""The fixed world the generator runs in: entities, calendars, banks, providers, sellers, tax and FX.

Everything here is either constant or drawn from a stream keyed by the seed and a stable key, so the world
for a given seed is the same wherever it is built.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from close_contracts.calendar import BusinessCalendar
from close_contracts.default_sources import CARRIER_ACCOUNTS
from close_datagen.rng import Stream


@dataclass(frozen=True)
class Entity:
    entity_id: str
    name: str
    currency: str
    country: str
    timezone: str
    calendar: str
    bank_account: str
    bank: str
    bank_format: str  # bai2, camt053 or mt940
    psp_weights: tuple[tuple[str, float], ...]  # (stripe | adyen, weight)
    jurisdictions: tuple[tuple[str, int], ...]  # (jurisdiction, tax rate in basis points)
    shipping_minor: int


ENTITIES: Final = (
    Entity(
        "US01",
        "Platform Inc. (US)",
        "USD",
        "US",
        "America/New_York",
        "US_FED",
        "US01-OPER-001",
        "First National",
        "bai2",
        (("stripe", 1.0),),
        (("CA", 725), ("NY", 888), ("TX", 625), ("FL", 600), ("WA", 650), ("IL", 625)),
        1400,
    ),
    Entity(
        "EU01",
        "Platform B.V. (EU)",
        "EUR",
        "NL",
        "Europe/Amsterdam",
        "TARGET2",
        "EU01-OPER-001",
        "Europa Bank",
        "camt053",
        (("adyen", 1.0),),
        (("NL", 2100), ("DE", 1900), ("FR", 2000)),
        1200,
    ),
    Entity(
        "UK01",
        "Platform Ltd (UK)",
        "GBP",
        "GB",
        "Europe/London",
        "UK_BANK",
        "UK01-OPER-001",
        "Albion Bank",
        "mt940",
        (("adyen", 0.6), ("stripe", 0.4)),
        (("GB", 2000),),
        1000,
    ),
)
BY_ID: Final = {e.entity_id: e for e in ENTITIES}
CURRENCIES: Final = (("USD", 2, "US dollar"), ("EUR", 2, "Euro"), ("GBP", 2, "Pound sterling"))

HOLIDAYS_2026: Final = {
    "US_FED": [
        date(2026, m, d)
        for m, d in ((1, 1), (1, 19), (2, 16), (5, 25), (6, 19), (7, 3), (9, 7), (10, 12), (11, 11), (11, 26), (12, 25))
    ],
    "TARGET2": [date(2026, m, d) for m, d in ((1, 1), (4, 3), (4, 6), (5, 1), (12, 25), (12, 26))],
    "UK_BANK": [date(2026, m, d) for m, d in ((1, 1), (4, 3), (4, 6), (5, 4), (5, 25), (8, 31), (12, 25), (12, 28))],
}
CALENDARS: Final = {code: BusinessCalendar.of(code, holidays=days) for code, days in HOLIDAYS_2026.items()}

SELLER_LEVELS: Final = ((1, 0.35, 1000), (2, 0.30, 950), (3, 0.20, 900), (4, 0.10, 850), (5, 0.05, 800))
PROGRAMS: Final = (("verified_marketplace", 0.55), ("direct_ship", 0.20), ("consignment", 0.15), ("listings", 0.10))
STRIPE_ACCOUNT: Final = {"US01": "acct_1PlatformUS01", "UK01": "acct_1PlatformUK01"}
ADYEN_MERCHANT: Final = {"EU01": "PlatformEU01", "UK01": "PlatformUK01"}
ADYEN_COMPANY: Final = "PlatformGroup"
CARRIER: Final = "FDX"
CARRIER_ACCOUNT: Final = {entity: account for account, entity in CARRIER_ACCOUNTS.items()}
PROCESSING_FEE_BPS: Final = 300  # the seller-side processing fee
BUYER_FEE_BPS: Final = 200
PSP_FEE_BPS: Final = {"stripe": 290, "adyen": 250}
PSP_FEE_FIXED_MINOR: Final = {"stripe": 30, "adyen": 11}
CROSS_BORDER_SHARE: Final = 0.08


@dataclass(frozen=True)
class Seller:
    seller_id: str
    entity_id: str
    level: int
    fee_bps: int
    payout_provider: str


def sellers(seed: int, entity: Entity, count: int) -> list[Seller]:
    out = []
    for n in range(count):
        s = Stream(seed, "seller", entity.entity_id, n)
        level = s.weighted([lv for lv, _, _ in SELLER_LEVELS], [w for _, w, _ in SELLER_LEVELS])
        fee = next(bps for lv, _, bps in SELLER_LEVELS if lv == level)
        hyperwallet_share = 0.3 if entity.country == "US" else 0.8
        provider = "hyperwallet" if s.chance(hyperwallet_share) else "paypal"
        out.append(Seller(f"S{entity.entity_id}{n:06d}", entity.entity_id, level, fee, provider))
    return out


def fx_rates(seed: int, start: date, days: int) -> dict[tuple[date, str, str], Decimal]:
    """Daily spot rates (to_currency per one from_currency) with a small seeded drift, both directions.

    They run from five days before the start to 130 days after the end. That covers every date the generator
    emits: the latest is a late-won dispute's reversal, about 100 days after the last order. Extending the
    horizon only appends days to each pair's seeded stream, so the earlier rates never change."""
    base = {("EUR", "USD"): 1.08, ("GBP", "USD"): 1.27, ("EUR", "GBP"): 0.85}
    out: dict[tuple[date, str, str], Decimal] = {}
    for (src, dst), level in base.items():
        s = Stream(seed, "fx", src, dst)
        rate = level
        for d in range(-5, days + 130):
            rate *= 1 + (s.uniform() - 0.5) * 0.004
            day = start + timedelta(days=d)
            forward = Decimal(f"{rate:.6f}")
            out[(day, src, dst)] = forward
            inverse = Decimal(1) / forward  # spark-ban: allow SB008 Python Decimal
            out[(day, dst, src)] = inverse.quantize(Decimal("0.000001"))
    return out
