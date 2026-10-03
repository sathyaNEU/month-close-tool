"""Posting runs and the ERP write-back (Stage 7b, ADR-0017).

- **Starting a run** (:func:`start`) takes the entity's approved journals, at most 500, batches only whole. It
  locks them in id order and their periods ``FOR SHARE``, and freezes them as ``posting`` in this run. It then
  starts the ``post_journals`` job under the entity's ledger lease, with fencing (ADR-0007).
- **Separation of duties.** A requester's own journals are left out, and so is a batch holding one. They stay
  ``approved`` for someone else, and the run lists them as skipped.
- **Reconciling** (:func:`reconcile`, a scheduler task) settles finished runs.
  - **A succeeded run:** the journals it wrote, or found already written, become ``posted``, with the run and
    the ledger's commit version.
  - **Every other outcome:** the journals return to ``approved``. A retry then finds any lines a lost result
    hid, so a journal is never written twice and never lost.
- **The ERP write-back** (:func:`confirm`) records document numbers for posted journals, moving them to
  ``erp_confirmed``. It is all or nothing: the same number again is a no-op, and a different one is refused.
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
from close_app.journals import store, workflow
from close_platform.coordinator import RunCoordinator
from close_platform.leases import LeaseBusyError

MAX_JOURNALS: Final = 500
JOB: Final = "post_journals"
_APPROVED: Final = text(
    "SELECT * FROM journals WHERE entity_id = :e AND status = 'approved' ORDER BY journal_id FOR UPDATE"
)
_RUN: Final = text("SELECT * FROM posting_runs WHERE run_id = :id")
_RUN_LOCKED: Final = text("SELECT * FROM posting_runs WHERE run_id = :id FOR UPDATE")
_INSERT_RUN: Final = text(
    "INSERT INTO posting_runs (run_id, entity_id, requested_by, journal_count, skipped, status) "
    "VALUES (:id, :e, :by, :n, CAST(:skipped AS jsonb), 'posting')"
)
_FREEZE: Final = text(
    "UPDATE journals SET status = 'posting', posting_run_id = :run, row_version = row_version + 1 "
    "WHERE journal_id = ANY (:ids)"
)
_IN_RUN: Final = text("SELECT * FROM journals WHERE posting_run_id = :run AND status = 'posting' ORDER BY journal_id")
_POSTED: Final = text(
    "UPDATE journals SET status = 'posted', posted_at = clock_timestamp(), posted_version = :v, "
    "row_version = row_version + 1 WHERE journal_id = :id AND status = 'posting'"
)
_BACK: Final = text(
    "UPDATE journals SET status = 'approved', posting_run_id = NULL, row_version = row_version + 1 "
    "WHERE journal_id = :id AND status = 'posting'"
)
_SETTLE: Final = text(
    "UPDATE posting_runs SET status = :st, written = :w, already_present = :a, commit_version = :v, "
    "result = CAST(:result AS jsonb), reconciled_at = clock_timestamp(), row_version = row_version + 1 "
    "WHERE run_id = :id"
)
_FINISHED: Final = text(
    "SELECT p.run_id, r.state, r.result, r.error FROM posting_runs p JOIN runs r ON r.run_id = p.run_id "
    "WHERE p.status = 'posting' AND r.state IN ('succeeded', 'failed', 'cancelled', 'submit_failed') "
    "ORDER BY p.requested_at"
)
_CONFIRM: Final = text(
    "UPDATE journals SET status = 'erp_confirmed', erp_document_id = :doc, erp_confirmed_at = clock_timestamp(), "
    "erp_confirmed_by = :by, row_version = row_version + 1 WHERE journal_id = :id"
)


def _plain(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else str(value) if isinstance(value, UUID) else value


def run_json(row: RowMapping) -> dict[str, Any]:
    return {k: _plain(v) for k, v in row.items()}


def _pick(rows: list[RowMapping], requester: str) -> tuple[list[RowMapping], list[dict[str, Any]]]:
    """The journals to post: whole batches only, none prepared by the requester, at most MAX_JOURNALS."""
    groups: dict[str, list[RowMapping]] = {}
    for row in rows:
        key = f"batch:{row['batch_id']}" if row["batch_id"] is not None else f"journal:{row['journal_id']}"
        groups.setdefault(key, []).append(row)
    picked: list[RowMapping] = []
    skipped: list[dict[str, Any]] = []
    for group in sorted(groups.values(), key=lambda g: min(r["number"] for r in g)):
        own = [r for r in group if r["created_by"] == requester]
        if own:
            reason = "you prepared it" if len(group) == 1 else "you prepared a journal in its batch"
            skipped += [{"number": r["number"], "journal_id": str(r["journal_id"]), "reason": reason} for r in group]
        elif len(picked) + len(group) > MAX_JOURNALS:
            skipped += [
                {"number": r["number"], "journal_id": str(r["journal_id"]), "reason": "the run is full"} for r in group
            ]
        else:
            picked += group
    return picked, skipped


def start(
    tx: Connection, coordinator: RunCoordinator, entity_id: str, *, p: Principal, request_id: str | None
) -> RowMapping:
    candidates = list(tx.execute(_APPROVED, {"e": entity_id}).mappings())
    picked, skipped = _pick(candidates, p.user_id)
    if not picked:
        detail = f"{len(skipped)} approved journals are yours or don't fit" if skipped else "no journal is approved"
        raise Problem(409, "Nothing to post", detail, kind="conflict", skipped=skipped)
    for period in sorted({r["period"] for r in picked}):
        workflow.open_period(tx, entity_id, period)
    run_id = uuid.uuid4()
    tx.execute(
        _INSERT_RUN, {"id": run_id, "e": entity_id, "by": p.user_id, "n": len(picked), "skipped": json.dumps(skipped)}
    )
    tx.execute(_FREEZE, {"run": run_id, "ids": [r["journal_id"] for r in picked]})
    try:
        coordinator.start(
            tx,
            job_key=JOB,
            lease_resource=f"ledger:{entity_id}",
            requested_by=p.user_id,
            request_id=request_id,
            params={"entity_id": entity_id},
            run_id=run_id,
        )
    except LeaseBusyError:
        raise Problem(  # the lease is shared with journaling runs (Stage 8a)
            409, "The entity's ledger is busy", "a posting or journaling run is in progress", kind="conflict"
        ) from None
    for r in picked:
        new = store.reload(tx, r)
        store.history(
            tx, action="post_start", actor=p.user_id, journal=new, from_status="approved", to_status="posting"
        )
    audit.append(
        tx,
        audit.Event(
            p.user_id,
            "posting.start",
            entity_ids=(entity_id,),
            target=f"posting_run:{run_id}",
            payload={"journals": sorted(int(r["number"]) for r in picked), "skipped": len(skipped)},
        ),
        request_id,
    )
    found = tx.execute(_RUN, {"id": run_id}).mappings().first()
    if found is None:
        raise RuntimeError("the posting run just created is missing")
    return found


def _settle(tx: Connection, run: RowMapping, state: str, result: dict[str, Any] | None, error: str | None) -> int:
    """Settle one finished run: what it wrote or found becomes posted, everything else returns to approved."""
    journals = list(tx.execute(_IN_RUN, {"run": run["run_id"]}).mappings())
    ok = state == "succeeded" and result is not None and not result.get("fenced")
    reported = set(result.get("written", [])) | set(result.get("already_present", [])) if ok and result else set()
    version = int(result["commit_version"]) if ok and result and "commit_version" in result else None
    posted = 0
    for j in journals:
        if str(j["journal_id"]) in reported:
            tx.execute(_POSTED, {"id": j["journal_id"], "v": version})
            new = store.reload(tx, j)
            store.history(tx, action="post", actor=store.SYSTEM, journal=new, from_status="posting", to_status="posted")
            store.audit_event(
                tx, store.SYSTEM, "post", new, None, posting_run=str(run["run_id"]), commit_version=version
            )
            posted += 1
        else:
            tx.execute(_BACK, {"id": j["journal_id"]})
            new = store.reload(tx, j)
            reason = (error or "the posting job did not report this journal")[:500]
            store.history(
                tx, action="post_failed", actor=store.SYSTEM, journal=new, from_status="posting", to_status="approved",
                reason=reason,
            )  # fmt: skip
            store.audit_event(tx, store.SYSTEM, "post_failed", new, None, posting_run=str(run["run_id"]), reason=reason)
    tx.execute(
        _SETTLE,
        {
            "id": run["run_id"],
            "st": "reconciled" if ok else "failed",
            "w": len(result.get("written", [])) if ok and result else None,
            "a": len(result.get("already_present", [])) if ok and result else None,
            "v": version,
            "result": json.dumps(result if result is not None else {"error": error}, default=str),
        },
    )
    return posted


def reconcile(engine: Engine) -> int:
    """Settle every posting run whose job has finished. Returns how many journals became posted."""
    with engine.connect() as conn:
        finished = [dict(r) for r in conn.execute(_FINISHED).mappings()]
    posted = 0
    for f in finished:
        with engine.begin() as tx:
            run = tx.execute(_RUN_LOCKED, {"id": f["run_id"]}).mappings().first()
            if run is None or run["status"] != "posting":
                continue  # another instance settled it
            result = dict(f["result"]) if f["result"] is not None else None
            posted += _settle(tx, run, str(f["state"]), result, f["error"])
    return posted


def confirm(
    tx: Connection, entries: list[dict[str, str]], *, p: Principal, entity_ids: set[str], request_id: str | None
) -> list[dict[str, Any]]:
    """Record ERP document numbers, all or nothing. ``entity_ids`` are those the caller may confirm in; a journal
    outside them is a 404, as for any object in none of the caller's entities."""
    rows: list[tuple[RowMapping, str]] = []
    problems: list[dict[str, str]] = []
    unknown: list[dict[str, str]] = []
    for i, entry in enumerate(entries):
        row = store.load(tx, entry["journal_id"], lock=True)
        if row is None or row["entity_id"] not in entity_ids:
            unknown.append(
                {"code": "unknown_journal", "path": f"/{i}/journal_id", "message": "not found or not visible"}
            )
            continue
        doc = entry["document_id"]
        if row["status"] == "erp_confirmed":
            if row["erp_document_id"] != doc:
                message = f"already confirmed as {row['erp_document_id']}"
                problems.append({"code": "different_document", "path": f"/{i}/document_id", "message": message})
            continue
        if row["status"] != "posted":
            problems.append(
                {"code": "not_posted", "path": f"/{i}/journal_id", "message": f"the journal is {row['status']}"}
            )
            continue
        rows.append((row, doc))
    if unknown:
        raise Problem(
            404, "Not found", "a journal does not exist or is not visible to you", kind="not-found", issues=unknown
        )
    if problems:
        raise Problem(409, "The write-back was refused", kind="conflict", issues=problems)
    out = []
    for row, doc in rows:
        tx.execute(_CONFIRM, {"id": row["journal_id"], "doc": doc, "by": p.user_id})
        new = store.reload(tx, row)
        store.history(
            tx, action="erp_confirm", actor=p.user_id, journal=new, from_status="posted", to_status="erp_confirmed"
        )
        store.audit_event(tx, p.user_id, "erp_confirm", new, request_id, document_id=doc)
        out.append({"journal_id": str(row["journal_id"]), "number": row["number"], "document_id": doc})
    return out
