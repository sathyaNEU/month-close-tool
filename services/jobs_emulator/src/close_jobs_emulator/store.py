"""Durable run state for the emulator (SQLite on a volume), so a restart never forgets a run.

State follows the Jobs API 2.2 model: ``state_v2`` is the ``status.state`` value, and the legacy
``life_cycle_state``/``result_state`` pair is kept alongside it, since the API returns both.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
  task_run_id       INTEGER UNIQUE,
  job_id            INTEGER NOT NULL,
  idempotency_token TEXT,
  token_created_at  REAL,
  job_parameters    TEXT NOT NULL,
  state_v2          TEXT NOT NULL,
  life_cycle_state  TEXT NOT NULL,
  result_state      TEXT,
  termination_code  TEXT,
  termination_type  TEXT,
  state_message     TEXT NOT NULL DEFAULT '',
  created_at        REAL NOT NULL,
  start_time        REAL,
  end_time          REAL,
  cancel_requested  INTEGER NOT NULL DEFAULT 0,
  stdout_path       TEXT,
  stderr_path       TEXT,
  exit_code         INTEGER
);
CREATE INDEX IF NOT EXISTS runs_token ON runs (idempotency_token);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO sqlite_sequence (name, seq) SELECT 'runs', 5000
  WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'runs');
"""
_UPDATABLE = frozenset(
    {
        "task_run_id",
        "state_v2",
        "life_cycle_state",
        "result_state",
        "termination_code",
        "termination_type",
        "state_message",
        "start_time",
        "end_time",
        "cancel_requested",
        "stdout_path",
        "stderr_path",
        "exit_code",
    }
)
TASK_RUN_OFFSET = 500_000  # task run ids differ from job run ids, as on Databricks


@dataclass(frozen=True)
class RunRow:
    run_id: int
    task_run_id: int
    job_id: int
    idempotency_token: str | None
    job_parameters: dict[str, str]
    state_v2: str
    life_cycle_state: str
    result_state: str | None
    termination_code: str | None
    termination_type: str | None
    state_message: str
    created_at: float
    start_time: float | None
    end_time: float | None
    cancel_requested: bool
    stdout_path: str | None
    stderr_path: str | None
    exit_code: int | None


def _row(r: sqlite3.Row) -> RunRow:
    return RunRow(
        run_id=r["run_id"],
        task_run_id=r["task_run_id"],
        job_id=r["job_id"],
        idempotency_token=r["idempotency_token"],
        job_parameters=json.loads(r["job_parameters"]),
        state_v2=r["state_v2"],
        life_cycle_state=r["life_cycle_state"],
        result_state=r["result_state"],
        termination_code=r["termination_code"],
        termination_type=r["termination_type"],
        state_message=r["state_message"],
        created_at=r["created_at"],
        start_time=r["start_time"],
        end_time=r["end_time"],
        cancel_requested=bool(r["cancel_requested"]),
        stdout_path=r["stdout_path"],
        stderr_path=r["stderr_path"],
        exit_code=r["exit_code"],
    )


class RunStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)

    def recover_orphans(self) -> int:
        """Runs that were active when the emulator stopped can no longer be watched: fail them
        as INTERNAL_ERROR, which makes the control plane quarantine the lease."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE runs SET state_v2 = 'TERMINATED', life_cycle_state = 'INTERNAL_ERROR', "
                "result_state = 'FAILED', "
                "termination_code = 'INTERNAL_ERROR', termination_type = 'INTERNAL_ERROR', "
                "state_message = 'orphaned by an emulator restart', end_time = ? "
                "WHERE state_v2 IN ('RUNNING', 'TERMINATING')",
                (time.time(),),
            )
            return cur.rowcount

    def find_by_token(self, token: str, ttl_s: float) -> RunRow | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM runs WHERE idempotency_token = ? AND token_created_at >= ? ORDER BY run_id DESC LIMIT 1",
                (token, time.time() - ttl_s),
            ).fetchone()
        return None if r is None else _row(r)

    def create(self, job_id: int, params: dict[str, str], token: str | None) -> RunRow:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO runs (job_id, idempotency_token, token_created_at, job_parameters, state_v2, "
                "life_cycle_state, created_at) VALUES (?, ?, ?, ?, 'QUEUED', 'QUEUED', ?)",
                (job_id, token, now if token else None, json.dumps(params, sort_keys=True), now),
            )
            run_id = int(cur.lastrowid or 0)
            self._conn.execute("UPDATE runs SET task_run_id = ? WHERE run_id = ?", (run_id + TASK_RUN_OFFSET, run_id))
        row = self.get(run_id)
        if row is None:
            raise RuntimeError(f"run {run_id} was not stored")
        return row

    def get(self, run_id: int) -> RunRow | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return None if r is None else _row(r)

    def get_by_task(self, task_run_id: int) -> RunRow | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM runs WHERE task_run_id = ?", (task_run_id,)).fetchone()
        return None if r is None else _row(r)

    def list(self, job_id: int | None, start_from: float | None, active_only: bool) -> list[RunRow]:
        sql = "SELECT * FROM runs WHERE 1 = 1"
        args: list[Any] = []
        if job_id is not None:
            sql += " AND job_id = ?"
            args.append(job_id)
        if start_from is not None:
            sql += " AND created_at >= ?"
            args.append(start_from)
        if active_only:
            sql += " AND state_v2 != 'TERMINATED'"
        sql += " ORDER BY run_id DESC"
        with self._lock:
            return [_row(r) for r in self._conn.execute(sql, args).fetchall()]

    def next_queued(self) -> RunRow | None:
        with self._lock:
            active = self._conn.execute(
                "SELECT count(*) FROM runs WHERE state_v2 IN ('RUNNING', 'TERMINATING')"
            ).fetchone()[0]
            if active:
                return None
            r = self._conn.execute("SELECT * FROM runs WHERE state_v2 = 'QUEUED' ORDER BY run_id LIMIT 1").fetchone()
        return None if r is None else _row(r)

    def update(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"not updatable: {sorted(unknown)}")
        cols = ", ".join(f"{k} = ?" for k in fields)  # names checked against _UPDATABLE, values are bound
        with self._lock:
            self._conn.execute(f"UPDATE runs SET {cols} WHERE run_id = ?", (*fields.values(), run_id))  # noqa: S608  # nosec B608

    def meta_get(self, key: str) -> str | None:
        with self._lock:
            r = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if r is None else str(r["value"])

    def meta_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
