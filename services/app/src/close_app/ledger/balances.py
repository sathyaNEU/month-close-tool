"""Balances and roll-forward from the whole Databricks GL (Stage 8a, ADR-0019).

- **Two sources,** read through the read path, so the caller's entities are always applied:
  - the engine's GL rollups (``ledger.gl_rollup_lines``, written by the journaling stand-in until the Spark job
    exists);
  - the posted manual journals (``ledger.journal_lines``).

  Only functional amounts are read. The stand-in always writes them. An account whose rollups have none is a 500,
  never a silent zero. (Sums skip nulls, so a null among an account's other rollups can't be seen this way; the
  writer's guarantee covers that.)
- **Opening** is everything before the period, by account; **activity** is the period's debits and credits; and
  **closing** is opening plus debits less credits. Each source takes one grouped query by account per range, well
  inside the read path's 1,000-group limit.
- **The roll-forward** reads each period's opening independently, so its check that each period opens at the
  previous period's close is a real one: a line in an odd period, or a null amount, breaks it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Final

from close_app.context import AppContext
from close_app.errors import Problem
from close_app.identity import Principal
from close_app.readpath import run_query
from close_contracts.query import Aggregate, Eq, Filter, QuerySpec, Range
from close_contracts.queryable import GL_ROLLUP_LINES, JOURNAL_LINES

SOURCES: Final = (
    (GL_ROLLUP_LINES.key, "functional_debit_minor", "functional_credit_minor"),
    (JOURNAL_LINES.key, "debit_minor", "credit_minor"),
)
MAX_PERIODS: Final = 24


@dataclass
class Movement:
    debits: int = 0
    credits: int = 0
    lines: int = 0

    @property
    def net(self) -> int:
        return self.debits - self.credits


@dataclass
class Movements:
    by_account: dict[str, Movement] = field(default_factory=dict)

    def add(self, account: str, debits: int, credits: int, lines: int) -> None:
        m = self.by_account.setdefault(account, Movement())
        m.debits, m.credits, m.lines = m.debits + debits, m.credits + credits, m.lines + lines


def next_period(period: str) -> str:
    year, month = int(period[:4]), int(period[5:])
    return f"{year + month // 12}-{month % 12 + 1:02d}"


def periods_between(first: str, last: str) -> list[str]:
    out, current = [], first
    while current <= last:
        out.append(current)
        if len(out) > MAX_PERIODS:
            raise Problem(400, "Too many periods", f"a roll-forward covers at most {MAX_PERIODS} periods")
        current = next_period(current)
    if not out:
        raise Problem(400, "Invalid period range", "the first period is after the last")
    return out


def movements(
    ctx: AppContext, p: Principal, entity_id: str, filters: tuple[Filter, ...], by: tuple[str, ...] = ("account",)
) -> dict[tuple[str, ...], Movement]:
    """Functional debits, credits and line counts, grouped by ``by``, over both sources."""
    out: dict[tuple[str, ...], Movement] = {}
    for table, debit, credit in SOURCES:
        spec = QuerySpec(
            table,
            filters=filters,
            group_by=by,
            aggregates=(
                Aggregate("sum", "debits", debit),
                Aggregate("sum", "credits", credit),
                Aggregate("count", "lines"),
            ),
            limit=1000,
        )
        for row in run_query(ctx, spec, p, [entity_id]).rows:
            if row["lines"] and (row["debits"] is None or row["credits"] is None):
                raise Problem(500, "A GL rollup has no functional amounts", f"{table} {[row[c] for c in by]}")
            m = out.setdefault(tuple(str(row[c]) for c in by), Movement())
            m.debits += int(row["debits"] or 0)
            m.credits += int(row["credits"] or 0)
            m.lines += int(row["lines"] or 0)
    return out


def _accounts(*groups: dict[tuple[str, ...], Movement]) -> list[str]:
    return sorted({key[0] for g in groups for key in g})


def balances(ctx: AppContext, p: Principal, entity_id: str, period: str) -> dict[str, Any]:
    before = movements(ctx, p, entity_id, (Range("period", None, period),))
    during = movements(ctx, p, entity_id, (Eq("period", period),))
    rows: list[dict[str, Any]] = []
    for account in _accounts(before, during):
        opening = before.get((account,), Movement()).net
        activity = during.get((account,), Movement())
        rows.append(
            {
                "account": account,
                "opening_minor": opening,
                "debits_minor": activity.debits,
                "credits_minor": activity.credits,
                "closing_minor": opening + activity.net,
                "lines": activity.lines,
            }
        )
    debits, credits = sum(r["debits_minor"] for r in rows), sum(r["credits_minor"] for r in rows)
    closing = sum(r["closing_minor"] for r in rows)
    return {
        "entity_id": entity_id,
        "period": period,
        "accounts": rows,
        "debits_minor": debits,
        "credits_minor": credits,
        "balanced": debits == credits and closing == 0,  # a balanced ledger's balances also sum to zero
    }


def roll_forward(ctx: AppContext, p: Principal, entity_id: str, first: str, last: str) -> dict[str, Any]:
    periods = periods_between(first, last)
    activity = movements(ctx, p, entity_id, (Range("period", first, next_period(last)),), by=("account", "period"))
    openings = {
        period: movements(ctx, p, entity_id, (Range("period", None, period),))
        for period in [*periods, next_period(last)]
    }
    out, breaks = [], []
    for period, following in zip(periods, [*periods[1:], next_period(last)], strict=True):
        accounts = _accounts(
            openings[period], openings[following], {(a,): m for (a, pr), m in activity.items() if pr == period}
        )
        rows = []
        for account in accounts:
            opening = openings[period].get((account,), Movement()).net
            moved = activity.get((account, period), Movement())
            closing = opening + moved.net
            reopened = openings[following].get((account,), Movement()).net
            if closing != reopened:
                breaks.append(
                    {"account": account, "period": period, "closing_minor": closing, "next_opening_minor": reopened}
                )
            rows.append(
                {
                    "account": account,
                    "opening_minor": opening,
                    "debits_minor": moved.debits,
                    "credits_minor": moved.credits,
                    "closing_minor": closing,
                }
            )
        out.append({"period": period, "accounts": rows})
    return {"entity_id": entity_id, "periods": out, "ties": not breaks, "breaks": breaks}


def trial_balance_rows(groups: Iterable[tuple[str, Movement]]) -> list[dict[str, Any]]:
    return [
        {"account": account, "debits": m.debits, "credits": m.credits, "lines": m.lines}
        for account, m in sorted(groups)
    ]
