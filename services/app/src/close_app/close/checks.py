"""The automatic tasks' checks (Stage 8b, ADR-0020), run on a user's request before the transaction that records
them, like the rate lookup: GL reads never happen inside a database transaction.

Each takes Postgres time first, before it reads anything, and returns whether it passed with its evidence:
- ``journals_settled``: no journal dated in the period waits for approval or posting (Postgres);
- ``suspense_clear``: the active chart's suspense account has no closing balance for the period (the GL);
- ``gl_balanced``: the period's balances balance (the GL);
- ``erp_ties``: the latest accepted ERP extract for the period ties to the GL, every account (the GL and Postgres);
- ``flux_reviewed``: the period's latest flux review is still current (the GL and Postgres).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from sqlalchemy import text

from close_app import workflow_configs
from close_app.close import flux
from close_app.context import AppContext
from close_app.errors import Problem
from close_app.identity import Principal
from close_app.ledger import erp
from close_app.ledger.balances import balances

_WAITING: Final = text(
    "SELECT number FROM journals WHERE entity_id = :e AND period = :p "
    "AND status IN ('pending_approval', 'approved', 'posting') ORDER BY number"
)


def run(
    ctx: AppContext, p: Principal, check: str, entity_id: str, period: str
) -> tuple[bool, dict[str, Any], datetime]:
    with ctx.engine.connect() as conn:
        started: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        chart = workflow_configs.context(conn, ("active",)).chart
        waiting = [int(n) for (n,) in conn.execute(_WAITING, {"e": entity_id, "p": period})]
    if check == "journals_settled":
        return not waiting, {"waiting": waiting[:50]}, started
    if check == "suspense_clear":
        if chart is None:
            return False, {"reason": "no chart of accounts is active"}, started
        found = balances(ctx, p, entity_id, period)
        closing = next((a["closing_minor"] for a in found["accounts"] if a["account"] == chart.suspense_account), 0)
        return closing == 0, {"account": chart.suspense_account, "closing_minor": closing}, started
    if check == "gl_balanced":
        found = balances(ctx, p, entity_id, period)
        evidence = {k: found[k] for k in ("debits_minor", "credits_minor", "balanced")}
        return bool(found["balanced"]), evidence, started
    if check == "erp_ties":
        try:
            tie = erp.tie_out(ctx, p, entity_id, period)
        except Problem as missing:
            if missing.status != 404:
                raise
            return False, {"reason": "no ERP trial balance has been accepted for the period"}, started
        evidence = {"extract_id": tie["extract"]["extract_id"], "version": tie["extract"]["version"]}
        return bool(tie["ties"]), evidence | {"differences": tie["differences"]}, started
    if check == "flux_reviewed":
        current = flux.compute(ctx, p, entity_id, period)
        review = current["review"]
        evidence = {"flux_hash": current["flux_hash"], "review_id": review["review_id"] if review else None}
        return bool(review and review["current"]), evidence, started
    raise ValueError(f"unknown check {check}")
