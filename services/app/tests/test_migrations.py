"""Migrations run up, down and up again cleanly, and the privilege rules survive the cycle (Gate 1a)."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from pgtestkit import ALEMBIC_INI, TEST_PG, read_env
from sqlalchemy import Engine, TextClause, text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from close_app.authz import Role
from close_contracts.config.envelope import ConfigKind

pytestmark = pytest.mark.pg

TABLES = {
    "role_assignments", "runs", "run_attempts", "leases", "lease_events", "outbox", "outbox_receipts",
    "idempotency_keys", "scheduler_tasks", "audit_chain_head", "audit_events", "config_versions",
    "accounting_periods", "journal_counters", "journal_batches", "journal_imports", "journals", "journal_lines",
    "journal_approvals", "journal_history", "delegations", "attachments", "notifications", "posting_runs",
    "journal_conversions", "config_snapshots", "journaling_runs", "ledger_pins", "erp_trial_balances",
    "erp_trial_balance_lines", "close_checklists", "close_tasks", "close_task_events", "flux_explanations",
    "flux_reviews",
}  # fmt: skip


def _alembic(*args: str) -> str:
    env = {**os.environ, **read_env("pg-migrator.env"), "PGHOST": TEST_PG["host"], "PGPORT": TEST_PG["port"]}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout + proc.stderr


def _tables(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'close'")).scalars()
        return set(rows) - {"alembic_version"}


def test_up_down_up_is_clean_and_keeps_the_privilege_rules(engine: Engine, admin_engine: Engine) -> None:
    try:
        _alembic("downgrade", "base")
        assert _tables(admin_engine) == set()
        _alembic("upgrade", "head")
        assert _tables(admin_engine) == TABLES
        _alembic("downgrade", "base")
        _alembic("upgrade", "head")
        assert _tables(admin_engine) == TABLES
    finally:
        _alembic("upgrade", "head")  # leave the session's database usable whatever happened
    engine.dispose()  # pooled connections may hold plans for the dropped tables
    with engine.begin() as tx:  # the app role can use the recreated tables...
        tx.execute(
            text(
                "INSERT INTO role_assignments (assignment_id, user_id, role, scope, granted_by) "
                "VALUES (gen_random_uuid(), 'u', 'auditor', 'US01', 't')"
            )
        )
        assert tx.execute(text("SELECT entity_id FROM principal_entities WHERE user_id = 'u'")).scalar_one() == "US01"
    for statement in (
        "UPDATE lease_events SET reason = 'x'",  # ...but the lease audit trail stays append-only
        "DELETE FROM lease_events",
        "UPDATE alembic_version SET version_num = '0000'",  # ...and it cannot move the schema version
        "UPDATE audit_events SET actor = 'x'",  # ...the audit log is append-only
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
        "DELETE FROM audit_chain_head",  # ...its head can only move forward through an update
        "INSERT INTO audit_chain_head VALUES (2, 0, decode(repeat('00', 32), 'hex'))",
        "DELETE FROM role_assignments",  # ...assignments are revoked, never deleted
        "DELETE FROM config_versions",  # ...configuration versions are never deleted
        "DELETE FROM journals",  # ...journals are cancelled, never deleted (Stage 7)
        "TRUNCATE journals CASCADE",
        "DELETE FROM accounting_periods",
        "DELETE FROM attachments",  # ...an attachment is marked removed, and kept
        "DELETE FROM delegations",  # ...a delegation is revoked, and kept
        "UPDATE journal_lines SET amount_minor = 1",  # ...lines, approvals and the history are append-only
        "UPDATE journal_approvals SET approver = 'x'",
        "UPDATE journal_history SET actor = 'x'",
        "UPDATE journal_imports SET status = 'created'",
        "DELETE FROM posting_runs",  # ...posting runs are kept (Stage 7b)
        "UPDATE journal_conversions SET rate = 2",  # ...a frozen conversion never changes (Stage 7c)
        "DELETE FROM journal_conversions",
        "UPDATE config_snapshots SET content = '{}'",  # ...a configuration snapshot never changes (Stage 8a)
        "DELETE FROM config_snapshots",
        "UPDATE ledger_pins SET inputs_digest = repeat('0', 64)",  # ...nor does a ledger pin
        "DELETE FROM journaling_runs",
        "UPDATE erp_trial_balance_lines SET balance_minor = 0",  # ...nor an uploaded ERP extract
        "DELETE FROM erp_trial_balances",
        "UPDATE close_checklists SET checklist_version = 2",  # ...a generated checklist is frozen (Stage 8b)
        "DELETE FROM close_tasks",  # ...tasks are reopened, never deleted
        "UPDATE close_task_events SET actor = 'x'",  # ...their history is append-only
        "UPDATE flux_explanations SET explanation = 'x'",  # ...as are flux explanations and reviews
        "DELETE FROM flux_reviews",
    ):
        with pytest.raises(ProgrammingError, match="permission denied"), engine.begin() as tx:
            tx.execute(text(statement))
    # Visibility comes from assignments only: the view cannot be written.
    with pytest.raises(DBAPIError, match=r"permission denied|cannot insert into view"), engine.begin() as tx:
        tx.execute(text("INSERT INTO principal_entities VALUES ('u', 'EU01')"))


def test_the_migrations_list_every_role_and_configuration_kind() -> None:
    """The CHECK constraints' catalogs equal the code's: the roles (0002), and the configuration kinds of the newest
    migration that sets them."""
    script = ScriptDirectory.from_config(AlembicConfig(str(ALEMBIC_INI)))
    governance = script.get_revision("0002")
    assert governance is not None
    assert set(governance.module.ROLES) == {r.value for r in Role}
    newest = next(s for s in script.walk_revisions() if hasattr(s.module, "KINDS"))  # head first
    assert set(newest.module.KINDS) == {k.value for k in ConfigKind}


def _version_of(kind: str) -> TextClause:
    return text(
        "INSERT INTO config_versions (kind, config_id, version, status, effective_from, spec, content_hash, "
        "entity_ids, authors, created_by) VALUES (:kind, 'sample_one', 1, 'draft', '2026-06-01', '{}', "
        "repeat('0', 64), '{}', '{t}', 't')"
    ).bindparams(kind=kind)


@pytest.mark.parametrize(("below", "kind"), [("0002", "event_type"), ("0003", "journal_template")])
def test_a_downgrade_removes_the_newer_kinds_and_the_upgrade_allows_them(
    admin_engine: Engine, below: str, kind: str
) -> None:
    """0003 added the Stage 6 journaling kinds and 0004 the Stage 7 workflow kinds."""
    try:
        with admin_engine.begin() as tx:
            tx.execute(_version_of(kind))
        _alembic("downgrade", below)
        with admin_engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM config_versions")).scalar_one() == 0
        with pytest.raises(IntegrityError, match="config_versions_kind_check"), admin_engine.begin() as tx:
            tx.execute(_version_of(kind))
        _alembic("upgrade", "head")
        with admin_engine.begin() as tx:
            tx.execute(_version_of(kind))
    finally:
        _alembic("upgrade", "head")


def test_entity_grants_survive_a_downgrade_and_upgrade(engine: Engine, admin_engine: Engine) -> None:
    """0002 moves the Stage 1 entity grants into role assignments, and its downgrade moves them back."""
    try:
        _alembic("downgrade", "0001")
        with admin_engine.begin() as tx:
            tx.execute(
                text("INSERT INTO principal_entities (user_id, entity_id, granted_by) VALUES ('g', 'EU01', 't')")
            )
        _alembic("upgrade", "head")
        with admin_engine.connect() as conn:
            rows = conn.execute(text("SELECT role, scope FROM role_assignments WHERE user_id = 'g'")).all()
        assert [tuple(r) for r in rows] == [("match_preparer", "EU01")]
    finally:
        _alembic("upgrade", "head")
    engine.dispose()
