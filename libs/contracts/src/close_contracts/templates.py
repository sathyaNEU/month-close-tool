"""Templates 1, 2, 3, 6, 9 and 10 as match sets over the default data sources (roadmap Stage 3).

A role is bound to one data source, so a template that spans several sources is several match sets:
Stripe and Adyen apart for templates 1 and 2, each payout source against each bank-statement format its
entities use for template 3, and two match sets for template 10 (tax documents are reconciled once to their
orders and once to the remittance that paid them, so each document needs its own state in each).

Tolerances and windows carry a business reason, never a fit to the generated data:

- **Template 1.** An order matches its card captures exactly in the presentment currency. Captures within one
  currency unit of the order are suggested for review as rounding.
- **Template 3.** A payout is matched to its bank credit when the bank value date is at most two business
  days after the expected arrival (Adyen: three after the payout is created, which includes its one day to
  arrive). A later credit with the same reference and amount is suggested, because the delay itself needs
  a look.
- **Template 6.** A seller payout that did not complete (returned or unclaimed) never matches.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from close_contracts.config.match_set import MatchSet
from close_contracts.default_sources import col, fn, lit, s

OWNERS: Final = {"preparer": "close.preparer", "reviewer": "close.reviewer"}
ENTITIES: Final = ("US01", "EU01", "UK01")


def _key(left: Any, right: Any) -> dict[str, Any]:
    return {"condition": "key_equals", "left": left, "right": right}


def _amount(left: Any, right: Any, mode: str = "exact", **tolerance: int) -> dict[str, Any]:
    return {"condition": "amount", "left": left, "right": right, "mode": mode, **tolerance}


def _window(left: Any, right: Any, before: int, after: int, calendar: str) -> dict[str, Any]:
    return {
        "condition": "date_window",
        "left": left,
        "right": right,
        "unit": "business_days",
        "before": before,
        "after": after,
        "calendar": calendar,
    }


_CURRENCY: Final = {"condition": "currency", "mode": "equal"}


def _rule(rule_id: str, name: str, shape: str, outcome: str, conditions: list[Any], **extra: Any) -> Any:
    """``shape`` is the kind and the sides, such as ``"1:M ab"``."""
    kind, sides = shape.split()
    return {
        "rule_id": rule_id,
        "name": name,
        "kind": kind,
        "sides": list(sides),
        "outcome": outcome,
        "conditions": conditions,
        **extra,
    }


def _set(match_set_id: str, name: str, entities: tuple[str, ...], sources: list[Any], rules: list[Any]) -> MatchSet:
    return MatchSet.model_validate(
        {
            "match_set_id": match_set_id,
            "name": name,
            "entity_ids": list(entities),
            "sources": sources,
            "pass_rules": rules,
            "owners": OWNERS,
        }
    )


def _bind(role: str, source_id: str, scope: Any = None) -> dict[str, Any]:
    return {"role": role, "source_id": source_id, **({"scope": scope} if scope is not None else {})}


def _eq(column: str, value: str) -> Any:
    return fn("eq", col(column), s(value))


def _t1(psp: str, source_id: str, capture_type: str, presentment: str, entities: tuple[str, ...]) -> MatchSet:
    keys = [_key(col("a.reference"), col("b.reference")), _key(col("a.currency"), col("b.text_2"))]
    group_by = {"group_by": [col("b.reference")]}
    return _set(
        f"t1_orders_{psp}",
        f"Template 1: orders to {psp.title()} captures",
        entities,
        [_bind("a", "orders", _eq("a.text_1", psp)), _bind("b", source_id, _eq("b.text_1", capture_type))],
        [
            _rule(
                "exact_captures",
                "Order equals its captures",
                "1:M ab",
                "auto",
                [*keys, _amount(col("a.amount_minor"), col(presentment))],
                **group_by,
            ),
            _rule(
                "rounding",
                "Order within one currency unit of its captures",
                "1:M ab",
                "suggested",
                [*keys, _amount(col("a.amount_minor"), col(presentment), "absolute", absolute_minor=100)],
                **group_by,
            ),
        ],
    )


def _t3(psp: str, bank: str, entity: str, calendar: str) -> MatchSet:
    stripe = psp == "stripe"
    payout = (
        _bind("a", "stripe_payouts")
        if stripe
        else _bind("a", "adyen_settlement_details", _eq("a.text_1", "MerchantPayout"))
    )
    credit = fn("and", _eq("b.text_2", "C"), fn("contains", fn("upper", col("b.description")), s(psp.upper())))
    keys = [
        _key(
            col("a.reference_2" if stripe else "a.reference_3"),
            fn("coalesce", col("b.reference_2"), col("b.reference")),
        ),
        _amount(col("a.amount_minor") if stripe else fn("neg", col("a.amount_minor")), col("b.amount_minor")),
        _CURRENCY,
    ]
    on_time = (
        _window(col("a.value_date"), col("b.value_date"), 1, 2, calendar)
        if stripe
        else _window(col("a.transaction_date"), col("b.value_date"), 0, 3, calendar)
    )
    return _set(
        f"t3_{psp}_{bank}_{entity.lower()}",
        f"Template 3: {psp.title()} payouts to {bank.removeprefix('bank_')} bank credits ({entity})",
        (entity,),
        [payout, _bind("b", bank, credit)],
        [
            _rule("on_time", "Payout credited on time", "1:1 ab", "auto", [*keys, on_time]),
            _rule("late", "Payout credited late", "1:1 ab", "suggested", keys),
        ],
    )


_DOC_PERIOD: Final = fn("substring", fn("cast_string", col("b.transaction_date")), lit(1, "bigint"), lit(7, "bigint"))

MATCH_SETS: Final[tuple[MatchSet, ...]] = (
    _t1("stripe", "stripe_balance_transactions", "charge", "b.number_3", ("US01", "UK01")),
    _t1("adyen", "adyen_settlement_details", "Settled", "b.number_1", ("EU01", "UK01")),
    _set(
        "t2_stripe_payouts",
        "Template 2: Stripe payouts to their balance transactions",
        ("US01", "UK01"),
        [
            _bind("a", "stripe_payouts"),
            _bind("b", "stripe_balance_transactions", fn("ne", col("b.text_1"), s("payout"))),
        ],
        [
            _rule(
                "payout_total",
                "Payout equals the net of its transactions",
                "1:M ab",
                "auto",
                [
                    _key(col("a.reference"), col("b.reference_3")),
                    _amount(col("a.amount_minor"), col("b.number_2")),
                    _CURRENCY,
                ],
                group_by=[col("b.reference_3")],
            )
        ],
    ),
    _set(
        "t2_adyen_batches",
        "Template 2: Adyen payouts to their settlement batches",
        ("EU01", "UK01"),
        [
            _bind("a", "adyen_settlement_details", _eq("a.text_1", "MerchantPayout")),
            _bind("b", "adyen_settlement_details", fn("ne", col("b.text_1"), s("MerchantPayout"))),
        ],
        [
            _rule(
                "batch_total",
                "Payout equals the net of its batch",
                "1:M ab",
                "auto",
                [
                    _key(col("a.entity_id"), col("b.entity_id")),
                    _key(col("a.number_2"), col("b.number_2")),
                    _amount(fn("neg", col("a.amount_minor")), col("b.amount_minor")),
                    _CURRENCY,
                ],
                group_by=[col("b.entity_id"), col("b.number_2")],
            )
        ],
    ),
    _t3("stripe", "bank_bai2", "US01", "US_FED"),
    _t3("stripe", "bank_mt940", "UK01", "UK_BANK"),
    _t3("adyen", "bank_camt053", "EU01", "TARGET2"),
    _t3("adyen", "bank_mt940", "UK01", "UK_BANK"),
    _set(
        "t6_seller_payouts",
        "Template 6: seller payouts to seller payables",
        ENTITIES,
        [_bind("a", "payout_items"), _bind("b", "seller_payables")],
        [
            _rule(
                "payout_batch",
                "Completed payout equals its batch of payables",
                "1:M ab",
                "auto",
                [
                    _key(col("a.reference"), col("b.reference")),
                    {"condition": "expression", "expr": _eq("a.text_1", "COMPLETED")},
                    _amount(col("a.amount_minor"), col("b.amount_minor")),
                    _CURRENCY,
                ],
                group_by=[col("b.reference")],
            )
        ],
    ),
    _set(
        "t9_carrier_invoices",
        "Template 9: shipping labels to carrier invoice lines",
        ENTITIES,
        [_bind("a", "shipping_labels"), _bind("b", "carrier_invoice_lines")],
        [
            _rule(
                "label_charges",
                "Label equals its billed lines",
                "1:M ab",
                "auto",
                [
                    _key(col("a.reference"), col("b.reference")),
                    _amount(col("a.amount_minor"), col("b.amount_minor")),
                    _CURRENCY,
                ],
                group_by=[col("b.reference")],
            )
        ],
    ),
    _set(
        "t10_order_tax",
        "Template 10: order tax to tax documents",
        ENTITIES,
        [_bind("a", "orders", fn("ne", col("a.text_3"), s("cancelled"))), _bind("b", "tax_documents")],
        [
            _rule(
                "order_document",
                "Order tax equals its document",
                "1:1 ab",
                "auto",
                [
                    _key(col("a.reference"), col("b.reference")),
                    _amount(col("a.number_1"), col("b.amount_minor")),
                    _CURRENCY,
                ],
            )
        ],
    ),
    _set(
        "t10_tax_remittance",
        "Template 10: tax remittances to tax documents",
        ENTITIES,
        [_bind("c", "tax_remittances"), _bind("b", "tax_documents")],
        [
            _rule(
                "period_total",
                "Remittance equals its period's documents",
                "1:M cb",
                "auto",
                [
                    _key(col("c.entity_id"), col("b.entity_id")),
                    _key(col("c.text_1"), col("b.text_1")),
                    _key(col("c.text_2"), _DOC_PERIOD),
                    _amount(col("c.amount_minor"), col("b.amount_minor")),
                    _CURRENCY,
                ],
                group_by=[col("b.entity_id"), col("b.text_1"), _DOC_PERIOD],
            )
        ],
    ),
)
BY_ID: Final = {m.match_set_id: m for m in MATCH_SETS}


@dataclass(frozen=True)
class Family:
    """The match sets one template's answer-key groups are scored against."""

    name: str
    template: int
    match_sets: tuple[str, ...]
    exclude_prefix: str | None = None  # answer-key group keys that belong to another family
    only_prefix: str | None = None

    def selects(self, group_key: str) -> bool:
        if self.only_prefix is not None:
            return group_key.startswith(self.only_prefix)
        return self.exclude_prefix is None or not group_key.startswith(self.exclude_prefix)


FAMILIES: Final = (
    Family("t1", 1, ("t1_orders_stripe", "t1_orders_adyen")),
    Family("t2", 2, ("t2_stripe_payouts", "t2_adyen_batches")),
    Family(
        "t3",
        3,
        (
            "t3_stripe_bank_bai2_us01",
            "t3_stripe_bank_mt940_uk01",
            "t3_adyen_bank_camt053_eu01",
            "t3_adyen_bank_mt940_uk01",
        ),
    ),
    Family("t6", 6, ("t6_seller_payouts",)),
    Family("t9", 9, ("t9_carrier_invoices",)),
    Family("t10_orders", 10, ("t10_order_tax",), exclude_prefix="T10-REM"),
    Family("t10_remittances", 10, ("t10_tax_remittance",), only_prefix="T10-REM"),
)
