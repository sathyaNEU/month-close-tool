"""The period close's configuration (Stage 8b, ADR-0020): the close checklist and the flux rules, as data.

- **The close checklist.** A :class:`CloseChecklist` lists the tasks of every period's close, like BlackLine's task
  lists. Each :class:`CloseTask` has a due day counted in business days from the period end, the tasks it depends
  on, and either a preparer (a manual task) or a system check (an automatic task). A manual task needs a reviewer,
  who can't be its preparer. A task can be limited to some entities. One checklist is active at a time.
- **Due days** (:func:`due_date`). BD+1 is the first business day after the period end, and BD-1 the last
  business day in the period. There is no BD0.
- **Flux rules.** :class:`FluxRules` set, per account type with per-account overrides, when a change against the
  prior period needs an explanation: an amount per functional currency and a percent, joined by ``and`` or ``or``
  (BlackLine's ``{amount, percent, operator}`` shape). Balance-sheet accounts compare closing balances, and
  income-statement accounts compare the period's activity (:func:`measure_of`). One set is active at a time.
- **Flux lines** (:func:`flux_line`) are computed from those rules, and :func:`flux_hash` binds a review to them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from close_contracts.canonical_json import content_sha256
from close_contracts.config.envelope import PRINCIPAL, SLUG
from close_contracts.config.journal_workflow import CURRENCY, AccountType
from close_contracts.config.journaling import ACCOUNT_CODE, ENTITY, NAME

CloseCheck = Literal["journals_settled", "suspense_clear", "gl_balanced", "erp_ties", "flux_reviewed"]
CLOSE_CHECKS: Final[tuple[CloseCheck, ...]] = (
    "journals_settled",
    "suspense_clear",
    "gl_balanced",
    "erp_ties",
    "flux_reviewed",
)
# The checks that read the GL, so a later change to the ledger makes their result stale.
GL_CHECKS: Final[frozenset[str]] = frozenset({"suspense_clear", "gl_balanced", "erp_ties", "flux_reviewed"})
ACCOUNT_TYPES: Final[tuple[AccountType, ...]] = ("asset", "liability", "equity", "revenue", "expense")
MAX_DUE_DAYS: Final = 20
Measure = Literal["closing", "activity"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------------------------- the close checklist


class CloseTask(_Strict):
    task_id: str = Field(pattern=NAME)
    name: str = Field(min_length=1, max_length=200)
    instructions: str = Field(default="", max_length=4000)
    due_day: int = Field(ge=-MAX_DUE_DAYS, le=MAX_DUE_DAYS)  # business days from the period end; never 0
    depends_on: tuple[str, ...] = Field(default=(), max_length=20)
    preparer: str | None = Field(default=None, pattern=PRINCIPAL)  # a manual task's preparer (a user id)
    reviewer: str | None = Field(default=None, pattern=PRINCIPAL)
    check: CloseCheck | None = None  # an automatic task's system check
    entities: tuple[str, ...] = Field(default=(), max_length=50)  # empty: every entity in the entity set

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.due_day == 0:
            raise ValueError("due_day counts business days from the period end (-1 the last in it), so there is no BD0")
        if (self.preparer is None) == (self.check is None):
            raise ValueError("a task has either a preparer (a manual task) or a check (an automatic task)")
        if self.check is None and self.reviewer is None:
            raise ValueError("a manual task needs a reviewer")
        if self.reviewer is not None and self.reviewer == self.preparer:
            raise ValueError("a task's reviewer can't be its preparer")
        if len(set(self.depends_on)) != len(self.depends_on) or self.task_id in self.depends_on:
            raise ValueError("a task depends on other tasks, each once")
        bad = [e for e in self.entities if not re.fullmatch(ENTITY, e)]
        if bad or len(set(self.entities)) != len(self.entities):
            raise ValueError(f"entities must be distinct entity ids: {bad}")
        return self

    def applies_to(self, entity_id: str) -> bool:
        return not self.entities or entity_id in self.entities


class TaskReminders(_Strict):
    escalate_after_days: int = Field(default=2, ge=1, le=30)  # days overdue before the controller is told


class CloseChecklist(_Strict):
    checklist_id: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=200)
    tasks: tuple[CloseTask, ...] = Field(min_length=1, max_length=200)
    reminders: TaskReminders = TaskReminders()

    @model_validator(mode="after")
    def _graph(self) -> Self:
        by_id = {t.task_id: t for t in self.tasks}
        if len(by_id) != len(self.tasks):
            raise ValueError("task ids must be unique")
        for task in self.tasks:
            unknown = [d for d in task.depends_on if d not in by_id]
            if unknown:
                raise ValueError(f"task {task.task_id} depends on unknown tasks: {', '.join(unknown)}")
            for dep in (by_id[d] for d in task.depends_on):
                if dep.entities and not (task.entities and set(task.entities) <= set(dep.entities)):
                    raise ValueError(f"task {task.task_id} applies to entities its dependency {dep.task_id} doesn't")
        topological(self.tasks)  # raises on a cycle
        return self

    def tasks_for(self, entity_id: str) -> list[CloseTask]:
        return [t for t in self.tasks if t.applies_to(entity_id)]


def topological(tasks: Sequence[CloseTask]) -> list[str]:
    """The task ids, each after the tasks it depends on (ties in listed order); a ValueError on a cycle."""
    remaining = {t.task_id: set(t.depends_on) for t in tasks}
    order: list[str] = []
    while remaining:
        ready = [t.task_id for t in tasks if t.task_id in remaining and not remaining[t.task_id]]
        if not ready:
            raise ValueError(f"the tasks' dependencies form a cycle: {', '.join(sorted(remaining))}")
        for task_id in ready:
            order.append(task_id)
            del remaining[task_id]
        for deps in remaining.values():
            deps.difference_update(ready)
    return order


def due_date(ends_on: date, day: int, business: Mapping[date, bool]) -> date | None:
    """The date ``day`` business days from the period end ``ends_on``.

    BD-1 is the last business day on or before ``ends_on`` and BD+1 the first after it. ``business`` holds the
    days a calendar covers, and whether each is a business day. None when the count runs past them."""
    if day == 0:
        raise ValueError("there is no BD0")
    step = timedelta(days=1 if day > 0 else -1)
    d = ends_on + timedelta(days=1) if day > 0 else ends_on
    counted = 0
    while d in business:
        if business[d]:
            counted += 1
            if counted == abs(day):
                return d
        d += step
    return None


def calendar_window(ends_on: date, days: Iterable[int]) -> tuple[date, date]:
    """Calendar days around ``ends_on`` that hold every due day in ``days``, with room for holidays."""
    listed = list(days)
    back = max([-d for d in listed if d < 0], default=1)
    ahead = max([d for d in listed if d > 0], default=1)
    return ends_on - timedelta(days=2 * back + 14), ends_on + timedelta(days=2 * ahead + 14)


# ---------------------------------------------------------------------------------------------- flux


class FluxThreshold(_Strict):
    """A change needs an explanation when it reaches the amount and (or, with ``or``) the percent."""

    amounts: dict[str, int]  # functional currency -> minor units
    percent: Decimal = Field(ge=0, le=10_000, decimal_places=2)
    operator: Literal["and", "or"] = "and"

    @model_validator(mode="after")
    def _amounts(self) -> Self:
        bad = [c for c in self.amounts if not re.fullmatch(CURRENCY, c)]
        if bad or not self.amounts:
            raise ValueError(f"amounts are per ISO currency, at least one: {bad}")
        if any(a < 0 for a in self.amounts.values()):
            raise ValueError("amounts can't be negative")
        return self

    def exceeded(self, change: int, comparison: int, currency: str) -> bool:
        """Whether a change needs an explanation. Any change in a currency with no amount does."""
        if change == 0:
            return False
        amount = self.amounts.get(currency)
        if amount is None:
            return True
        over_amount = abs(change) >= amount
        over_percent = comparison == 0 or Decimal(abs(change)) * 100 >= self.percent * abs(comparison)
        return (over_amount and over_percent) if self.operator == "and" else (over_amount or over_percent)


class FluxRules(_Strict):
    rules_id: str = Field(pattern=SLUG)
    basis: Literal["prior_period"] = "prior_period"
    by_type: dict[AccountType, FluxThreshold]
    accounts: dict[str, FluxThreshold] = {}  # overrides, by account code

    @model_validator(mode="after")
    def _covered(self) -> Self:
        missing = [t for t in ACCOUNT_TYPES if t not in self.by_type]
        if missing:
            raise ValueError(f"every account type needs a threshold: {', '.join(missing)}")
        bad = [a for a in self.accounts if not re.fullmatch(ACCOUNT_CODE, a)]
        if bad:
            raise ValueError(f"overrides are by account code: {bad}")
        return self

    def threshold(self, account: str, account_type: str | None) -> FluxThreshold | None:
        override = self.accounts.get(account)
        if override is not None:
            return override
        return next((t for kind, t in self.by_type.items() if kind == account_type), None)

    def currencies(self) -> set[str]:
        return {c for t in [*self.by_type.values(), *self.accounts.values()] for c in t.amounts}


def measure_of(account_type: str | None) -> Measure:
    """Income-statement accounts compare the period's activity; the others compare closing balances."""
    return "activity" if account_type in ("revenue", "expense") else "closing"


@dataclass(frozen=True)
class FluxLine:
    account: str
    account_type: str | None
    measure: Measure
    current_minor: int
    comparison_minor: int
    change_minor: int
    percent: str | None  # the change against the comparison, two decimals; None when the comparison is zero
    needs_explanation: bool


def flux_line(
    account: str,
    account_type: str | None,
    current: int,
    comparison: int,
    *,
    threshold: FluxThreshold | None,
    currency: str,
) -> FluxLine:
    """One account's line. An account the rules don't cover (not in the chart) needs an explanation for any change."""
    change = current - comparison
    percent = None if comparison == 0 else str((Decimal(change) * 100 / abs(comparison)).quantize(Decimal("0.01")))
    needs = change != 0 if threshold is None else threshold.exceeded(change, comparison, currency)
    return FluxLine(account, account_type, measure_of(account_type), current, comparison, change, percent, needs)


def flux_hash(lines: Iterable[FluxLine], explanations: Mapping[str, str]) -> str:
    """What a flux review is bound to: every line, and the explanation each one needing it had."""
    ordered = sorted(lines, key=lambda line: line.account)
    return content_sha256([asdict(line) | {"explanation_id": explanations.get(line.account)} for line in ordered])
