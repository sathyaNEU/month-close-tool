"""Helpers shared by the app tests (conftest modules are not importable by name)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from close_app.context import AppContext
from close_contracts.enums import JobStatus
from close_contracts.query import Row
from close_platform.compute.fake import FakeComputeBackend
from close_platform.outbox import Relay
from close_platform.query.reference import ReferenceBackend

# Same users and grants as close_app.devseed (and deploy/devproxy/Caddyfile).
USERS = {
    "alice": ("1001", "alice@example.com"),
    "bob": ("1002", "bob@example.com"),
    "carol": ("1003", "carol@example.com"),
    "dave": ("1004", "dave@example.com"),
    "erin": ("1005", "erin@example.com"),
    "erp": ("1006", "erp@example.com"),
}
GRANTS = [("1001", "US01"), ("1002", "EU01"), ("1004", "US01"), ("1004", "EU01")]  # match_preparer


def assign(engine: Engine, user_id: str, role: str, scope: str) -> None:
    """Grant a role directly (tests), as the migration and devseed do."""
    with engine.begin() as tx:
        tx.execute(
            text(
                "INSERT INTO role_assignments (assignment_id, user_id, role, scope, granted_by) "
                "VALUES (:id, :u, :r, :s, 'test')"
            ),
            {"id": uuid.uuid4(), "u": user_id, "r": role, "s": scope},
        )


def who(user: str, **extra: str) -> dict[str, str]:
    """The identity headers the proxy would set for ``user``."""
    user_id, email = USERS[user]
    return {"X-Forwarded-User": user_id, "X-Forwarded-Email": email, "X-Forwarded-Preferred-Username": user, **extra}


# ---------------------------------------------------------------- driving a run with the fake Jobs backend

RESULT = {
    "job": "smoke",
    "rows": 1000,
    "digest": "d-all",
    "digests_by_entity": {"US01": "d-us", "EU01": "d-eu"},
    "rows_by_entity": {"US01": 600, "EU01": 400},
    "fenced": False,
    "spark_master": "local[2]",
    "engine_version": "0.1.0",
}


def start(client: Any, user: str = "alice", key: str | None = None, rows: int = 1000) -> Any:
    headers = who(user, **{"Idempotency-Key": key or f"key-{uuid.uuid4()}"})
    return client.post("/api/v1/smoke/runs", json={"rows": rows}, headers=headers)


def token_of(engine: Engine, run_id: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text("SELECT fencing_token FROM run_attempts WHERE run_id = :r"), {"r": run_id}).scalar_one()
        )


def publish_manifest(
    lake: ReferenceBackend, run_id: str, token: int, *, attempt: int = 1, extra: list[Row] | None = None
) -> None:
    """What the smoke job writes to ops.run_manifests when it succeeds."""
    rows: list[Row] = [
        {
            "run_id": uuid.UUID(run_id).hex,
            "attempt": attempt,
            "fencing_token": token,
            "job": "smoke",
            "entity_id": entity,
            "row_count": RESULT["rows_by_entity"][entity],  # type: ignore[index]
            "digest": digest,
        }
        for entity, digest in RESULT["digests_by_entity"].items()  # type: ignore[union-attr]
    ]
    lake.load("ops.run_manifests", rows + (extra or []))


def run_to(
    client: Any,
    relay: Relay,
    ctx: AppContext,
    fake: FakeComputeBackend,
    status: JobStatus,
    *,
    lake: ReferenceBackend | None = None,
) -> str:
    """Start a run and drive it to ``status``; a successful run publishes its manifest like the real job."""
    run_id = str(start(client).json()["run_id"])
    assert relay.run_once() == 1
    ext = fake.by_token[next(t for t in fake.by_token if uuid.UUID(run_id).hex in t)]
    fake.advance(ext, status, result=RESULT if status == JobStatus.SUCCEEDED else None)
    assert ctx.coordinator.poll() == 1
    if status == JobStatus.SUCCEEDED and lake is not None:
        publish_manifest(lake, run_id, token_of(ctx.engine, run_id))
    return run_id


# ---------------------------------------------------------------- the SAMPLE journaling configuration as API bodies

SAMPLES = Path(__file__).resolve().parents[3] / "docs" / "samples" / "journaling"


def journal_samples() -> list[tuple[str, dict[str, Any]]]:
    """(resource, request body) for every published sample, in activation order."""
    out = []
    for path in sorted(SAMPLES.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out.append((str(data["resource"]), dict(data["body"])))
    return out
