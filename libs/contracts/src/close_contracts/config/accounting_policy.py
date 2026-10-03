"""The accounting policy per program: the machine-readable form of the controller's policy sheet.

``docs/domain/accounting-policy-sheet.md`` is what the controller signs; this model is what the
generator and, from Stage 6, the posting rules read. The free-text answers on the sheet (penalty
amounts, reserve basis, escheat jurisdictions) become configuration in the stages that need them.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator


class Program(StrEnum):
    VERIFIED_MARKETPLACE = "verified_marketplace"
    DIRECT_SHIP = "direct_ship"
    CONSIGNMENT = "consignment"
    LISTINGS = "listings"


class Trigger(StrEnum):
    PAYMENT = "payment"
    VERIFICATION_PASSED = "verification_passed"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    RETURN_WINDOW_CLOSED = "return_window_closed"


class ProgramModel(StrEnum):
    AGENT = "agent"
    OWNED_INVENTORY = "owned_inventory"


class ShippingTreatment(StrEnum):
    NET_AS_AGENT = "net_as_agent"
    GROSS = "gross"


class IncentiveTreatment(StrEnum):
    CONTRA_REVENUE = "contra_revenue"
    CONTRA_UP_TO_CUMULATIVE_THEN_MARKETING = "contra_up_to_cumulative_then_marketing"
    MARKETING = "marketing"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProgramPolicy(_Strict):
    program: Program
    trigger: Trigger
    model: ProgramModel
    shipping: ShippingTreatment
    incentives: IncentiveTreatment

    @model_validator(mode="after")
    def _verification(self) -> Self:
        if self.trigger == Trigger.VERIFICATION_PASSED and self.program != Program.VERIFIED_MARKETPLACE:
            raise ValueError("only the verified marketplace has a verification step")
        return self


class Presentation(_Strict):
    psp_receivable: Literal["cash_equivalent", "accounts_receivable"]
    processing_fees: Literal["cost_of_revenue", "sga"]
    chargebacks: Literal["reserve", "transaction_loss"]


class AccountingPolicy(_Strict):
    programs: tuple[ProgramPolicy, ...]
    presentation: Presentation

    @model_validator(mode="after")
    def _unique(self) -> Self:
        programs = [p.program for p in self.programs]
        if not programs or len(set(programs)) != len(programs):
            raise ValueError("each program appears at most once, and at least one is configured")
        return self
