"""Alembic environment: runs as the migrator role, using the PG* environment and a credential provider."""

from __future__ import annotations

from alembic import context

from close_platform.db.credentials import StaticCredentialProvider
from close_platform.db.engine import SCHEMA, make_engine


def run_migrations_online() -> None:
    engine = make_engine(StaticCredentialProvider.from_env(), pool_size=1, max_overflow=0)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            version_table_schema=SCHEMA,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("offline migrations are not supported; run against a database")
run_migrations_online()
