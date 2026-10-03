"""Periodic tasks that are safe with any number of app instances (Stage 1 design rule 4).

Each task has a row in ``scheduler_tasks``. An instance runs a task only after winning a
compare-and-set that moves ``next_due_at`` forward (Postgres time), so each due tick is claimed by
exactly one instance. No advisory locks, no pinned connections: a pooled connection can silently lose
a session-level lock, while a claim row cannot be lost or held by a frozen process.

A task that crashes after claiming just misses that tick, so every task must be idempotent (and all
of ours are: they work through compare-and-set updates and receipts). The next tick is claimed by
whichever instance is alive, which gives failover within one interval.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import Engine, text

log = logging.getLogger(__name__)
# A claim holds the task's row lock between its UPDATE and COMMIT. If the process freezes in that gap
# (docker pause, a VM stall), Postgres ends the session after this long, so the survivor is delayed
# by at most this much rather than by the server-wide idle timeout.
_CLAIM_GUARDS = "SET LOCAL idle_in_transaction_session_timeout = '500ms'; SET LOCAL lock_timeout = '1s'"


@dataclass(frozen=True)
class PeriodicTask:
    name: str
    interval: timedelta
    fn: Callable[[], object]


class Scheduler:
    def __init__(self, engine: Engine, tasks: Sequence[PeriodicTask], *, instance_id: str) -> None:
        names = [t.name for t in tasks]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate task names: {names}")
        self._engine = engine
        self._tasks = list(tasks)
        self._me = instance_id

    def register(self) -> None:
        """Create or update the claim rows (safe to call from every instance at startup)."""
        with self._engine.begin() as tx:
            for task in self._tasks:
                tx.execute(
                    text(
                        "INSERT INTO scheduler_tasks (task, interval_s) VALUES (:t, :i) "
                        "ON CONFLICT (task) DO UPDATE SET interval_s = EXCLUDED.interval_s"
                    ),
                    {"t": task.name, "i": max(1, int(task.interval.total_seconds()))},
                )

    def _claim(self, task: PeriodicTask) -> int | None:
        with self._engine.begin() as tx:
            tx.execute(text(_CLAIM_GUARDS))
            row = tx.execute(
                text(
                    """
                    UPDATE scheduler_tasks
                       SET next_due_at = now() + make_interval(secs => interval_s), claim_seq = claim_seq + 1,
                           last_claimed_by = :me, last_claimed_at = now()
                     WHERE task = :t AND next_due_at <= now()
                    RETURNING claim_seq
                    """
                ),
                {"t": task.name, "me": self._me},
            ).first()
        return None if row is None else int(row.claim_seq)

    def _finished(self, task: PeriodicTask, error: str | None) -> None:
        with self._engine.begin() as tx:
            tx.execute(text(_CLAIM_GUARDS))
            tx.execute(
                text("UPDATE scheduler_tasks SET last_finished_at = now(), last_error = :e WHERE task = :t"),
                {"e": error, "t": task.name},
            )

    def tick(self) -> list[tuple[str, int]]:
        """Claim and run every due task once. Returns (task, claim_seq) for the ticks this instance ran."""
        ran = []
        for task in self._tasks:
            seq = self._claim(task)
            if seq is None:
                continue
            # One line per claimed tick: the e2e gate checks uniqueness and contiguity from these.
            log.debug("tick", extra={"task": task.name, "claim_seq": seq, "instance": self._me})
            error = None
            try:
                task.fn()
            except Exception as exc:  # a failing task must not stop the others
                error = f"{type(exc).__name__}: {exc}"[:2000]
                log.exception("scheduled task failed", extra={"task": task.name})
            self._finished(task, error)
            ran.append((task.name, seq))
        return ran

    def run_forever(
        self,
        stop: threading.Event,
        poll: timedelta = timedelta(seconds=1),
        reregister: timedelta = timedelta(minutes=1),
    ) -> None:
        """Tick until ``stop`` is set. Registration is retried until it works (a database that is down
        at boot is survivable) and then repeated every ``reregister``, so a claim row that disappears
        (a restored database, a manual cleanup) comes back instead of silently stopping its task."""
        registered_at: float | None = None
        while not stop.is_set():
            try:
                if registered_at is None or time.monotonic() - registered_at >= reregister.total_seconds():
                    self.register()
                    registered_at = time.monotonic()
                self.tick()
            except Exception:  # the database may be briefly unreachable; keep going
                log.exception("scheduler tick failed")
            stop.wait(poll.total_seconds())
