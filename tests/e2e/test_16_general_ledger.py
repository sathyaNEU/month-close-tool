"""The general ledger through the stack (Gate 8a).

1. The sample journaling configuration is made active if it isn't, and the generator's ``tiny`` data is in the
   local lake (``test_15`` writes it when the lake has none).
2. dave, the controller, journals US01 with the stand-in. The run snapshots its configuration, and the job writes the
   reference engine's lines and rollups to the Delta ledger. A rerun writes nothing.
3. The balances for July balance, and the roll-forward from July to September ties.
4. The stub ERP's trial balance, the GL's balances plus one accrual booked only in the ERP, is uploaded. The tie-out
   reports exactly that accrual.

The audit chain verifies afterwards.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from stacklib import User, compose, wait_until
from test_13_journals import _active, _make_active, _post
from test_15_fx_journals import _rates_in_the_lake

from tools import erp_stub

pytestmark = pytest.mark.stack
SAMPLES = Path(__file__).resolve().parents[2] / "docs" / "samples" / "journaling"
JOURNALING = ("accounts", "dimensions", "programs", "account-mappings", "event-types", "auto-journal-definitions")
SINGLETONS = ("accounts", "dimensions", "programs")  # one active at a time
ERP_ONLY = {"6050": 1_000, "2420": -1_000}  # a 10.00 accrual booked directly in the ERP


def _journaling_configuration(alice: User, dave: User) -> None:
    """Every published journaling sample active, in the files' dependency order (``NN-<resource>-<id>.json``)."""
    for path in sorted(SAMPLES.glob("*.json")):
        resource, config_id = path.stem.split("-", 1)[1].rsplit("-", 1)
        if resource not in JOURNALING:
            continue
        if _active(alice, resource, None if resource in SINGLETONS else config_id):
            continue
        body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))["body"]
        _make_active(alice, dave, resource, body)


def _settled(user: User, run_id: str) -> dict[str, Any] | None:
    run: dict[str, Any] = user.http.get(f"/api/v1/journaling/runs/{run_id}").json()
    return run if run["status"] != "running" else None


def _journal(dave: User) -> dict[str, Any]:
    started = _post(dave, "/api/v1/journaling/runs", {"entity_id": "US01"})
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    settled: dict[str, Any] = wait_until(lambda: _settled(dave, run_id), 600, interval=2, what="the journaling run")
    return settled


def test_the_general_ledger_is_journaled_balanced_and_tied_to_the_erp_on_the_stack() -> None:
    alice, dave, erp = User("alice"), User("dave"), User("erp")
    _journaling_configuration(alice, dave)
    _rates_in_the_lake(alice)  # the generator's tiny data, written once
    first = _journal(dave)
    assert first["status"] == "reconciled", first
    assert first["lines"] > 0
    again = _journal(dave)
    assert (again["status"], again["lines_written"], again["rollups_written"]) == ("reconciled", 0, 0)
    balances = dave.http.get("/api/v1/gl/balances?entity_id=US01&period=2026-07").json()
    assert balances["balanced"], balances
    assert {a["account"] for a in balances["accounts"]} >= {"1110", "2010"}
    rf = dave.http.get("/api/v1/gl/roll-forward?entity_id=US01&from=2026-07&to=2026-09").json()
    assert rf["ties"], rf["breaks"]
    extract = erp_stub.trial_balance_csv(balances, ERP_ONLY, decimals=2)
    uploaded = erp_stub.upload_trial_balance(erp.http, "US01", "2026-07", extract)
    assert uploaded["status"] == "accepted", uploaded
    tie = dave.http.get("/api/v1/gl/erp-tie-out?entity_id=US01&period=2026-07").json()
    assert {(i["account"], i["difference_minor"]) for i in tie["items"] if i["status"] != "ties"} == set(
        ERP_ONLY.items()
    )
    assert tie["extract"]["extract_id"] == uploaded["extract_id"]
    verdict = json.loads(compose("exec", "-T", "app", "python", "-m", "close_app.audit", "verify").stdout)
    assert verdict["ok"], verdict
    assert uuid.UUID(first["run_id"])
