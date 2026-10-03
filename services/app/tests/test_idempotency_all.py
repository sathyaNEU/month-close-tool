"""Idempotency and audit on every POST (Gate 4).

- The first call writes its audit events: one per change, and two when an activation retires the active version.
- A replay is byte-identical and writes nothing.
- Concurrent duplicates have one effect.
- A key is scoped to its concrete path.
"""

from __future__ import annotations

import base64
import threading
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from apptestkit import assign, journal_samples
from fastapi.testclient import TestClient
from journalkit import body as journal_body
from sqlalchemy import Engine, text
from test_journal_posting import play_job
from test_route_inventory import operations

from close_app.context import AppContext
from close_app.journals import tasks as journal_tasks
from close_contracts.default_sources import BY_ID as SOURCES
from close_contracts.templates import BY_ID as TEMPLATES
from close_platform.compute.fake import FakeComputeBackend
from close_platform.outbox import Relay
from close_platform.query.reference import ReferenceBackend

pytestmark = pytest.mark.pg

AUTHOR, APPROVER, ADMIN, RUNNER, ERP = "4001", "4002", "4003", "4004", "4005"


@pytest.fixture
def people(engine: Engine) -> None:
    for scope in ("US01", "GLOBAL"):
        assign(engine, AUTHOR, "config_author", scope)
        assign(engine, APPROVER, "config_approver", scope)
    assign(engine, ADMIN, "platform_admin", "GLOBAL")
    assign(engine, RUNNER, "match_preparer", "US01")
    assign(engine, AUTHOR, "journal_preparer", "US01")  # Stage 7: prepares journals...
    assign(engine, APPROVER, "journal_approver_l3", "US01")  # ...another approves them...
    assign(engine, ADMIN, "controller", "US01")  # ...and the controller manages periods and posts
    assign(engine, ERP, "integration_client", "US01")  # Stage 7b: the ERP confirms what was posted
    assign(engine, "1001", "journal_preparer", "US01")  # the sample recurring journal's preparer
    assign(engine, "1004", "controller", "US01")  # Stage 8b: the sample checklist's other preparer and reviewers
    assign(engine, "1005", "controller", "US01")  # close.review, without joining the journal approvers


def _events(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM audit_events")).scalar_one())


class Replayer:
    """Sends each POST twice with one key: the first call writes ``events`` audit events, the second is a pure
    replay that writes none."""

    def __init__(self, client: TestClient, engine: Engine) -> None:
        self.client, self.engine = client, engine
        self.seen: set[tuple[str, str]] = set()

    def post(
        self,
        template: str,
        url: str,
        user: str,
        body: Any = None,
        if_match: str | None = None,
        *,
        writes: int = 1,
        csv: str | None = None,
    ) -> Any:
        headers = {"X-Forwarded-User": user, "X-Forwarded-Email": f"{user}@x", "Idempotency-Key": f"k-{uuid.uuid4()}"}
        if if_match:
            headers["If-Match"] = if_match
        if csv is not None:
            headers["Content-Type"] = "text/csv"
        before = _events(self.engine)
        send = {"content": csv.encode()} if csv is not None else {"json": body}
        first = self.client.post(url, headers=headers, **send)
        assert first.status_code < 300, (url, first.status_code, first.text)
        events = _events(self.engine)
        assert events - before == writes, f"{url}: wrote {events - before} audit events, expected {writes}"
        again = self.client.post(url, headers=headers, **send)
        assert again.content == first.content, url
        assert again.status_code == first.status_code
        assert again.headers["Idempotent-Replayed"] == "true"
        assert _events(self.engine) == events, f"{url}: the replay had an effect"
        self.seen.add(("POST", template))
        return first.json()


DS, MS = "/api/v1/data-sources", "/api/v1/match-sets"
V = "/{config_id}/versions/{version}"


def _flow(r: Replayer, base: str, config_id: str, spec: dict[str, Any], day: str) -> dict[str, Any]:
    v = r.post(base, base, AUTHOR, {"effective_from": day, "spec": spec})
    for verb, user in (("submit", AUTHOR), ("approve", APPROVER), ("activate", APPROVER)):
        v = r.post(f"{base}{V}:{verb}", f"{base}/{config_id}/versions/{v['version']}:{verb}", user, None, v["etag"])
    return v


def _journaling_body(resource: str, config_id: str) -> dict[str, Any]:
    ids = ("recurring_id", "type_id", "definition_id", "mapping_id", "chart_id", "dimension_set_id", "entity_set_id",
           "matrix_id", "template_id", "checklist_id", "rules_id")  # fmt: skip
    for res, body in journal_samples():
        spec_id = next((body["spec"][k] for k in ids if k in body["spec"]), "accounting_policy")
        if res == resource and spec_id == config_id:
            return body
    raise KeyError((resource, config_id))


# In activation order: a definition needs the chart, the dimensions, the policy, its mapping and its event type to
# be active; retiring in reverse order retires each user before what it uses.
JOURNALING = [
    (resource, config_id, _journaling_body(resource, config_id))
    for resource, config_id in (
        ("accounts", "sample_chart"),
        ("dimensions", "sample_dimensions"),
        ("programs", "accounting_policy"),
        ("account-mappings", "sample_psp_receivable_account"),
        ("event-types", "sample_order_captured"),
        ("auto-journal-definitions", "sample_order_capture"),
    )
]


def _journaling_flows(r: Replayer, between: Callable[[], None] | None = None) -> None:
    """Activate each journaling configuration in dependency order, run ``between``, then (in reverse) add a version,
    reject it, and retire the first one."""
    active = {}
    for resource, config_id, body in JOURNALING:
        active[resource] = _flow(r, f"/api/v1/{resource}", config_id, body["spec"], body["effective_from"])
    if between is not None:
        between()
    for resource, config_id, body in reversed(JOURNALING):
        base = f"/api/v1/{resource}"
        v2 = r.post(
            f"{base}/{{config_id}}/versions",
            f"{base}/{config_id}/versions",
            AUTHOR,
            body | {"effective_from": "2026-11-01"},
        )
        v2 = r.post(f"{base}{V}:submit", f"{base}/{config_id}/versions/2:submit", AUTHOR, None, v2["etag"])
        r.post(
            f"{base}{V}:reject", f"{base}/{config_id}/versions/2:reject", APPROVER, {"reason": "not yet"}, v2["etag"]
        )
        retire = {"effective_to": "2027-01-01"}
        r.post(f"{base}{V}:retire", f"{base}/{config_id}/versions/1:retire", APPROVER, retire, active[resource]["etag"])


def _versions_and_rejection(r: Replayer, resource: str, config_id: str, body: dict[str, Any], version: int) -> None:
    base = f"/api/v1/{resource}"
    new = r.post(
        f"{base}/{{config_id}}/versions",
        f"{base}/{config_id}/versions",
        AUTHOR,
        body | {"effective_from": "2026-11-01"},
    )
    new = r.post(f"{base}{V}:submit", f"{base}/{config_id}/versions/{version}:submit", AUTHOR, None, new["etag"])
    r.post(
        f"{base}{V}:reject", f"{base}/{config_id}/versions/{version}:reject", APPROVER, {"reason": "later"}, new["etag"]
    )


def _workflow_configs(r: Replayer) -> dict[str, dict[str, Any]]:
    """The Stage 7 kinds. The entity set is retired while nothing uses it yet, and a second version goes live."""
    entities = _journaling_body("entities", "sample_entities")
    base = "/api/v1/entities"
    v1 = _flow(r, base, "sample_entities", entities["spec"], entities["effective_from"])
    retire = {"effective_to": "2026-07-01"}
    r.post(f"{base}{V}:retire", f"{base}/sample_entities/versions/1:retire", APPROVER, retire, v1["etag"])
    v2 = r.post(
        f"{base}/{{config_id}}/versions",
        f"{base}/sample_entities/versions",
        AUTHOR,
        entities | {"effective_from": "2026-07-01"},
    )
    for verb, user in (("submit", AUTHOR), ("approve", APPROVER), ("activate", APPROVER)):
        v2 = r.post(f"{base}{V}:{verb}", f"{base}/sample_entities/versions/2:{verb}", user, None, v2["etag"])
    _versions_and_rejection(r, "entities", "sample_entities", entities, 3)
    active = {}
    for resource, config_id in (
        ("approval-matrices", "sample_approval_matrix"),
        ("journal-templates", "sample_standard_journal"),
        ("recurring-journals", "sample_reserve_top_up"),
    ):
        body = _journaling_body(resource, config_id)
        active[resource] = _flow(r, f"/api/v1/{resource}", config_id, body["spec"], body["effective_from"])
        _versions_and_rejection(r, resource, config_id, body, 2)
    return active


def _journals(r: Replayer, engine: Engine) -> None:
    """Every journal, batch, period, delegation and notification POST."""
    per = "/api/v1/periods"
    r.post(per, per, ADMIN, {"entity_id": "US01", "period": "2026-09"})
    august = r.post(per, per, ADMIN, {"entity_id": "US01", "period": "2026-08"})
    move = per + "/{entity_id}/{period}:{verb}"
    for verb, extra in (("close", None), ("reopen", {"reason": "a late invoice"}), ("close", None), ("lock", None)):
        august = r.post(move.replace("{verb}", verb), f"{per}/US01/2026-08:{verb}", ADMIN, extra, august["etag"])
    j_ = "/api/v1/journals"
    one = "/api/v1/journals/{journal_id}"
    j = r.post(j_, j_, AUTHOR, journal_body())
    url = f"{j_}/{j['journal_id']}"
    pdf = {
        "file_name": "support.pdf",
        "content_type": "application/pdf",
        "content_base64": base64.b64encode(b"%PDF").decode(),
    }
    att = r.post(one + "/attachments", url + "/attachments", AUTHOR, pdf)
    r.post(one + "/attachments/{attachment_id}:remove", f"{url}/attachments/{att['attachment_id']}:remove", AUTHOR)
    for verb, user, extra in (
        ("submit", AUTHOR, None), ("recall", AUTHOR, None), ("submit", AUTHOR, None),
        ("reject", APPROVER, {"reason": "wrong account"}), ("submit", AUTHOR, None), ("approve", APPROVER, None),
    ):  # fmt: skip
        j = r.post(f"{one}:{verb}", f"{url}:{verb}", user, extra, j["etag"])
    back = r.post(one + ":reverse", url + ":reverse", AUTHOR, {"accounting_date": "2026-09-30"})
    r.post(one + ":cancel", f"{j_}/{back['journal_id']}:cancel", AUTHOR, None, back["etag"])
    csv = (
        "journal_key,template_id,accounting_date,description,account,side,amount\n"
        "J1,sample_standard_journal,2026-09-10,Imported carrier accrual,5020,DR,12.34\n"
        "J1,,,,2410,CR,12.34\n"
    )
    r.post("/api/v1/journal-imports", "/api/v1/journal-imports?entity_id=US01", AUTHOR, csv=csv, writes=2)
    b_ = "/api/v1/journal-batches"
    batch_one = b_ + "/{batch_id}"
    batch = r.post(b_, b_, AUTHOR, {"entity_id": "US01", "description": "September batch"})
    burl = f"{b_}/{batch['batch_id']}"
    member, spare = r.post(j_, j_, AUTHOR, journal_body()), r.post(j_, j_, AUTHOR, journal_body())
    batch = r.post(
        batch_one + "/journals", burl + "/journals", AUTHOR, {"journal_id": member["journal_id"]}, batch["etag"]
    )
    batch = r.post(
        batch_one + "/journals", burl + "/journals", AUTHOR, {"journal_id": spare["journal_id"]}, batch["etag"]
    )
    remove = f"{burl}/journals/{spare['journal_id']}:remove"
    batch = r.post(batch_one + "/journals/{journal_id}:remove", remove, AUTHOR, None, batch["etag"])
    for verb, user, extra in (
        ("submit", AUTHOR, None), ("recall", AUTHOR, None), ("submit", AUTHOR, None),
        ("reject", APPROVER, {"reason": "split it"}), ("submit", AUTHOR, None), ("approve", APPROVER, None),
    ):  # fmt: skip
        batch = r.post(f"{batch_one}:{verb}", f"{burl}:{verb}", user, extra, batch["etag"], writes=2)
    spare_batch = r.post(b_, b_, AUTHOR, {"entity_id": "US01", "description": "Not needed"})
    r.post(f"{batch_one}:cancel", f"{b_}/{spare_batch['batch_id']}:cancel", AUTHOR, None, spare_batch["etag"])
    window = {"delegate": RUNNER, "entity_ids": ["US01"], "starts_on": "2026-01-01", "ends_on": "2027-12-31"}
    d = r.post("/api/v1/delegations", "/api/v1/delegations", APPROVER, window)
    r.post("/api/v1/delegations/{delegation_id}:revoke", f"/api/v1/delegations/{d['delegation_id']}:revoke", APPROVER)
    waiting = r.post(j_, j_, AUTHOR, journal_body())
    waiting = r.post(f"{one}:submit", f"{j_}/{waiting['journal_id']}:submit", AUTHOR, None, waiting["etag"])
    with engine.connect() as conn:
        since = conn.execute(
            text("SELECT level_since FROM journals WHERE journal_id = :id"), {"id": waiting["journal_id"]}
        ).scalar_one()
    assert journal_tasks.run_reminders(engine, as_of=since + timedelta(hours=25)) == 1  # to the one approver
    with engine.connect() as conn:
        note = conn.execute(
            text("SELECT notification_id FROM notifications WHERE user_id = :u"), {"u": APPROVER}
        ).scalar_one()
    r.post("/api/v1/notifications/{notification_id}:read", f"/api/v1/notifications/{note}:read", APPROVER)


def _posting(r: Replayer, ctx: AppContext, fake: FakeComputeBackend, lake: ReferenceBackend) -> None:
    """Stage 7b: a posting run over the approved journals, its job played, then the ERP write-back."""
    run = r.post("/api/v1/posting/runs", "/api/v1/posting/runs", ADMIN, {"entity_id": "US01"})
    assert run["journal_count"] >= 1
    relay = Relay(ctx.engine, ctx.coordinator.handlers(), instance_id="idempotency-relay")
    play_job(ctx.engine, relay, ctx, fake, lake, run["run_id"])
    with ctx.engine.connect() as conn:
        posted = conn.execute(text("SELECT journal_id FROM journals WHERE status = 'posted' LIMIT 1")).scalar_one()
    entries = {"entries": [{"journal_id": str(posted), "document_id": "ERP-US01-000001"}]}
    assert r.post("/api/v1/posting/status", "/api/v1/posting/status", ERP, entries)["confirmed"][0]["document_id"]


def _ledger(r: Replayer) -> None:
    """Stage 8a: a journaling run (the journaling configuration and entity set are active here) and an ERP extract."""
    run = r.post("/api/v1/journaling/runs", "/api/v1/journaling/runs", ADMIN, {"entity_id": "US01"})
    assert run["status"] == "running"
    extract = r.post(
        "/api/v1/erp/trial-balances",
        "/api/v1/erp/trial-balances?entity_id=US01&period=2026-09",
        ERP,
        csv="account,balance\n1110,1.00\n4010,-1.00\n",
    )
    assert (extract["status"], extract["version"]) == ("accepted", 1)


def _close(r: Replayer, client: TestClient) -> None:
    """Stage 8b: the close configuration, an October checklist taken through every task move, a soft close, and
    September's flux explained and reviewed (its posted journals are in the lake)."""
    active = {}
    for resource, config_id in (("close-checklists", "sample_month_end_close"), ("flux-rules", "sample_flux_rules")):
        body = _journaling_body(resource, config_id)
        active[resource] = _flow(r, f"/api/v1/{resource}", config_id, body["spec"], body["effective_from"])
        _versions_and_rejection(r, resource, config_id, body, 2)
    per = "/api/v1/periods"
    october = r.post(per, per, ADMIN, {"entity_id": "US01", "period": "2026-10"})
    made = r.post(
        "/api/v1/close/checklists", "/api/v1/close/checklists", ADMIN, {"entity_id": "US01", "period": "2026-10"}
    )
    assert len(made["tasks"]) == 9
    template = "/api/v1/close/tasks/{entity_id}/{period}/{task_id}"

    def move(task_id: str, verb: str, user: str, body: Any = None) -> Any:
        return r.post(f"{template}:{verb}", f"/api/v1/close/tasks/US01/2026-10/{task_id}:{verb}", user, body)

    move("shipping_label_cutoff", "prepare", "1001", {"note": "labels cut off"})
    move("shipping_label_cutoff", "review", "1004")
    move("carrier_accrual", "prepare", "1001")
    move("carrier_accrual", "send-back", "1005", {"reason": "attach the label count"})
    checked = move("journals_settled", "check", ADMIN)
    assert (checked["passed"], checked["status"]) == (True, "done")
    move("journals_settled", "reopen", ADMIN, {"reason": "check it again after the accrual"})
    move("seller_payables_review", "reassign", ADMIN, {"reviewer": "1005"})
    soft = per + "/{entity_id}/{period}:soft-close"
    r.post(soft, f"{per}/US01/2026-10:soft-close", ADMIN, None, october["etag"])
    reader = {"X-Forwarded-User": AUTHOR, "X-Forwarded-Email": "a@x"}
    lines = client.get("/api/v1/flux?entity_id=US01&period=2026-09", headers=reader).json()["lines"]
    assert lines
    for line in lines:
        why = {"entity_id": "US01", "period": "2026-09", "account": line["account"], "explanation": "posted accruals"}
        r.post("/api/v1/flux/explanations", "/api/v1/flux/explanations", AUTHOR, why)
    r.post("/api/v1/flux/reviews", "/api/v1/flux/reviews", APPROVER, {"entity_id": "US01", "period": "2026-09"})
    for resource, config_id in (("close-checklists", "sample_month_end_close"), ("flux-rules", "sample_flux_rules")):
        base = f"/api/v1/{resource}"
        retire = {"effective_to": "2027-01-01"}
        r.post(f"{base}{V}:retire", f"{base}/{config_id}/versions/1:retire", APPROVER, retire, active[resource]["etag"])


def _retire_workflow(r: Replayer, active: dict[str, dict[str, Any]]) -> None:
    """Users before what they use: the recurring journal, the template, then the matrix."""
    for resource, config_id in (
        ("recurring-journals", "sample_reserve_top_up"),
        ("journal-templates", "sample_standard_journal"),
        ("approval-matrices", "sample_approval_matrix"),
    ):
        base = f"/api/v1/{resource}"
        retire = {"effective_to": "2027-01-01"}
        r.post(f"{base}{V}:retire", f"{base}/{config_id}/versions/1:retire", APPROVER, retire, active[resource]["etag"])


def test_every_post_replays_byte_identically_without_a_second_effect(
    client: TestClient,
    engine: Engine,
    people: None,
    ctx: AppContext,
    fake: FakeComputeBackend,
    lake: ReferenceBackend,
) -> None:
    r = Replayer(client, engine)
    ms_spec = TEMPLATES["t1_orders_stripe"].model_dump(mode="json") | {"entity_ids": ["US01"]}
    for source_id in ("orders", "stripe_balance_transactions"):
        _flow(r, DS, source_id, SOURCES[source_id].model_dump(mode="json"), "2026-10-01")
    active = _flow(r, MS, "t1_orders_stripe", ms_spec, "2026-10-01")
    for base, config_id, spec in (
        (DS, "orders", SOURCES["orders"].model_dump(mode="json")),
        (MS, "t1_orders_stripe", ms_spec),
    ):
        v2 = r.post(
            f"{base}/{{config_id}}/versions",
            f"{base}/{config_id}/versions",
            AUTHOR,
            {"effective_from": "2026-11-01", "spec": spec},
        )
        v2 = r.post(f"{base}{V}:submit", f"{base}/{config_id}/versions/2:submit", AUTHOR, None, v2["etag"])
        r.post(
            f"{base}{V}:reject", f"{base}/{config_id}/versions/2:reject", APPROVER, {"reason": "not yet"}, v2["etag"]
        )
    retired = client.get(
        f"{MS}/t1_orders_stripe/versions/1", headers={"X-Forwarded-User": AUTHOR, "X-Forwarded-Email": "a@x"}
    )
    r.post(
        f"{MS}{V}:retire",
        f"{MS}/t1_orders_stripe/versions/1:retire",
        APPROVER,
        {"effective_to": "2027-01-01"},
        retired.headers["ETag"],
    )
    ds_active = client.get(
        f"{DS}/stripe_balance_transactions/versions/1", headers={"X-Forwarded-User": AUTHOR, "X-Forwarded-Email": "a@x"}
    )
    r.post(
        f"{DS}{V}:retire",
        f"{DS}/stripe_balance_transactions/versions/1:retire",
        APPROVER,
        {"effective_to": "2027-01-01"},
        ds_active.headers["ETag"],
    )

    def stage_7() -> None:
        active = _workflow_configs(r)
        _journals(r, engine)
        _posting(r, ctx, fake, lake)
        _close(r, client)  # before _ledger, whose journaling run stays in progress and would hold off the soft close
        _ledger(r)
        _retire_workflow(r, active)

    _journaling_flows(r, between=stage_7)
    reader = {"X-Forwarded-User": AUTHOR, "X-Forwarded-Email": "a@x"}
    v2 = client.get(f"{DS}/orders/versions/2", headers=reader).json()  # rejected back to draft above
    v2 = r.post(f"{DS}{V}:submit", f"{DS}/orders/versions/2:submit", AUTHOR, None, v2["etag"])
    v2 = r.post(f"{DS}{V}:approve", f"{DS}/orders/versions/2:approve", APPROVER, None, v2["etag"])
    # Activating over the active version 1 retires it: two audit events, the retirement and the activation.
    v2 = r.post(f"{DS}{V}:activate", f"{DS}/orders/versions/2:activate", APPROVER, None, v2["etag"], writes=2)
    assert v2["status"] == "active"
    granted = r.post(
        "/api/v1/role-assignments",
        "/api/v1/role-assignments",
        ADMIN,
        {"user_id": "new", "role": "auditor", "scope": "US01"},
    )
    aid = granted["assignment_id"]
    r.post("/api/v1/role-assignments/{assignment_id}:revoke", f"/api/v1/role-assignments/{aid}:revoke", ADMIN)
    run = r.post("/api/v1/smoke/runs", "/api/v1/smoke/runs", RUNNER, {"rows": 5})
    r.post("/api/v1/smoke/runs/{run_id}:cancel", f"/api/v1/smoke/runs/{run['run_id']}:cancel", RUNNER)
    assert active["status"] == "active"
    served = {(m, p) for m, p, _ in operations() if m == "POST"}
    assert r.seen == served


def test_concurrent_duplicates_have_one_effect(client: TestClient, engine: Engine, people: None) -> None:
    headers = {"X-Forwarded-User": ADMIN, "X-Forwarded-Email": "a@x", "Idempotency-Key": "concurrent-grant-1"}
    body = {"user_id": "dup", "role": "controller", "scope": "EU01"}
    results: list[Any] = []

    def send() -> None:
        results.append(client.post("/api/v1/role-assignments", json=body, headers=headers))

    threads = [threading.Thread(target=send) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert {r.status_code for r in results} == {201}
    assert len({r.content for r in results}) == 1
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM role_assignments WHERE user_id = 'dup'")).scalar_one() == 1
    assert _events(engine) == 1


def test_a_key_is_bound_to_its_body_and_its_path(client: TestClient, engine: Engine, people: None) -> None:
    headers = {"X-Forwarded-User": AUTHOR, "X-Forwarded-Email": "a@x", "Idempotency-Key": "shared-key-1"}
    first = client.post(
        DS, json={"effective_from": "2026-10-01", "spec": SOURCES["orders"].model_dump(mode="json")}, headers=headers
    )
    assert first.status_code == 201
    other_body = {"effective_from": "2026-10-02", "spec": SOURCES["orders"].model_dump(mode="json")}
    assert client.post(DS, json=other_body, headers=headers).status_code == 422
    etag = first.json()["etag"]
    submit = client.post(f"{DS}/orders/versions/1:submit", headers=headers | {"If-Match": etag})
    assert submit.status_code == 200  # the same key on another path is a different request
    assert "Idempotent-Replayed" not in submit.headers


def test_an_edit_writes_one_audit_event(client: TestClient, engine: Engine, people: None) -> None:
    headers = {"X-Forwarded-User": AUTHOR, "X-Forwarded-Email": "a@x", "Idempotency-Key": "edit-audit-1"}
    body = {"effective_from": "2026-10-01", "spec": SOURCES["orders"].model_dump(mode="json")}
    created = client.post(DS, json=body, headers=headers).json()
    before = _events(engine)
    edited = client.put(
        f"{DS}/orders/versions/1",
        json=body | {"effective_from": "2026-10-09"},
        headers={"X-Forwarded-User": AUTHOR, "X-Forwarded-Email": "a@x", "If-Match": created["etag"]},
    )
    assert edited.status_code == 200
    assert _events(engine) - before == 1
    with engine.connect() as conn:
        action = conn.execute(text("SELECT action FROM audit_events ORDER BY seq DESC LIMIT 1")).scalar_one()
    assert action == "data_source.edit"
