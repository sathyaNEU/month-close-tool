"""Journaling runs: the sample stand-in for the Spark journaling job (Stage 8a, ADR-0019).

- **Starting a run** (:func:`start`) snapshots everything the run depends on into ``config_snapshots``:
  - the active journaling configuration;
  - the active entity set;
  - the entity's period statuses, read ``FOR SHARE`` so a close can't slip in between.

  It then starts the ``journal_ref`` job under the entity's ``ledger:<entity>`` lease, the same lease posting runs
  take, passing only the snapshot's hash. It waits for the entity's previous run to be settled, not only for the
  lease, which is released when the job ends: a pin is written when its run is settled, and the next run must see
  it.
- **Pins.** The first successful run for an entity pins the configuration and the digest of the inputs it read. A
  later run with another configuration is refused here. The job refuses inputs that no longer match the pin. So a
  rerun writes nothing, and nothing is booked twice: applying changes to a written ledger needs the correction
  state only the Spark job will keep.
- **Reconciling** (:func:`reconcile`, a scheduler task) settles finished runs. A succeeded run is ``reconciled``
  (and pins the entity if it had no pin), a refusal is ``refused`` with its reason, and anything else is ``failed``.
- **Periods.** A period can't close while a journaling run for its entity is in progress (``periods.move``).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Connection, Engine, RowMapping, text

from close_app import audit
from close_app.errors import Problem
from close_app.identity import Principal
from close_contracts.canonical_json import content_sha256
from close_contracts.config.envelope import ConfigKind
from close_platform.coordinator import RunCoordinator
from close_platform.leases import LeaseBusyError

JOB: Final = "journal_ref"
KINDS: Final = (
    ConfigKind.ACCOUNTING_POLICY,
    ConfigKind.CHART_OF_ACCOUNTS,
    ConfigKind.DIMENSION_SET,
    ConfigKind.ACCOUNT_MAPPING,
    ConfigKind.EVENT_TYPE,
    ConfigKind.AUTO_JOURNAL_DEFINITION,
    ConfigKind.ENTITY_SET,
)
SINGLETONS: Final = (
    ConfigKind.ACCOUNTING_POLICY,
    ConfigKind.CHART_OF_ACCOUNTS,
    ConfigKind.DIMENSION_SET,
    ConfigKind.ENTITY_SET,
)
_ACTIVE: Final = text(
    "SELECT kind, config_id, version, spec FROM config_versions WHERE status = 'active' AND kind = ANY (:k) "
    "ORDER BY kind, config_id"
)
_PERIODS: Final = text("SELECT period, status FROM accounting_periods WHERE entity_id = :e ORDER BY period FOR SHARE")
_PIN: Final = text("SELECT * FROM ledger_pins WHERE entity_id = :e")
_SNAPSHOT: Final = text(
    "INSERT INTO config_snapshots (snapshot_hash, purpose, configuration_hash, entity_id, content, created_by) "
    "VALUES (:h, 'journaling', :ch, :e, CAST(:content AS jsonb), :by) ON CONFLICT (snapshot_hash) DO NOTHING"
)
_INSERT_RUN: Final = text(
    "INSERT INTO journaling_runs (run_id, entity_id, requested_by, snapshot_hash, configuration_hash, "
    "pinned_inputs, status) VALUES (:id, :e, :by, :h, :ch, :pinned, 'running')"
)
_RUN: Final = text("SELECT * FROM journaling_runs WHERE run_id = :id")
_RUN_LOCKED: Final = text("SELECT * FROM journaling_runs WHERE run_id = :id FOR UPDATE")
_FINISHED: Final = text(
    "SELECT j.run_id, r.state, r.result, r.error FROM journaling_runs j JOIN runs r ON r.run_id = j.run_id "
    "WHERE j.status = 'running' AND r.state IN ('succeeded', 'failed', 'cancelled', 'submit_failed') "
    "ORDER BY j.requested_at"
)
_SETTLE: Final = text(
    "UPDATE journaling_runs SET status = :st, inputs_digest = :d, events = :ev, lines = :ln, "
    "lines_written = :lw, rollups = :ro, rollups_written = :rw, suspense_lines = :su, "
    "prior_period_adjustments = :ppa, commit_version = :v, reason = :why, result = CAST(:result AS jsonb), "
    "reconciled_at = clock_timestamp(), row_version = row_version + 1 WHERE run_id = :id"
)
_PIN_IT: Final = text(
    "INSERT INTO ledger_pins (entity_id, configuration_hash, inputs_digest, run_id) VALUES (:e, :ch, :d, :id) "
    "ON CONFLICT (entity_id) DO NOTHING"
)
RUNNING: Final = text("SELECT 1 FROM journaling_runs WHERE entity_id = :e AND status = 'running' LIMIT 1")


def _plain(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else str(value) if isinstance(value, UUID) else value


def run_json(row: RowMapping) -> dict[str, Any]:
    return {k: _plain(v) for k, v in row.items()}


def snapshot_for(tx: Connection, entity_id: str) -> dict[str, Any]:
    """What a run for ``entity_id`` depends on, as canonical JSON: a 409 unless a complete configuration is active."""
    configuration: dict[str, list[dict[str, Any]]] = {k.value: [] for k in KINDS}
    for r in tx.execute(_ACTIVE, {"k": [k.value for k in KINDS]}).mappings():
        configuration[r["kind"]].append({"config_id": r["config_id"], "version": r["version"], "spec": r["spec"]})
    missing = [k.value for k in SINGLETONS if len(configuration[k.value]) != 1]
    missing += [k for k in ("event_type", "auto_journal_definition") if not configuration[k]]
    if missing:
        raise Problem(409, "No complete journaling configuration is active", f"missing: {', '.join(missing)}")
    entities = {e["entity_id"] for e in configuration["entity_set"][0]["spec"]["entities"]}
    if entity_id not in entities:
        raise Problem(409, "The entity isn't in the active entity set", entity_id, kind="conflict")
    periods = {str(r.period): str(r.status) for r in tx.execute(_PERIODS, {"e": entity_id})}
    return {"configuration": configuration, "periods": periods, "entity_id": entity_id}


def start(
    tx: Connection, coordinator: RunCoordinator, entity_id: str, *, p: Principal, request_id: str | None
) -> RowMapping:
    if tx.execute(RUNNING, {"e": entity_id}).first() is not None:  # its job may be done, but its pin isn't written yet
        raise Problem(
            409, "The entity's ledger is busy", "an earlier journaling run hasn't been settled yet", kind="conflict"
        )
    snapshot = snapshot_for(tx, entity_id)
    configuration_hash = content_sha256(snapshot["configuration"])
    pin = tx.execute(_PIN, {"e": entity_id}).mappings().first()
    if pin is not None and pin["configuration_hash"] != configuration_hash:
        raise Problem(
            409,
            "The journaling configuration changed",
            "the ledger was written with another configuration, and the stand-in can't apply changes to it; "
            "the Spark journaling job will",
            kind="conflict",
        )
    snapshot_hash = content_sha256(snapshot)
    tx.execute(
        _SNAPSHOT,
        {
            "h": snapshot_hash,
            "ch": configuration_hash,
            "e": entity_id,
            "content": json.dumps(snapshot),
            "by": p.user_id,
        },
    )
    run_id = uuid.uuid4()
    pinned = pin["inputs_digest"] if pin is not None else None
    tx.execute(
        _INSERT_RUN,
        {"id": run_id, "e": entity_id, "by": p.user_id, "h": snapshot_hash, "ch": configuration_hash, "pinned": pinned},
    )
    try:
        coordinator.start(
            tx,
            job_key=JOB,
            lease_resource=f"ledger:{entity_id}",
            requested_by=p.user_id,
            request_id=request_id,
            params={"entity_id": entity_id, "snapshot_hash": snapshot_hash, "pinned_inputs": pinned or ""},
            run_id=run_id,
        )
    except LeaseBusyError:
        raise Problem(
            409, "The entity's ledger is busy", "a posting or journaling run is in progress", kind="conflict"
        ) from None
    audit.append(
        tx,
        audit.Event(
            p.user_id,
            "journaling.start",
            entity_ids=(entity_id,),
            target=f"journaling_run:{run_id}",
            payload={"snapshot_hash": snapshot_hash, "configuration_hash": configuration_hash, "pinned": bool(pin)},
        ),
        request_id,
    )
    found = tx.execute(_RUN, {"id": run_id}).mappings().first()
    if found is None:
        raise RuntimeError("the journaling run just created is missing")
    return found


def _settle(tx: Connection, run: RowMapping, state: str, result: dict[str, Any] | None, error: str | None) -> str:
    ok = state == "succeeded" and result is not None and not result.get("fenced")
    if ok and result is not None and "refused" in result:
        status, reason = "refused", str(result["refused"])[:2000]
    elif ok and result is not None and result.get("configuration_hash") != run["configuration_hash"]:
        status, reason = "failed", "the job didn't report the configuration the run snapshotted"
    elif ok and result is not None and not isinstance(result.get("inputs_digest"), str):
        status, reason = "failed", "the job didn't report the digest of its inputs"
    elif ok:
        status, reason = "reconciled", None
    else:
        status, reason = "failed", (error or "the journaling job did not report")[:2000]
    counted = result if status == "reconciled" and result is not None else {}
    tx.execute(
        _SETTLE,
        {
            "id": run["run_id"],
            "st": status,
            "d": counted.get("inputs_digest"),
            "ev": counted.get("events"),
            "ln": counted.get("lines"),
            "lw": counted.get("lines_written"),
            "ro": counted.get("rollups"),
            "rw": counted.get("rollups_written"),
            "su": counted.get("suspense_lines"),
            "ppa": counted.get("prior_period_adjustments"),
            "v": counted.get("commit_version"),
            "why": reason,
            "result": json.dumps(result if result is not None else {"error": error}, default=str),
        },
    )
    if status == "reconciled":
        tx.execute(
            _PIN_IT,
            {
                "e": run["entity_id"],
                "ch": run["configuration_hash"],
                "d": counted["inputs_digest"],
                "id": run["run_id"],
            },
        )
    audit.append(
        tx,
        audit.Event(
            "system:journaling",
            f"journaling.{status}",
            entity_ids=(str(run["entity_id"]),),
            target=f"journaling_run:{run['run_id']}",
            payload={"lines_written": counted.get("lines_written"), "reason": reason},
        ),
        None,
    )
    return status


def reconcile(engine: Engine) -> int:
    """Settle every journaling run whose job has finished. Returns how many were settled."""
    with engine.connect() as conn:
        finished = [dict(r) for r in conn.execute(_FINISHED).mappings()]
    settled = 0
    for f in finished:
        with engine.begin() as tx:
            run = tx.execute(_RUN_LOCKED, {"id": f["run_id"]}).mappings().first()
            if run is None or run["status"] != "running":
                continue  # another instance settled it
            result = dict(f["result"]) if f["result"] is not None else None
            _settle(tx, run, str(f["state"]), result, f["error"])
            settled += 1
    return settled
