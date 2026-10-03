"""Exchange rates over HTTP (Stage 7c, ADR-0018).

    GET /fx-rates/{from}/{to}/{date}?rate_type=daily_spot    the rate a journal dated that day would be converted at

Anyone who reads journals in some entity may ask, so a preparer can see the rate before submitting. Rates are
global reference data; the lookup is the narrow, typed one (never a query spec), outside any transaction.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request

from close_app.authz import Action, access
from close_app.context import AppContext
from close_app.errors import Problem
from close_app.guard import Caller, not_found
from close_app.routers.journals import lookup_rates, readable_entities
from close_contracts.fx import RATE_TYPES, RateKey, RateKeyError

router = APIRouter(prefix="/api/v1")
Currency = Annotated[str, Path(pattern=r"^[A-Z]{3}$")]


@router.get(
    "/fx-rates/{from_currency}/{to_currency}/{on}", openapi_extra=access(Action.JOURNAL_READ), name="get_fx_rate"
)
def get_fx_rate(
    request: Request,
    p: Caller,
    from_currency: Currency,
    to_currency: Currency,
    *,
    on: date,
    rate_type: Annotated[str, Query(pattern="^(" + "|".join(RATE_TYPES) + ")$")] = "daily_spot",
) -> dict[str, Any]:
    readable_entities(p, Action.JOURNAL_READ, None)  # a 403 for someone who reads no journals anywhere
    try:
        key = RateKey(from_currency, to_currency, on, rate_type)
    except RateKeyError as exc:
        raise Problem(422, "Invalid rate request", str(exc), kind="validation-failed") from None
    ctx: AppContext = request.app.state.ctx
    rate = lookup_rates(ctx, {key}).get(key)
    if rate is None:
        raise not_found(f"the {key.label()} rate")
    return rate.as_json()
