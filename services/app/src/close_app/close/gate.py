"""What a hard close waits for (Stage 8b, ADR-0020), read inside the close's own transaction.

A period without a generated checklist closes as before (Stage 7a). One with a checklist closes only from
``soft_closed``, and only when every task is done and no check that read the GL predates the ledger's last change.

- **The ledger's last change** for an entity is the latest of these Postgres times:
  - a journal reaching ``posted`` (set when its run is settled, after its Delta commit);
  - a journaling run settled with lines or rollups written;
  - an ERP extract accepted.

  For the flux review, a later flux explanation counts too. A check records Postgres time *before* it starts
  reading, so a change during the read makes it stale, never the other way round.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import Connection, RowMapping, text

from close_contracts.config.close import GL_CHECKS

_CHECKLIST: Final = text("SELECT 1 FROM close_checklists WHERE entity_id = :e AND period = :p")
_TASKS: Final = text("SELECT * FROM close_tasks WHERE entity_id = :e AND period = :p ORDER BY position")
LEDGER_CHANGED: Final = text(
    "SELECT max(at) FROM ("
    " SELECT max(posted_at) AS at FROM journals WHERE entity_id = :e"
    " UNION ALL SELECT max(reconciled_at) FROM journaling_runs WHERE entity_id = :e AND status = 'reconciled'"
    "   AND coalesce(lines_written, 0) + coalesce(rollups_written, 0) > 0"
    " UNION ALL SELECT max(uploaded_at) FROM erp_trial_balances WHERE entity_id = :e AND status = 'accepted'"
    ") AS changes"
)
_EXPLAINED: Final = text("SELECT max(explained_at) FROM flux_explanations WHERE entity_id = :e AND period = :p")


def has_checklist(tx: Connection, entity_id: str, period: str) -> bool:
    return tx.execute(_CHECKLIST, {"e": entity_id, "p": period}).first() is not None


def stale(task: RowMapping, ledger_changed: datetime | None, explained: datetime | None) -> bool:
    """Whether a done or prepared automatic task's check read the GL before its latest change."""
    if task["check_name"] not in GL_CHECKS or task["checked_at"] is None:
        return False
    latest = [t for t in (ledger_changed, explained if task["check_name"] == "flux_reviewed" else None) if t]
    return bool(latest) and max(latest) > task["checked_at"]


def blockers(tx: Connection, entity_id: str, period: str) -> list[str]:
    """Every reason the period's checklist keeps it open; empty when the close may go ahead."""
    tasks = list(tx.execute(_TASKS, {"e": entity_id, "p": period}).mappings())
    changed = tx.execute(LEDGER_CHANGED, {"e": entity_id}).scalar()
    explained = tx.execute(_EXPLAINED, {"e": entity_id, "p": period}).scalar()
    reasons = [f"{t['task_id']} is {t['status']}" for t in tasks if t["status"] != "done"]
    reasons += [
        f"{t['task_id']} was checked before the ledger last changed: check it again"
        for t in tasks
        if t["status"] == "done" and stale(t, changed, explained)
    ]
    return reasons
