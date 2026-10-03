"""Flux analysis (Stage 8b, ADR-0020): each account against the prior period, explained and reviewed.

- **Lines.** Read through the read path from the whole Databricks GL (both sources, ADR-0019), so the caller's
  entities apply. Asset, liability and equity accounts compare closing balances, and revenue and expense accounts
  the period's activity. The active flux rules say which lines need an explanation. A line with nothing on either
  side is left out.
- **Explanations** are bound to the amounts the server computed when one was written, never to amounts a client
  sends. A line's latest explanation counts, and it is stale once the line's amounts differ from the ones it
  explained.
- **The review** signs off a period's flux once every line that needs one has a current explanation, and only by
  a reviewer who wrote none of them. It records :func:`close_contracts.config.close.flux_hash` of the lines and
  their explanations. The review is current while the flux still hashes the same, which is what the
  ``flux_reviewed`` check asks.
- **Insert-only.** Explanations and reviews are kept, and both are audited.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Any, Final

from sqlalchemy import Connection, RowMapping, text

from close_app import audit, close_configs, workflow_configs
from close_app.context import AppContext
from close_app.errors import Problem
from close_app.identity import Principal
from close_app.journals import periods
from close_app.ledger.balances import Movement, movements, next_period
from close_contracts.config.close import FluxLine, flux_hash, flux_line, measure_of
from close_contracts.query import Eq, Range

_EXPLANATIONS: Final = text(
    "SELECT DISTINCT ON (account) * FROM flux_explanations WHERE entity_id = :e AND period = :p "
    "ORDER BY account, explained_at DESC, explanation_id"
)
_REVIEW: Final = text(
    "SELECT * FROM flux_reviews WHERE entity_id = :e AND period = :p ORDER BY reviewed_at DESC, review_id LIMIT 1"
)
_PERIOD_SHARE: Final = text("SELECT status FROM accounting_periods WHERE entity_id = :e AND period = :p FOR SHARE")
_EXPLAIN: Final = text(
    "INSERT INTO flux_explanations (explanation_id, entity_id, period, account, measure, current_minor, "
    "comparison_minor, explanation, explained_by) VALUES (:id, :e, :p, :a, :m, :cur, :cmp, :x, :by) "
    "RETURNING explained_at"
)
_INSERT_REVIEW: Final = text(
    "INSERT INTO flux_reviews (review_id, entity_id, period, flux_hash, lines, explained, rules_id, rules_version, "
    "reviewed_by) VALUES (:id, :e, :p, :h, :n, :x, :r, :rv, :by) RETURNING reviewed_at"
)


def _iso(at: datetime) -> str:
    return at.isoformat()


def _current(explanation: RowMapping | None, line: FluxLine) -> bool:
    return explanation is not None and (
        explanation["measure"],
        int(explanation["current_minor"]),
        int(explanation["comparison_minor"]),
    ) == (line.measure, line.current_minor, line.comparison_minor)


def _nets(found: dict[tuple[str, ...], Movement]) -> dict[str, int]:
    return {key[0]: m.net for key, m in found.items()}


def compute(ctx: AppContext, p: Principal, entity_id: str, period: str) -> dict[str, Any]:
    """The period's flux: every line, its explanation, the latest review, and whether that review is current."""
    with ctx.engine.connect() as conn:
        rules = close_configs.active_flux_rules(conn)
        if rules is None:
            raise Problem(409, "No flux rules are active", kind="conflict")
        active = workflow_configs.context(conn, ("active",))
        entity = active.entities.entity(entity_id) if active.entities else None
        if entity is None:
            raise Problem(
                422, "Unknown entity", f"{entity_id} is not in the active entity set", kind="validation-failed"
            )
        explanations = {
            str(r["account"]): r for r in conn.execute(_EXPLANATIONS, {"e": entity_id, "p": period}).mappings()
        }
        review = conn.execute(_REVIEW, {"e": entity_id, "p": period}).mappings().first()
    model, rules_id, rules_version = rules
    prior = periods.previous(period)
    closing = _nets(movements(ctx, p, entity_id, (Range("period", None, next_period(period)),)))
    closing_before = _nets(movements(ctx, p, entity_id, (Range("period", None, period),)))
    activity = _nets(movements(ctx, p, entity_id, (Eq("period", period),)))
    activity_before = _nets(movements(ctx, p, entity_id, (Eq("period", prior),)))
    lines: list[FluxLine] = []
    for account in sorted(set(closing) | set(closing_before) | set(activity) | set(activity_before)):
        found = active.chart.account(account) if active.chart else None
        kind = found.account_type if found else None
        now, before = (closing, closing_before) if measure_of(kind) == "closing" else (activity, activity_before)
        current, comparison = now.get(account, 0), before.get(account, 0)
        if current == 0 and comparison == 0:
            continue
        lines.append(
            flux_line(
                account,
                kind,
                current,
                comparison,
                threshold=model.threshold(account, kind),
                currency=entity.functional_currency,
            )
        )
    explained = {
        line.account: str(explanations[line.account]["explanation_id"])
        for line in lines
        if line.needs_explanation and _current(explanations.get(line.account), line)
    }
    digest = flux_hash(lines, explained)
    out_lines = []
    for line in lines:
        x = explanations.get(line.account)
        listed = None
        if x is not None:
            listed = {
                "explanation_id": str(x["explanation_id"]),
                "explanation": x["explanation"],
                "explained_by": x["explained_by"],
                "explained_at": _iso(x["explained_at"]),
                "current": _current(x, line),
            }
        out_lines.append(asdict(line) | {"explanation": listed})
    needing = [line for line in lines if line.needs_explanation]
    return {
        "entity_id": entity_id,
        "period": period,
        "comparison_period": prior,
        "currency": entity.functional_currency,
        "rules": {"rules_id": rules_id, "version": rules_version},
        "lines": out_lines,
        "needing_explanation": len(needing),
        "explained": len(explained),
        "unexplained": sorted(line.account for line in needing if line.account not in explained),
        "explainers": sorted({str(explanations[a]["explained_by"]) for a in explained}),
        "flux_hash": digest,
        "review": None
        if review is None
        else {
            "review_id": str(review["review_id"]),
            "reviewed_by": review["reviewed_by"],
            "reviewed_at": _iso(review["reviewed_at"]),
            "flux_hash": review["flux_hash"],
            "current": review["flux_hash"] == digest,
        },
    }


def _period_takes_entries(tx: Connection, entity_id: str, period: str) -> None:
    status = tx.execute(_PERIOD_SHARE, {"e": entity_id, "p": period}).scalar()
    if status is None:
        raise Problem(404, "Not found", f"period {entity_id} {period}", kind="not-found")
    if status not in ("open", "soft_closed"):
        raise Problem(409, f"The period is {status}", f"{entity_id} {period}", kind="conflict")


def explain(
    tx: Connection,
    flux: dict[str, Any],
    account: str,
    explanation: str,
    *,
    p: Principal,
    request_id: str | None,
) -> dict[str, Any]:
    """Store an explanation of ``account``'s line, bound to the amounts in ``flux`` (computed by the server)."""
    entity_id, period = flux["entity_id"], flux["period"]
    _period_takes_entries(tx, entity_id, period)
    line = next((x for x in flux["lines"] if x["account"] == account), None)
    if line is None:
        raise Problem(
            409, "The account has no flux line", f"{account} has nothing to explain in {period}", kind="conflict"
        )
    explanation_id = uuid.uuid4()
    at = tx.execute(
        _EXPLAIN,
        {
            "id": explanation_id,
            "e": entity_id,
            "p": period,
            "a": account,
            "m": line["measure"],
            "cur": line["current_minor"],
            "cmp": line["comparison_minor"],
            "x": explanation,
            "by": p.user_id,
        },
    ).scalar_one()
    out = {
        "explanation_id": str(explanation_id),
        "entity_id": entity_id,
        "period": period,
        "account": account,
        "measure": line["measure"],
        "current_minor": line["current_minor"],
        "comparison_minor": line["comparison_minor"],
        "explanation": explanation,
        "explained_by": p.user_id,
        "explained_at": _iso(at),
    }
    audit.append(
        tx,
        audit.Event(
            p.user_id,
            "flux.explain",
            entity_ids=(entity_id,),
            target=f"flux:{entity_id}:{period}:{account}",
            payload={k: out[k] for k in ("explanation_id", "measure", "current_minor", "comparison_minor")},
        ),
        request_id,
    )
    return out


def review(tx: Connection, flux: dict[str, Any], *, p: Principal, request_id: str | None) -> dict[str, Any]:
    """Sign off the flux in ``flux``: every line that needs one explained, by people other than the reviewer."""
    entity_id, period = flux["entity_id"], flux["period"]
    _period_takes_entries(tx, entity_id, period)
    if flux["unexplained"]:
        detail = f"lines without a current explanation: {', '.join(flux['unexplained'])}"
        raise Problem(409, "Every line over its threshold needs an explanation", detail, kind="conflict")
    if p.user_id in flux["explainers"]:
        raise Problem(409, "A reviewer can't review their own explanations", p.user_id, kind="conflict")
    review_id = uuid.uuid4()
    at = tx.execute(
        _INSERT_REVIEW,
        {
            "id": review_id,
            "e": entity_id,
            "p": period,
            "h": flux["flux_hash"],
            "n": len(flux["lines"]),
            "x": flux["explained"],
            "r": flux["rules"]["rules_id"],
            "rv": flux["rules"]["version"],
            "by": p.user_id,
        },
    ).scalar_one()
    out = {
        "review_id": str(review_id),
        "entity_id": entity_id,
        "period": period,
        "flux_hash": flux["flux_hash"],
        "lines": len(flux["lines"]),
        "explained": flux["explained"],
        "reviewed_by": p.user_id,
        "reviewed_at": _iso(at),
    }
    audit.append(
        tx,
        audit.Event(
            p.user_id,
            "flux.review",
            entity_ids=(entity_id,),
            target=f"flux:{entity_id}:{period}",
            payload={k: out[k] for k in ("review_id", "flux_hash", "lines", "explained")},
        ),
        request_id,
    )
    return out
