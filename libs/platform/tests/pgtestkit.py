"""Shared fixtures and helpers for control-plane tests against the test Postgres (compose.test.yaml,
profile pg, 127.0.0.1:55432). Conftests import the fixtures from here.

Tests run as the real application role (close_app), so privilege rules are exercised too; the
migrator role applies migrations once per session and truncates tables before each ``pg`` test.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, text

from close_platform.db.credentials import StaticCredentialProvider
from close_platform.db.engine import make_engine

REPO = Path(__file__).resolve().parents[3]
SECRETS = REPO / ".local" / "secrets"
ALEMBIC_INI = REPO / "services" / "app" / "migrations" / "alembic.ini"
TEST_PG = {"host": "127.0.0.1", "port": "55432", "dbname": "close", "sslmode": "disable"}
TABLES = (
    "role_assignments, runs, run_attempts, leases, lease_events, outbox, outbox_receipts, "
    "idempotency_keys, scheduler_tasks, audit_events, config_versions, accounting_periods, journal_counters, "
    "journal_batches, journal_imports, journals, journal_lines, journal_approvals, journal_history, delegations, "
    "attachments, notifications, posting_runs, journal_conversions, config_snapshots, journaling_runs, ledger_pins, "
    "erp_trial_balances, erp_trial_balance_lines, close_checklists, close_tasks, close_task_events, "
    "flux_explanations, flux_reviews"
)


def read_env(name: str) -> dict[str, str]:
    path = SECRETS / name
    if not path.exists():
        pytest.fail(f"{path} missing: run `make secrets`")
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def engine_for(env_file: str, **kwargs: int) -> Engine:
    env = read_env(env_file)
    return make_engine(
        StaticCredentialProvider(env["PGPASSWORD"]), overrides={**TEST_PG, "user": env["PGUSER"]}, **kwargs
    )


def insert_run(tx: Connection, resource: str = "smoke:default", run_id: uuid.UUID | None = None) -> uuid.UUID:
    rid = run_id or uuid.uuid4()
    tx.execute(
        text(
            "INSERT INTO runs (run_id, job_key, lease_resource, requested_by, params, state) "
            "VALUES (:id, 'smoke', :res, 'tester', '{}'::jsonb, 'requested')"
        ),
        {"id": rid, "res": resource},
    )
    return rid


def insert_attempt(tx: Connection, run_id: uuid.UUID, attempt: int, token: int, state: str = "pending") -> None:
    tx.execute(
        text(
            "INSERT INTO run_attempts (run_id, attempt, fencing_token, idempotency_token, state) "
            "VALUES (:id, :a, :t, :tok, :s)"
        ),
        {"id": run_id, "a": attempt, "t": token, "tok": f"tok-{run_id.hex}-{attempt}", "s": state},
    )


@pytest.fixture(scope="session")
def migrated() -> None:
    env = {**os.environ, **read_env("pg-migrator.env"), "PGHOST": TEST_PG["host"], "PGPORT": TEST_PG["port"]}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), "upgrade", "head"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"migrations failed (is `make up-pg` running?):\n{proc.stderr[-2000:]}")


@pytest.fixture(scope="session")
def engine(migrated: None) -> Iterator[Engine]:
    e = engine_for("pg-app.env", pool_size=10, max_overflow=30)
    yield e
    e.dispose()


@pytest.fixture(scope="session")
def admin_engine(migrated: None) -> Iterator[Engine]:
    e = engine_for("pg-migrator.env", pool_size=2)
    yield e
    e.dispose()


@pytest.fixture(autouse=True)
def clean_tables(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("pg") is None:
        return
    admin: Engine = request.getfixturevalue("admin_engine")
    with admin.begin() as tx:
        tx.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        tx.execute(text("UPDATE audit_chain_head SET last_seq = 0, last_hash = decode(repeat('00', 32), 'hex')"))
