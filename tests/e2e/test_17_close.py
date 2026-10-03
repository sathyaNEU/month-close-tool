"""The period close through the stack (Gate 8b).

1. The sample close checklist and flux rules are made active if they aren't.
2. dave opens US01's July, the period ``test_16`` journaled, generates its checklist from the sample, and soft-closes
   it. The due dates come from US01's calendar (``US_FED``) in the lake, through the narrow lookup.
3. The stub ERP's July trial balance is uploaded again, now with nothing booked only in the ERP, so the GL ties.
4. alice explains every flux line over its threshold (July against an empty June), and the line with the largest
   change whatever its threshold, and erin reviews the flux.
5. The preparers and reviewers work the manual tasks, and alice runs the automatic checks after the upload: journals
   settled, suspense clear, GL balanced, ERP ties and flux reviewed.
6. dave closes July, and the audit chain verifies.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from stacklib import User, compose
from test_13_journals import _active, _make_active, _post, _sample

from tools import erp_stub

pytestmark = pytest.mark.stack
PERIOD = "2026-07"
TASKS = f"/api/v1/close/tasks/US01/{PERIOD}"


def _task(user: User, task_id: str, verb: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    r = _post(user, f"{TASKS}/{task_id}:{verb}", body)
    assert r.status_code == 200, (task_id, verb, r.text)
    out: dict[str, Any] = r.json()
    return out


def _period(user: User) -> dict[str, Any]:
    found: dict[str, Any] = user.http.get(f"/api/v1/periods/US01/{PERIOD}").json()
    return found


def test_a_period_is_soft_closed_reviewed_and_closed_on_the_stack() -> None:
    alice, dave, erin, erp = User("alice"), User("dave"), User("erin"), User("erp")
    for resource, config_id in (("close-checklists", "sample_month_end_close"), ("flux-rules", "sample_flux_rules")):
        if not _active(alice, resource, config_id):
            _make_active(alice, dave, resource, _sample(f"{resource}-{config_id}"))
    if dave.http.get(f"/api/v1/periods/US01/{PERIOD}").status_code == 404:
        assert _post(dave, "/api/v1/periods", {"entity_id": "US01", "period": PERIOD}).status_code == 201
    made = _post(dave, "/api/v1/close/checklists", {"entity_id": "US01", "period": PERIOD})
    assert made.status_code == 201, made.text
    due = {t["task_id"]: t["due_on"] for t in made.json()["tasks"]}
    assert made.json()["calendar_code"] == "US_FED"
    assert (due["shipping_label_cutoff"], due["journals_settled"], due["controller_sign_off"]) == (
        "2026-07-31",  # BD-1: July ends on a Friday
        "2026-08-03",  # BD+1: the next Monday
        "2026-08-07",  # BD+5
    )
    soft = _post(dave, f"/api/v1/periods/US01/{PERIOD}:soft-close", None, _period(dave)["etag"])
    assert (soft.status_code, soft.json()["status"]) == (200, "soft_closed"), soft.text
    balances = dave.http.get(f"/api/v1/gl/balances?entity_id=US01&period={PERIOD}").json()
    extract = erp_stub.trial_balance_csv(balances, {}, decimals=2)
    assert erp_stub.upload_trial_balance(erp.http, "US01", PERIOD, extract)["status"] == "accepted"
    flux = alice.http.get(f"/api/v1/flux?entity_id=US01&period={PERIOD}").json()
    assert flux["comparison_period"] == "2026-06"
    assert flux["lines"]
    largest = max(flux["lines"], key=lambda line: abs(line["change_minor"]))  # explained whatever its threshold
    for line in flux["lines"]:
        if line["needs_explanation"] or line is largest:
            why = {"entity_id": "US01", "period": PERIOD, "account": line["account"], "explanation": "first month"}
            r = _post(alice, "/api/v1/flux/explanations", why)
            assert r.status_code == 201, r.text
            assert (r.json()["current_minor"], r.json()["comparison_minor"]) == (line["current_minor"], 0)
    reviewed = _post(erin, "/api/v1/flux/reviews", {"entity_id": "US01", "period": PERIOD})
    assert reviewed.status_code == 201, reviewed.text
    _task(alice, "shipping_label_cutoff", "prepare", {"note": "every July label is in the carrier extract"})
    _task(dave, "shipping_label_cutoff", "review")
    assert _task(alice, "journals_settled", "check")["passed"]
    _task(alice, "carrier_accrual", "prepare")
    _task(erin, "carrier_accrual", "review")
    _task(alice, "seller_payables_review", "prepare")
    _task(dave, "seller_payables_review", "review")
    for check in ("suspense_clear", "gl_balanced", "erp_ties", "flux_reviewed"):
        result = _task(alice, check, "check")
        assert (result["passed"], result["status"]) == (True, "done"), result
    _task(dave, "controller_sign_off", "prepare")
    _task(erin, "controller_sign_off", "review")
    assert dave.http.get(f"/api/v1/close/checklists?entity_id=US01&period={PERIOD}").json()["complete"]
    closed = _post(dave, f"/api/v1/periods/US01/{PERIOD}:close", None, _period(dave)["etag"])
    assert (closed.status_code, closed.json()["status"]) == (200, "closed"), closed.text
    verdict = json.loads(compose("exec", "-T", "app", "python", "-m", "close_app.audit", "verify").stdout)
    assert verdict["ok"], verdict
