"""Readiness enforces the schema head on the running app (Gate 1a)."""

from __future__ import annotations

import httpx
import pytest
from stacklib import BASE, psql, scalar, wait_until

pytestmark = pytest.mark.stack


def _ready() -> httpx.Response:
    return httpx.get(f"{BASE}/readyz", timeout=30)


def test_readiness_fails_while_the_schema_is_not_at_head_and_recovers() -> None:
    assert _ready().status_code == 200
    head = scalar("SELECT version_num FROM alembic_version")  # restored exactly, whatever the current head is
    psql("UPDATE alembic_version SET version_num = '0000'")  # as if the database were behind the code
    try:
        r = wait_until(lambda: (resp := _ready()).status_code == 503 and resp, 15, what="readiness to fail")
        assert r.json()["checks"]["database"] == {"ok": False, "detail": f"schema at 0000, expected {head}"}
    finally:
        psql(f"UPDATE alembic_version SET version_num = '{head}'")
    wait_until(lambda: _ready().status_code == 200, 15, what="readiness to recover")
