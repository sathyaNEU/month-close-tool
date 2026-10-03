"""The general ledger in the control plane (Gate 8a).

- **Journaling runs** go through the fake Jobs backend. A run snapshots the configuration, entity set and period
  statuses by hash. The first success pins the configuration and inputs, a changed configuration is refused, and a
  refusal or failure is recorded. The ledger lease keeps journaling and posting apart, and a period can't close
  while a run is in progress. The reader role sees only snapshots of runs in progress.
- **Balances, roll-forward and the trial balance** read both GL sources, the engine's rollups and the posted
  manual journals, and never another entity's.
- **The ERP's trial balance** is uploaded all or nothing and versioned. The tie-out reports every difference: an
  account only one side has, a sign error, an extract for the wrong period.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from apptestkit import assign
from fastapi.testclient import TestClient
from journalkit import AUDITOR, CONTROLLER, PERIOD, PREPARER, activate, approved, create, h, open_period, set_up
from pgtestkit import engine_for
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError
from test_journal_posting import start_run as start_posting

from close_app.context import AppContext
from close_app.ledger import erp, journaling
from close_contracts.canonical_json import content_sha256
from close_contracts.config.envelope import ConfigKind
from close_contracts.enums import JobStatus
from close_contracts.samples.journaling import SAMPLE_DEFINITIONS, SAMPLE_EVENT_TYPES, SAMPLE_MAPPINGS, SAMPLE_POLICY
from close_platform.compute.fake import FakeComputeBackend
from close_platform.outbox import Relay
from close_platform.query.reference import ReferenceBackend

pytestmark = pytest.mark.pg
ERP = "7101"
DIGEST = "d" * 64


@pytest.fixture
def ready(engine: Engine) -> None:
    set_up(engine)
    activate(engine, ConfigKind.ACCOUNTING_POLICY, "accounting_policy", SAMPLE_POLICY)
    for m in SAMPLE_MAPPINGS:
        activate(engine, ConfigKind.ACCOUNT_MAPPING, m.mapping_id, m)
    for e in SAMPLE_EVENT_TYPES:
        activate(engine, ConfigKind.EVENT_TYPE, e.type_id, e)
    for d in SAMPLE_DEFINITIONS:
        activate(engine, ConfigKind.AUTO_JOURNAL_DEFINITION, d.definition_id, d)
    for period in ("2026-07", "2026-08", PERIOD):
        open_period(engine, period=period)
    assign(engine, ERP, "integration_client", "US01")


@pytest.fixture
def relay(ctx: AppContext) -> Relay:
    return Relay(ctx.engine, ctx.coordinator.handlers(), instance_id="test-relay")


def start(client: TestClient, user: str = CONTROLLER) -> Any:
    return client.post("/api/v1/journaling/runs", json={"entity_id": "US01"}, headers=h(user))


def play(
    relay: Relay,
    ctx: AppContext,
    fake: FakeComputeBackend,
    run_id: str,
    result: dict[str, Any] | None,
    status: JobStatus = JobStatus.SUCCEEDED,
) -> dict[str, Any]:
    """Do what the journaling job does from the app's point of view: report its result, then settle the run."""
    relay.run_once()  # submits the run, unless the test already did
    ext = fake.by_token[next(t for t in fake.by_token if uuid.UUID(run_id).hex in t)]
    fake.advance(ext, status, result=result)
    assert ctx.coordinator.poll() == 1
    assert journaling.reconcile(ctx.engine) == 1
    with ctx.engine.connect() as conn:
        run = conn.execute(text("SELECT * FROM journaling_runs WHERE run_id = :id"), {"id": run_id}).mappings().one()
    return dict(run)


def result_of(run: dict[str, Any], written: int = 4, digest: str = DIGEST) -> dict[str, Any]:
    return {
        "job": "journal_ref",
        "fenced": False,
        "configuration_hash": run["configuration_hash"],
        "inputs_digest": digest,
        "events": 2,
        "lines": 4,
        "rollups": 2,
        "lines_written": written,
        "rollups_written": min(written, 2),
        "suspense_lines": 0,
        "prior_period_adjustments": 0,
        "commit_version": 3,
    }


def params(relay: Relay, fake: FakeComputeBackend, run_id: str) -> dict[str, str]:
    """The parameters the run's job was submitted with."""
    relay.run_once()
    ext = fake.by_token[next(t for t in fake.by_token if uuid.UUID(run_id).hex in t)]
    return dict(fake.runs[ext].spec.params)


# ---------------------------------------------------------------------------------------------- journaling runs


def test_a_run_snapshots_what_it_depends_on_and_its_first_success_pins_the_ledger(
    client: TestClient, engine: Engine, ctx: AppContext, fake: FakeComputeBackend, relay: Relay, ready: None
) -> None:
    started = start(client)
    assert started.status_code == 202, started.text
    run = started.json()
    with engine.connect() as conn:
        snapshot = (
            conn.execute(text("SELECT * FROM config_snapshots WHERE snapshot_hash = :h"), {"h": run["snapshot_hash"]})
            .mappings()
            .one()
        )
    content = snapshot["content"]
    assert content_sha256(content) == run["snapshot_hash"]
    assert content_sha256(content["configuration"]) == run["configuration_hash"] == snapshot["configuration_hash"]
    assert {k: len(v) for k, v in content["configuration"].items()} == {
        "accounting_policy": 1,
        "chart_of_accounts": 1,
        "dimension_set": 1,
        "account_mapping": len(SAMPLE_MAPPINGS),
        "event_type": len(SAMPLE_EVENT_TYPES),
        "auto_journal_definition": len(SAMPLE_DEFINITIONS),
        "entity_set": 1,
    }
    assert content["periods"] == {"2026-07": "open", "2026-08": "open", PERIOD: "open"}
    assert params(relay, fake, run["run_id"])["pinned_inputs"] == ""
    assert params(relay, fake, run["run_id"])["snapshot_hash"] == run["snapshot_hash"]
    first = play(relay, ctx, fake, run["run_id"], result_of(run))
    assert (first["status"], first["lines_written"], first["inputs_digest"]) == ("reconciled", 4, DIGEST)
    with engine.connect() as conn:
        pin = conn.execute(text("SELECT * FROM ledger_pins WHERE entity_id = 'US01'")).mappings().one()
    assert (pin["configuration_hash"], pin["inputs_digest"]) == (run["configuration_hash"], DIGEST)
    again = start(client).json()
    assert again["pinned_inputs"] == DIGEST
    assert params(relay, fake, again["run_id"])["pinned_inputs"] == DIGEST  # the job refuses other inputs
    rerun = play(relay, ctx, fake, again["run_id"], result_of(again, written=0))
    assert (rerun["status"], rerun["lines_written"]) == ("reconciled", 0)


def test_a_changed_configuration_is_refused_and_refusals_and_failures_are_recorded(
    client: TestClient, engine: Engine, ctx: AppContext, fake: FakeComputeBackend, relay: Relay, ready: None
) -> None:
    refused = start(client).json()
    out = play(
        relay, ctx, fake, refused["run_id"], {"job": "journal_ref", "fenced": False, "refused": "the inputs changed"}
    )
    assert (out["status"], out["reason"]) == ("refused", "the inputs changed")
    failed = start(client).json()
    out = play(relay, ctx, fake, failed["run_id"], None, status=JobStatus.FAILED)
    assert out["status"] == "failed"
    for broken, why in (
        ({"configuration_hash": "0" * 64}, "the job didn't report the configuration the run snapshotted"),
        ({"inputs_digest": None}, "the job didn't report the digest of its inputs"),
    ):
        odd = start(client).json()
        out = play(relay, ctx, fake, odd["run_id"], result_of(odd) | broken)
        assert (out["status"], out["reason"]) == ("failed", why)  # and nothing is pinned by it
    pinned = start(client).json()
    play(relay, ctx, fake, pinned["run_id"], result_of(pinned))
    changed = SAMPLE_MAPPINGS[0].model_copy(update={"description": "a new version"})
    with engine.begin() as tx:  # the old version retires, a new one is active
        tx.execute(
            text("UPDATE config_versions SET status = 'retired' WHERE kind = 'account_mapping' AND config_id = :c"),
            {"c": changed.mapping_id},
        )
    activate(engine, ConfigKind.ACCOUNT_MAPPING, changed.mapping_id, changed, version=2)
    r = start(client)
    assert (r.status_code, r.json()["title"]) == (409, "The journaling configuration changed")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM ledger_pins")).scalar_one() == 1


def busy(r: Any) -> bool:
    return (r.status_code, r.json()["title"]) == (409, "The entity's ledger is busy")


def test_the_ledger_lease_keeps_journaling_and_posting_apart(
    client: TestClient, engine: Engine, ctx: AppContext, fake: FakeComputeBackend, relay: Relay, ready: None
) -> None:
    approved(client, create(client))  # something to post, so posting can only be waiting for the lease
    run = start(client).json()
    assert busy(start(client))
    assert busy(start_posting(client))
    play(relay, ctx, fake, run["run_id"], result_of(run))
    posting = start_posting(client)
    assert posting.status_code == 202, posting.text
    assert busy(start(client))  # and now posting holds it


def test_a_run_waits_for_the_previous_one_to_be_settled_not_only_for_the_lease(
    client: TestClient, engine: Engine, ctx: AppContext, fake: FakeComputeBackend, relay: Relay, ready: None
) -> None:
    first = start(client).json()
    relay.run_once()
    ext = fake.by_token[next(t for t in fake.by_token if uuid.UUID(first["run_id"]).hex in t)]
    fake.advance(ext, JobStatus.SUCCEEDED, result=result_of(first))
    assert ctx.coordinator.poll() == 1  # the job ended and its lease is free, but the run isn't settled
    early = start(client)
    assert (early.status_code, early.json()["detail"]) == (409, "an earlier journaling run hasn't been settled yet")
    assert journaling.reconcile(ctx.engine) == 1
    after = start(client)
    assert (after.status_code, after.json()["pinned_inputs"]) == (202, DIGEST)  # so it sees the pin


def test_a_period_cannot_close_while_a_run_is_in_progress(
    client: TestClient, engine: Engine, ctx: AppContext, fake: FakeComputeBackend, relay: Relay, ready: None
) -> None:
    run = start(client).json()
    period = client.get(f"/api/v1/periods/US01/{PERIOD}", headers=h(CONTROLLER)).json()
    refused = client.post(f"/api/v1/periods/US01/{PERIOD}:close", headers=h(CONTROLLER, period["etag"]))
    assert (refused.status_code, refused.json()["title"]) == (409, "A journaling run is in progress")
    play(relay, ctx, fake, run["run_id"], result_of(run))
    closed = client.post(f"/api/v1/periods/US01/{PERIOD}:close", headers=h(CONTROLLER, period["etag"]))
    assert closed.status_code == 200, closed.text
    later = start(client).json()
    with engine.connect() as conn:
        frozen = conn.execute(
            text("SELECT content FROM config_snapshots WHERE snapshot_hash = :h"), {"h": later["snapshot_hash"]}
        ).scalar_one()
    assert frozen["periods"][PERIOD] == "closed"  # the job will book that period's events into the next open one


def test_the_reader_role_sees_only_snapshots_of_runs_in_progress(
    client: TestClient, engine: Engine, ctx: AppContext, fake: FakeComputeBackend, relay: Relay, ready: None
) -> None:
    run = start(client).json()
    reader = engine_for("pg-ledger-reader.env")
    try:
        with reader.connect() as conn:
            seen = conn.execute(text("SELECT snapshot_hash FROM job_config_snapshots")).scalars().all()
        assert seen == [run["snapshot_hash"]]
        for table in ("config_snapshots", "journaling_runs", "ledger_pins", "erp_trial_balances"):
            with pytest.raises(ProgrammingError, match="permission denied"), reader.connect() as conn:
                conn.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
        play(relay, ctx, fake, run["run_id"], result_of(run))
        with reader.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM job_config_snapshots")).scalar_one() == 0
    finally:
        reader.dispose()
    assert start(client, PREPARER).status_code == 403  # only the controller journals


# ---------------------------------------------------------------------------------------------- balances


def rollup(period: str, account: str, side: str, amount: int, entity: str = "US01", **extra: Any) -> dict[str, Any]:
    return {
        "rollup_line_id": uuid.uuid4().hex,
        "entity_id": entity,
        "period": period,
        "account": account,
        "dimensions_key": "",
        "currency": "USD",
        "debit_minor": amount if side == "DR" else 0,
        "credit_minor": amount if side == "CR" else 0,
        "functional_debit_minor": amount if side == "DR" else 0,
        "functional_credit_minor": amount if side == "CR" else 0,
        "line_count": 1,
        "run_id": "r" * 32,
    } | extra


def manual(period: str, account: str, side: str, amount: int, line_no: int) -> dict[str, Any]:
    return {
        "journal_id": "j-" + period,
        "number": 1,
        "line_no": line_no,
        "period": period,
        "account": account,
        "side": side,
        "amount_minor": amount,
        "currency": "USD",
        "debit_minor": amount if side == "DR" else 0,
        "credit_minor": amount if side == "CR" else 0,
        "line_description": "",
        "journal_description": "",
        "source": "manual",
        "entity_id": "US01",
        "run_id": "p" * 32,
        "functional_amount_minor": amount,
        "functional_currency": "USD",
        "line_kind": "entry",
    }


@pytest.fixture
def ledger(lake: ReferenceBackend) -> ReferenceBackend:
    lake.load(
        "ledger.gl_rollup_lines",
        [
            rollup("2026-07", "1110", "DR", 1_000),
            rollup("2026-07", "4010", "CR", 1_000),
            rollup("2026-08", "1110", "DR", 500),
            rollup("2026-08", "4010", "CR", 500),
            rollup("2026-08", "1110", "DR", 99_999, entity="EU01"),  # never in US01's balances
            rollup("2026-08", "4010", "CR", 99_999, entity="EU01"),
        ],
    )
    lake.load(
        "ledger.journal_lines", [manual("2026-08", "5020", "DR", 200, 1), manual("2026-08", "2410", "CR", 200, 2)]
    )
    return lake


def test_balances_roll_forward_and_trial_balance_read_both_sources(
    client: TestClient, ledger: ReferenceBackend, ready: None
) -> None:
    out = client.get("/api/v1/gl/balances?entity_id=US01&period=2026-08", headers=h(AUDITOR)).json()
    assert {
        a["account"]: (a["opening_minor"], a["debits_minor"], a["credits_minor"], a["closing_minor"])
        for a in out["accounts"]
    } == {
        "1110": (1_000, 500, 0, 1_500),
        "4010": (-1_000, 0, 500, -1_500),
        "5020": (0, 200, 0, 200),
        "2410": (0, 0, 200, -200),
    }
    assert out["balanced"]
    rf = client.get("/api/v1/gl/roll-forward?entity_id=US01&from=2026-07&to=2026-09", headers=h(AUDITOR)).json()
    assert (rf["ties"], [p["period"] for p in rf["periods"]]) == (True, ["2026-07", "2026-08", "2026-09"])
    september = {a["account"]: a["opening_minor"] for a in rf["periods"][2]["accounts"]}
    assert september == {"1110": 1_500, "4010": -1_500, "5020": 200, "2410": -200}
    tb = client.get("/api/v1/gl/trial-balance?entity_id=US01&period=2026-08", headers=h(AUDITOR)).json()
    assert (tb["debits"], tb["credits"], tb["balanced"]) == (700, 700, True)
    assert client.get("/api/v1/gl/balances?entity_id=EU01&period=2026-08", headers=h(AUDITOR)).status_code == 404


def test_the_roll_forward_reports_a_break_and_a_rollup_without_functional_amounts_fails(
    client: TestClient, ledger: ReferenceBackend, ready: None
) -> None:
    rows = ledger.snapshots["ledger.gl_rollup_lines"][-1]
    odd = rollup("2026-08-adj", "1110", "DR", 7)  # sorts between August and September, so a period query misses it
    ledger.load("ledger.gl_rollup_lines", [*rows, odd, rollup("2026-08-adj", "4010", "CR", 7)])
    rf = client.get("/api/v1/gl/roll-forward?entity_id=US01&from=2026-07&to=2026-09", headers=h(AUDITOR)).json()
    assert not rf["ties"]
    assert {(b["account"], b["period"]) for b in rf["breaks"]} == {("1110", "2026-08"), ("4010", "2026-08")}
    nulls = rollup("2026-08", "1190", "DR", 1, functional_debit_minor=None, functional_credit_minor=None)
    ledger.load("ledger.gl_rollup_lines", [*rows, nulls])  # an account whose rollups have no functional amounts
    broken = client.get("/api/v1/gl/balances?entity_id=US01&period=2026-08", headers=h(AUDITOR))
    assert (broken.status_code, broken.json()["title"]) == (500, "A GL rollup has no functional amounts")


# ---------------------------------------------------------------------------------------------- the ERP's trial balance


def upload(client: TestClient, csv: str, period: str = "2026-08", user: str = ERP) -> Any:
    headers = h(user) | {"Content-Type": "text/csv"}
    return client.post(
        f"/api/v1/erp/trial-balances?entity_id=US01&period={period}", content=csv.encode(), headers=headers
    )


def tie_out(client: TestClient, period: str = "2026-08") -> dict[str, Any]:
    found: dict[str, Any] = client.get(
        f"/api/v1/gl/erp-tie-out?entity_id=US01&period={period}", headers=h(AUDITOR)
    ).json()
    return found


def test_an_erp_extract_ties_and_every_difference_is_reported(
    client: TestClient, engine: Engine, ledger: ReferenceBackend, ready: None
) -> None:
    assert client.get("/api/v1/gl/erp-tie-out?entity_id=US01&period=2026-08", headers=h(AUDITOR)).status_code == 404
    exact = "account,balance\n1110,15.00\n4010,-15.00\n5020,2.00\n2410,-2.00\n"
    accepted = upload(client, exact)
    assert (accepted.status_code, accepted.json()["version"], accepted.json()["line_count"]) == (201, 1, 4)
    assert tie_out(client)["ties"]
    erp_only = upload(client, exact + "6050,1.00\n2420,-1.00\n")  # an accrual booked only in the ERP
    report = tie_out(client)
    assert report["extract"]["version"] == 2
    assert {(i["account"], i["status"], i["difference_minor"]) for i in report["items"] if i["status"] != "ties"} == {
        ("6050", "erp_only", 100),
        ("2420", "erp_only", -100),
    }
    assert erp_only.status_code == 201
    upload(client, "account,balance\n1110,-15.00\n4010,-15.00\n5020,2.00\n")  # a sign error, and 2410 missing
    report = tie_out(client)
    assert {(i["account"], i["status"]) for i in report["items"] if i["status"] != "ties"} == {
        ("1110", "differs"),
        ("2410", "gl_only"),
    }
    stale = upload(client, "account,balance\n1110,10.00\n4010,-10.00\n")  # July's balances, uploaded as August's
    assert stale.status_code == 201
    assert {i["account"] for i in tie_out(client)["items"] if i["status"] != "ties"} == {"1110", "4010", "5020", "2410"}


def test_a_bad_extract_is_refused_with_its_rows_and_kept(
    client: TestClient, engine: Engine, ledger: ReferenceBackend, ready: None
) -> None:
    for csv, expected in (
        ("acct,balance\n1110,1.00\n", ("bad_columns", "/rows/1")),
        ("account,balance\n1110,1.001\n", ("invalid_balance", "/rows/2")),
        ("account,balance\n1110,1.00\n1110,2.00\n", ("duplicate_account", "/rows/3")),
        ("account,balance\n", ("empty", "/")),
        ("account,balance\nbad code!,1.00\n", ("invalid_account", "/rows/2")),
    ):
        r = upload(client, csv)
        assert r.status_code == 422, csv
        assert (r.json()["issues"][0]["code"], r.json()["issues"][0]["path"]) == expected
    with engine.connect() as conn:
        statuses = conn.execute(text("SELECT status FROM erp_trial_balances ORDER BY version")).scalars().all()
        assert conn.execute(text("SELECT count(*) FROM erp_trial_balance_lines")).scalar_one() == 0
    assert statuses == ["rejected"] * 5  # every upload is kept, and none counts
    listed = client.get("/api/v1/erp/trial-balances?entity_id=US01&period=2026-08", headers=h(AUDITOR)).json()
    assert [e["version"] for e in listed["items"]] == [1, 2, 3, 4, 5]
    assert upload(client, "account,balance\n1110,1.00\n", user=PREPARER).status_code == 403  # an integration only


def test_the_extract_parser_reads_each_currency_exactly_and_refuses_what_it_cannot_read() -> None:
    assert erp.parse(b"account,balance\n1110,1234\n", "JPY") == ({"1110": 1234}, [])
    assert erp.parse(b"account,balance\n1110,1.234\n", "KWD") == ({"1110": 1234}, [])
    assert erp.parse(b"\xef\xbb\xbfaccount,balance\n1110,-0.50\n", "USD") == ({"1110": -50}, [])  # a BOM is fine
    assert erp.parse(b"account,balance\n1110,1.5000\n", "USD") == ({"1110": 150}, [])  # trailing zeros are fine
    for body, code in (
        (b"\xff\xfe", "not_utf8"),
        (b"x" * (erp.MAX_BYTES + 1), "too_large"),
        (("account,balance\n" + "".join(f"A{n},1.00\n" for n in range(erp.MAX_ROWS + 1))).encode(), "too_many_rows"),
        (b"account,balance\n1110,1,234.56\n", "extra_fields"),  # an unquoted thousands separator...
        (b'account,balance\n1110,"1,234.56"\n', "invalid_balance"),  # ...and a quoted one
        (b"account,balance\n1110,sNaN\n", "invalid_balance"),
        (b"account,balance\n1110,Infinity\n", "invalid_balance"),
        (b"account,balance\n1110,1e999999999\n", "invalid_balance"),  # never builds a huge number
        (b"account,balance\n1110,92233720368547758.08\n", "invalid_balance"),  # 2**63 minor units: past BIGINT
        (b"account,balance\n1110,1.000000000000000000000000000001\n", "invalid_balance"),
    ):
        assert erp.parse(body, "USD")[1][0]["code"] == code, body[:40]
