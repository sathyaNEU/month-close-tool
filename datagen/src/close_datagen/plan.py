"""The order plan: one compact record per order, from which every landing table is derived.

Orders are drawn per (entity, day) from their own stream, so the plan is a pure function of the seed and
the profile. Lifecycle dates follow the program: the verified marketplace goes through authentication,
the other programs ship straight to the buyer. The seller-payable trigger follows the default policy on
the accounting-policy sheet (verification passed, shipped, shipped, delivered).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from close_datagen.profiles import Profile
from close_datagen.rng import Stream
from close_datagen.world import (
    BUYER_FEE_BPS,
    CROSS_BORDER_SHARE,
    ENTITIES,
    PROCESSING_FEE_BPS,
    PROGRAMS,
    Entity,
    Seller,
    sellers,
)

TRIGGER = {
    "verified_marketplace": "verified",
    "direct_ship": "shipped",
    "consignment": "shipped",
    "listings": "delivered",
}
SKUS = tuple(f"SKU-{n:05d}" for n in range(1, 501))


@dataclass
class Order:
    order_id: str
    entity: Entity
    day: int
    placed_at: datetime
    program: str
    buyer_id: str
    seller: Seller
    sku: str
    currency: str  # presentment
    jurisdiction: str
    item_price_minor: int
    buyer_fee_minor: int
    shipping_minor: int
    tax_minor: int
    incentive_minor: int
    seller_fee_minor: int
    seller_processing_fee_minor: int
    psp: str
    psp_ref: str  # Stripe PaymentIntent id or Adyen pspReference
    charge_ref: str  # Stripe ch_ id or Adyen merchant reference
    cancelled: bool
    verified: bool | None  # None outside the verified marketplace
    verify_date: date | None
    ship_date: date | None
    delivery_date: date | None
    return_close_date: date | None
    tracking: str
    store_credit_minor: int = 0
    breaks: dict[str, object] = field(default_factory=dict)

    @property
    def total_minor(self) -> int:
        return (
            self.item_price_minor + self.buyer_fee_minor + self.shipping_minor + self.tax_minor - self.incentive_minor
        )

    @property
    def card_amount_minor(self) -> int:
        return self.total_minor - self.store_credit_minor

    @property
    def placed_date(self) -> date:
        return self.placed_at.date()

    @property
    def cross_border(self) -> bool:
        return self.currency != self.entity.currency

    @property
    def trigger_date(self) -> date | None:
        """When the seller payable is created (None when the order never reaches its trigger)."""
        if self.cancelled or self.verified is False:
            return None
        return {"verified": self.verify_date, "shipped": self.ship_date, "delivered": self.delivery_date}[
            TRIGGER[self.program]
        ]

    @property
    def seller_net_minor(self) -> int:
        return self.item_price_minor - self.seller_fee_minor - self.seller_processing_fee_minor


def _price(s: Stream) -> int:
    """Item prices in whole currency units, log-normal around ~180 with a long tail, in minor units."""
    return max(25, min(5000, round(s.lognormal(5.2, 0.6)))) * 100


def _cross_border_currency(s: Stream, entity: Entity) -> str:
    others = [c for c in ("USD", "EUR", "GBP") if c != entity.currency]
    return s.pick(others)


def _lifecycle(
    s: Stream, program: str, placed: date
) -> tuple[bool, bool | None, date | None, date | None, date | None]:
    """(cancelled, verified, verify_date, ship_date, delivery_date)."""
    if s.chance(0.01):
        return True, None, None, None, None
    if program == "verified_marketplace":
        verify = placed + timedelta(days=s.between(2, 5))
        if s.chance(0.03):
            return False, False, verify, None, None
        ship = verify + timedelta(days=1)
        return False, True, verify, ship, ship + timedelta(days=s.between(2, 4))
    ship = placed + timedelta(days=s.between(1, 3))
    return False, None, None, ship, ship + timedelta(days=s.between(2, 5))


def orders_for_day(seed: int, profile: Profile, entity: Entity, day: int, pool: list[Seller]) -> list[Order]:
    s = Stream(seed, "orders", entity.entity_id, day)
    placed_day = profile.start + timedelta(days=day)
    base = profile.orders_per_day[entity.entity_id]
    count = max(1, round(base * (0.85 + 0.3 * s.uniform())))
    out = []
    for n in range(count):
        program = s.weighted([p for p, _ in PROGRAMS], [w for _, w in PROGRAMS])
        seller = pool[min(int(len(pool) * s.uniform() ** 1.6), len(pool) - 1)]  # a few sellers sell a lot
        currency = _cross_border_currency(s, entity) if s.chance(CROSS_BORDER_SHARE) else entity.currency
        jurisdiction, tax_bps = s.pick(entity.jurisdictions)
        price = _price(s)
        incentive = min(price // 10, 1000) if s.chance(0.05) else 0
        psp = s.weighted([p for p, _ in entity.psp_weights], [w for _, w in entity.psp_weights])
        cancelled, verified, verify, ship, delivered = _lifecycle(s, program, placed_day)
        seconds = s.below(86_400)
        order_id = f"O{entity.entity_id}{placed_day:%y%m%d}{n:05d}"
        token = s.token(14)
        out.append(
            Order(
                order_id=order_id,
                entity=entity,
                day=day,
                placed_at=datetime(placed_day.year, placed_day.month, placed_day.day, tzinfo=UTC)
                + timedelta(seconds=seconds),
                program=program,
                buyer_id=f"B{entity.entity_id}{s.below(200_000):06d}",
                seller=seller,
                sku=s.pick(SKUS),
                currency=currency,
                jurisdiction=jurisdiction,
                item_price_minor=price,
                buyer_fee_minor=price * BUYER_FEE_BPS // 10_000,
                shipping_minor=entity.shipping_minor,
                tax_minor=price * tax_bps // 10_000,
                incentive_minor=incentive,
                seller_fee_minor=price * seller.fee_bps // 10_000,
                seller_processing_fee_minor=price * PROCESSING_FEE_BPS // 10_000,
                psp=psp,
                psp_ref=f"pi_{token}" if psp == "stripe" else f"{8800000000000000 + s.below(10**14)}",
                charge_ref=f"ch_{token}" if psp == "stripe" else order_id,
                cancelled=cancelled,
                verified=verified,
                verify_date=verify,
                ship_date=ship,
                delivery_date=delivered,
                return_close_date=delivered + timedelta(days=3) if delivered else None,
                tracking=f"7{s.below(10**11):011d}",
            )
        )
    return out


def build_plan(seed: int, profile: Profile) -> tuple[list[Order], dict[str, list[Seller]]]:
    pools = {e.entity_id: sellers(seed, e, profile.sellers_per_entity) for e in ENTITIES}
    orders = [
        order
        for day in range(profile.days)
        for entity in ENTITIES
        for order in orders_for_day(seed, profile, entity, day, pools[entity.entity_id])
    ]
    return orders, pools
