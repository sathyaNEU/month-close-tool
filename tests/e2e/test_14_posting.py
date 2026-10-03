"""Posting through the stack (Gate 7b).

1. alice prepares a journal, dave and erin approve it, and dave (the controller) posts the entity's approved
   journals. The posting job writes them to the Delta ledger and the reconcile task marks them posted. The trial
   balance and the drill-down show the journal, and the Postgres-to-Delta tie-out holds.
2. The stub ERP (``tools/erp_stub.py``, signed in as ``erp``) reads the feed and writes document numbers back. A
   different number for a confirmed journal is refused.
3. A posting job is killed after its ledger commit, before its feed and its result line. The run fails and the
   journal returns to approved. The retry finds the lines, writes none, completes the feed, and the journal ends
   posted with its lines in the ledger once.

The lake gets DDL V007 first (``ddl_apply`` applies only what it hasn't), and the audit chain verifies afterwards.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from stacklib import LAKE_ROOT, User, compose, hold, release, stack_job, wait_reached, wait_until
from test_13_journals import PERIOD, _active, _make_active, _post, _sample

from tools import erp_stub

pytestmark = pytest.mark.stack
DDL_APPLY_JOB_ID = 1002
# Kill the posting job's driver (the JVM and its Python process) where it waits, as a crash would.
KILL = """
import os, signal
for pid in (p for p in os.listdir('/proc') if p.isdigit() and int(p) != os.getpid()):
    try:
        cmd = open(f'/proc/{pid}/cmdline', 'rb').read().replace(b'\\0', b' ').decode(errors='replace')
    except OSError:
        continue
    if 'post_journals' in cmd and ('SparkSubmit' in cmd or 'run_job.py' in cmd):
        os.kill(int(pid), signal.SIGKILL)
        print(pid)
"""


def _ready(alice: User, dave: User) -> None:
    for resource, prefix, config_id in (
        ("accounts", "accounts-sample_chart", None),
        ("entities", "entities-sample_entities", None),
        ("approval-matrices", "approval-matrices-sample_approval_matrix", None),
        ("journal-templates", "journal-templates-sample_standard_journal", "sample_standard_journal"),
    ):
        if not _active(alice, resource, config_id):
            _make_active(alice, dave, resource, _sample(prefix))
    if dave.http.get(f"/api/v1/periods/US01/{PERIOD}").status_code == 404:
        assert _post(dave, "/api/v1/periods", {"entity_id": "US01", "period": PERIOD}).status_code == 201


def _approved(alice: User, dave: User, erin: User) -> dict[str, Any]:
    body = {
        "entity_id": "US01",
        "template_id": "sample_standard_journal",
        "accounting_date": f"{PERIOD}-15",
        "description": f"Carrier accrual true-up, posting run {uuid.uuid4().hex[:8]}",
        "lines": [
            {"account": "5020", "side": "DR", "amount_minor": 2_000_000},
            {"account": "2410", "side": "CR", "amount_minor": 2_000_000},
        ],
    }
    created = _post(alice, "/api/v1/journals", body)
    assert created.status_code == 201, created.text
    j: dict[str, Any] = created.json()
    url = f"/api/v1/journals/{j['journal_id']}"
    j = _post(alice, url + ":submit", etag=j["etag"]).json()
    while j["status"] == "pending_approval":
        approver = dave if j["current_level"] == 1 else erin
        j = _post(approver, url + ":approve", etag=j["etag"]).json()
    assert j["status"] == "approved", j
    return j


def _status(user: User, journal: dict[str, Any]) -> str:
    return str(user.http.get(f"/api/v1/journals/{journal['journal_id']}").json()["status"])


def _settled(user: User, run_id: str) -> dict[str, Any] | None:
    run: dict[str, Any] = user.http.get(f"/api/v1/posting/runs/{run_id}").json()
    return run if run["status"] != "posting" else None


def _start(dave: User) -> str:
    started = _post(dave, "/api/v1/posting/runs", {"entity_id": "US01"})
    assert started.status_code == 202, started.text
    return str(started.json()["run_id"])


def _post_and_settle(dave: User) -> dict[str, Any]:
    run_id = _start(dave)
    settled: dict[str, Any] = wait_until(lambda: _settled(dave, run_id), 300, interval=1, what="the run to settle")
    return settled


def _pages(user: User, path: str, key: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params = {"entity_id": "US01", "page_size": "500"} | ({"cursor": cursor} if cursor else {})
        page = user.http.get(path, params=params)
        assert page.status_code == 200, page.text
        out += page.json()[key]
        cursor = page.json()["next_cursor"]
        if not cursor:
            return out


def _lines_of(user: User, journal: dict[str, Any]) -> list[dict[str, Any]]:
    return [ln for ln in _pages(user, "/api/v1/gl/lines", "lines") if ln["journal_id"] == journal["journal_id"]]


def _in_feed(erp: User, journal: dict[str, Any]) -> bool:
    return any(p["journal_id"] == journal["journal_id"] for p in _pages(erp, "/api/v1/gl/postings", "postings"))


def _ties(dave: User) -> dict[str, Any]:
    tie: dict[str, Any] = dave.http.get("/api/v1/posting/tie-out?entity_id=US01").json()
    return tie


def test_a_journal_goes_from_draft_through_the_ledger_to_the_erp_on_the_stack() -> None:
    ddl = stack_job("ddl_apply", DDL_APPLY_JOB_ID, root_uri=LAKE_ROOT)
    assert ddl["result_state"] == "SUCCESS", ddl
    alice, dave, erin, erp = User("alice"), User("dave"), User("erin"), User("erp")
    _ready(alice, dave)
    j = _approved(alice, dave, erin)
    run = _post_and_settle(dave)
    assert run["status"] == "reconciled", run
    assert _status(dave, j) == "posted"
    lines = _lines_of(dave, j)
    assert sorted((ln["account"], ln["debit_minor"], ln["credit_minor"]) for ln in lines) == [
        ("2410", 0, 2_000_000),
        ("5020", 2_000_000, 0),
    ]
    tb = dave.http.get(f"/api/v1/gl/trial-balance?entity_id=US01&period={PERIOD}").json()
    assert tb["balanced"], tb
    assert {a["account"] for a in tb["accounts"]} >= {"5020", "2410"}
    assert _ties(dave)["ties"], _ties(dave)
    # The stub ERP books what it reads and writes the numbers back.
    summary = erp_stub.run(erp.http, "US01")
    assert summary["refused"] == []
    expected = f"ERP-US01-{j['number']:06d}"
    assert {"journal_id": j["journal_id"], "number": j["number"], "document_id": expected} in summary["confirmed"]
    after = dave.http.get(f"/api/v1/journals/{j['journal_id']}").json()
    assert (after["status"], after["erp_document_id"]) == ("erp_confirmed", expected)
    other = {"entries": [{"journal_id": j["journal_id"], "document_id": "ERP-US01-999999"}]}
    clash = _post(erp, "/api/v1/posting/status", other)
    assert (clash.status_code, clash.json()["issues"][0]["code"]) == (409, "different_document")
    assert _post(alice, "/api/v1/posting/status", other).status_code == 403  # a preparer is no ERP


def test_a_job_killed_after_its_ledger_commit_posts_once_on_the_retry() -> None:
    alice, dave, erin, erp = User("alice"), User("dave"), User("erin"), User("erp")
    _ready(alice, dave)
    k = _approved(alice, dave, erin)
    hold("after_lines_commit")
    try:
        run_id = _start(dave)
        wait_reached("after_lines_commit", timeout=300)
        killed = compose("exec", "-T", "jobs", "python", "-c", KILL).stdout.split()
        assert killed, "no posting job process found to kill"
    finally:
        release("after_lines_commit")
    failed = wait_until(lambda: _settled(dave, run_id), 300, interval=1, what="the killed run to settle")
    assert failed["status"] == "failed", failed
    assert _status(dave, k) == "approved"  # back for a retry
    assert len(_lines_of(dave, k)) == 2  # the lines were committed before the crash...
    assert not _in_feed(erp, k)  # ...and the feed, written after them, was not
    retry = _post_and_settle(dave)
    assert (retry["status"], retry["written"], retry["already_present"]) == ("reconciled", 0, 1), retry
    assert _status(dave, k) == "posted"
    assert len(_lines_of(dave, k)) == 2  # once
    assert _in_feed(erp, k)  # the retry completed the feed
    assert _ties(dave)["ties"], _ties(dave)
    summary = erp_stub.run(erp.http, "US01")
    assert k["journal_id"] in {c["journal_id"] for c in summary["confirmed"]}
    verdict = json.loads(compose("exec", "-T", "app", "python", "-m", "close_app.audit", "verify").stdout)
    assert verdict["ok"], verdict
