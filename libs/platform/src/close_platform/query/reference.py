"""A QueryBackend over in-memory rows, using the reference evaluator. It lets the app's tests exercise
the real engine (entity injection, cursors, version pinning, grouping limits) without Spark. Every
``load`` makes a new table version, a plan is served from the version it names, and every plan the
backend receives is recorded so tests can assert what reached it."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from close_contracts.query import QueryableTable, Row, Scalar
from close_contracts.query_reference import evaluate
from close_platform.query.engine import QueryPlan, TableMissingError


@dataclass
class ReferenceBackend:
    snapshots: dict[str, list[list[Row]]] = field(default_factory=dict)
    plans: list[QueryPlan] = field(default_factory=list)

    def load(self, key: str, rows: Sequence[Mapping[str, Scalar | None]]) -> int:
        """Store ``rows`` as the table's next version and return that version."""
        versions = self.snapshots.setdefault(key, [])
        versions.append([dict(r) for r in rows])
        return len(versions) - 1

    def current_version(self, table: QueryableTable) -> int | None:
        if not self.snapshots.get(table.key):
            raise TableMissingError(table.key)
        return len(self.snapshots[table.key]) - 1

    def fetch(self, plan: QueryPlan) -> list[Row]:
        self.plans.append(plan)
        versions = self.snapshots.get(plan.table.key, [[]])
        rows = versions[-1 if plan.version is None else plan.version]
        return evaluate(plan.spec, plan.table, rows, entities=plan.entities, after=plan.after, fetch=plan.fetch)
