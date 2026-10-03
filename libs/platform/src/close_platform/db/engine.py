"""SQLAlchemy engine for the control-plane database (local Postgres 17, Lakebase in production).

Connection settings come from the standard libpq variables (``PGHOST``, ``PGPORT``, ``PGDATABASE``,
``PGUSER``, ``PGSSLMODE``, ``PGAPPNAME``), which is exactly what a Databricks App receives when a
Lakebase database is attached, so the same code runs in both places. Only the password differs, and
it comes from a :class:`~close_platform.db.credentials.PgCredentialProvider` at connect time.

Every connection gets server-side timeouts, so a stuck query or a forgotten open transaction can
never hold locks (and with them, writer leases) indefinitely.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event, text

from close_platform.db.credentials import PgCredentialProvider

SCHEMA = "close"


def make_engine(
    provider: PgCredentialProvider,
    *,
    pool_size: int = 5,
    max_overflow: int = 5,
    statement_timeout_ms: int = 15_000,
    lock_timeout_ms: int = 5_000,
    idle_in_transaction_timeout_ms: int = 30_000,
    overrides: Mapping[str, str] | None = None,
) -> Engine:
    """Create the engine. Host, port, database, user and SSL mode are read from the PG* environment.

    ``overrides`` (libpq keywords such as ``host``, ``port``, ``user``, ``dbname``) exist for tests that
    need two roles in one process; applications rely on the environment only.
    """
    engine = create_engine(
        "postgresql+psycopg://",
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
    )
    options = (
        f"-c statement_timeout={statement_timeout_ms} "
        f"-c lock_timeout={lock_timeout_ms} "
        f"-c idle_in_transaction_session_timeout={idle_in_transaction_timeout_ms} "
        f"-c search_path={SCHEMA} "
        "-c TimeZone=UTC"
    )

    @event.listens_for(engine, "do_connect")
    def _connect_args(_dialect: Any, _conn_rec: Any, _cargs: Any, cparams: dict[str, Any]) -> None:
        cparams.update(overrides or {})
        cparams["password"] = provider.password()
        cparams["options"] = options

    return engine


def db_now(conn: Connection) -> datetime:
    """Postgres ``now()``: the only clock used for comparisons (leases, schedules, expiry)."""
    value: datetime = conn.execute(text("SELECT now()")).scalar_one()
    return value
