"""The authorization matrix (Gate 4): every role x every operation x every scope, 0 unexpected allows and
0 unexpected denies.

The expectations below are written by hand from the Stage 4 plan, and deliberately do not import
``close_app.authz``, so a change to the enforced policy fails here until the expectations are reviewed too.

- **Allowed** means the request got past authorization: any status except 401, 403 and 404. A 409 or 412
  from the object's state counts as allowed.
- **Denied** means a 403, or a 404 for an object in none of the caller's entities.
- **Setup.** The objects exist before the matrix runs: the ``orders`` data source (version 1, a draft), a US01
  match set (version 1, a draft), and a smoke run. From Stage 7: the sample journal setup made active, an open
  September period, a draft journal with an attachment, a batch, a rejected import, a delegation and a
  notification, all in US01, and (7b) a posting run over a second, approved journal, so the ledger lease is held.
- **Ledger reads** (``/gl/...``) are called with ``entity_id=US01``, which they require.
- **Authorization comes first.** Every operation checks permission before an object's state or ETag, so these
  fixed objects give the same verdicts whatever state earlier calls leave them in.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from apptestkit import assign, journal_samples
from fastapi.testclient import TestClient
from journalkit import APPROVER_1, CONTROLLER, PREPARER, approved, create, open_period, set_up
from journalkit import body as journal_body
from journalkit import h as journal_headers
from sqlalchemy import Engine, text
from test_route_inventory import operations

from close_contracts.default_sources import BY_ID as SOURCES
from close_contracts.templates import BY_ID as TEMPLATES

pytestmark = pytest.mark.pg

ROLES = (
    "platform_admin", "config_author", "config_approver", "match_preparer", "match_approver", "journal_preparer",
    "journal_approver_l1", "journal_approver_l2", "journal_approver_l3", "controller", "auditor", "integration_client",
)  # fmt: skip
SCOPES = ("US01", "EU01", "GLOBAL")
EVERYONE = frozenset(ROLES)
AUTHORS = frozenset({"config_author"})
APPROVERS = frozenset({"config_approver"})
OVERSEERS = frozenset({"platform_admin", "controller", "auditor"})  # audit.read and access.read
ADMINS = frozenset({"platform_admin"})
RUNNERS = frozenset({"match_preparer", "integration_client"})
READERS = frozenset(
    {"journal_preparer", "journal_approver_l1", "journal_approver_l2", "journal_approver_l3", "controller", "auditor"}
)
PREPARERS = frozenset({"journal_preparer"})
APPROVE_ANY = frozenset({"journal_approver_l1", "journal_approver_l2", "journal_approver_l3"})
CONTROLLERS = frozenset({"controller"})
EXPORTERS = frozenset({"controller", "auditor"})
INTEGRATIONS = frozenset({"integration_client"})  # the ERP feed and write-back
CLOSE_PREPARERS = frozenset({"journal_preparer", "controller"})  # Stage 8b: close.prepare
CLOSE_REVIEWERS = APPROVE_ANY | {"controller"}  # close.review

Rule = Callable[[str, str], bool]


def anywhere(group: frozenset[str]) -> Rule:
    return lambda role, _scope: role in group


def global_(group: frozenset[str]) -> Rule:
    return lambda role, scope: role in group and scope == "GLOBAL"


def in_us01(group: frozenset[str]) -> Rule:
    return lambda role, scope: role in group and scope == "US01"


def in_an_entity(group: frozenset[str]) -> Rule:
    """Exports cover the caller's own entities, so any entity scope may read its own."""
    return lambda role, scope: role in group and scope != "GLOBAL"


def nobody(_role: str, _scope: str) -> bool:
    return False


DS = "/api/v1/data-sources"
MS = "/api/v1/match-sets"
MS_ID = "t1_orders_stripe"
SETUP_AUTHOR = "setup-author"

# (method, OpenAPI path, rule, body)
# Global configurations: (resource, configuration kind, the configuration the matrix acts on).
GLOBAL_RESOURCES: dict[str, tuple[str, str]] = {
    "data-sources": ("data_source", "orders"),
    "accounts": ("chart_of_accounts", "sample_chart"),
    "dimensions": ("dimension_set", "sample_dimensions"),
    "account-mappings": ("account_mapping", "sample_psp_receivable_account"),
    "event-types": ("event_type", "sample_order_captured"),
    "auto-journal-definitions": ("auto_journal_definition", "sample_order_capture"),
    "programs": ("accounting_policy", "accounting_policy"),
    "entities": ("entity_set", "sample_entities"),
    "approval-matrices": ("approval_matrix", "sample_approval_matrix"),
    "journal-templates": ("journal_template", "sample_standard_journal"),
    "close-checklists": ("close_checklist", "sample_month_end_close"),  # Stage 8b
    "flux-rules": ("flux_rules", "sample_flux_rules"),
}
ENTITY_RESOURCES: dict[str, tuple[str, str]] = {
    "match-sets": ("match_set", "t1_orders_stripe"),
    "recurring-journals": ("recurring_journal", "sample_reserve_top_up"),
}


def _global_rows(resource: str) -> list[tuple[str, str, Rule, Any]]:
    """The same hand-written expectations for every global configuration."""
    base, body = f"/api/v1/{resource}", f"body:{resource}"
    version = base + "/{config_id}/versions/{version}"
    return [
        ("GET", base, anywhere(EVERYONE), None),
        ("POST", base, global_(AUTHORS), body),
        ("GET", base + "/{config_id}/versions", anywhere(EVERYONE), None),
        ("POST", base + "/{config_id}/versions", global_(AUTHORS), body),
        ("GET", version, anywhere(EVERYONE), None),
        ("PUT", version, global_(AUTHORS), body),
        ("POST", version + ":submit", global_(AUTHORS), None),
        ("POST", version + ":approve", global_(APPROVERS), None),
        ("POST", version + ":reject", global_(APPROVERS), {"reason": "matrix"}),
        ("POST", version + ":activate", global_(APPROVERS), None),
        ("POST", version + ":retire", global_(APPROVERS), {"effective_to": "2027-01-01"}),
    ]


def _entity_rows(resource: str, body: str) -> list[tuple[str, str, Rule, Any]]:
    """Configurations that belong to entities (US01 here): match sets and recurring journals."""
    base = f"/api/v1/{resource}"
    version = base + "/{config_id}/versions/{version}"
    return [
        ("GET", base, in_us01(EVERYONE), None),
        ("POST", base, in_us01(AUTHORS), body),
        ("GET", base + "/{config_id}/versions", in_us01(EVERYONE), None),
        ("POST", base + "/{config_id}/versions", in_us01(AUTHORS), body),
        ("GET", version, in_us01(EVERYONE), None),
        ("PUT", version, in_us01(AUTHORS), body),
        ("POST", version + ":submit", in_us01(AUTHORS), None),
        ("POST", version + ":approve", in_us01(APPROVERS), None),
        ("POST", version + ":reject", in_us01(APPROVERS), {"reason": "matrix"}),
        ("POST", version + ":activate", in_us01(APPROVERS), None),
        ("POST", version + ":retire", in_us01(APPROVERS), {"effective_to": "2027-01-01"}),
    ]


J, B, P = "/api/v1/journals", "/api/v1/journal-batches", "/api/v1/periods"
JID, BID, PID = J + "/{journal_id}", B + "/{batch_id}", P + "/{entity_id}/{period}"
R = "/api/v1/posting/runs"
JR = "/api/v1/journaling/runs"  # Stage 8a
T = "/api/v1/close/tasks/{entity_id}/{period}/{task_id}"  # Stage 8b
LISTED = {J: "journal_id", B: "batch_id", P: "period", R: "run_id", JR: "run_id"}  # filtered: allowed means listed
QUERY = {  # the ledger reads, the ERP extract, the checklist and the flux need their entity and period
    "/api/v1/close/checklists": "entity_id=US01&period=2026-09",
    "/api/v1/flux": "entity_id=US01&period=2026-09",
    "/api/v1/gl/balances": "entity_id=US01&period=2026-09",
    "/api/v1/gl/roll-forward": "entity_id=US01&from=2026-08&to=2026-09",
    "/api/v1/gl/erp-tie-out": "entity_id=US01&period=2026-09",
    "/api/v1/erp/trial-balances": "entity_id=US01&period=2026-09",
}
JOURNAL_ROWS: list[tuple[str, str, Rule, Any]] = [
    ("GET", J, in_us01(READERS), None),
    ("POST", J, in_us01(PREPARERS), "journal"),
    ("GET", JID, in_us01(READERS), None),
    ("PUT", JID, in_us01(PREPARERS), "journal"),
    ("POST", JID + ":submit", in_us01(PREPARERS), None),
    ("POST", JID + ":approve", in_us01(APPROVE_ANY), None),
    ("POST", JID + ":reject", in_us01(APPROVE_ANY), {"reason": "matrix"}),
    ("POST", JID + ":recall", in_us01(PREPARERS), None),
    ("POST", JID + ":cancel", in_us01(PREPARERS | CONTROLLERS), None),
    ("POST", JID + ":reverse", in_us01(PREPARERS), None),
    ("GET", JID + "/history", in_us01(READERS), None),
    ("GET", JID + "/attachments", in_us01(READERS), None),
    ("POST", JID + "/attachments", in_us01(PREPARERS), "attachment"),
    ("GET", JID + "/attachments/{attachment_id}", in_us01(READERS), None),
    ("POST", JID + "/attachments/{attachment_id}:remove", in_us01(PREPARERS), None),
    ("POST", "/api/v1/journal-imports", in_us01(PREPARERS), "csv"),
    ("GET", "/api/v1/journal-imports/{import_id}", in_us01(READERS), None),
    ("GET", B, in_us01(READERS), None),
    ("POST", B, in_us01(PREPARERS), "batch"),
    ("GET", BID, in_us01(READERS), None),
    ("POST", BID + "/journals", in_us01(PREPARERS), "member"),
    ("POST", BID + "/journals/{journal_id}:remove", in_us01(PREPARERS), None),
    ("POST", BID + ":submit", in_us01(PREPARERS), None),
    ("POST", BID + ":approve", in_us01(APPROVE_ANY), None),
    ("POST", BID + ":reject", in_us01(APPROVE_ANY), {"reason": "matrix"}),
    ("POST", BID + ":recall", in_us01(PREPARERS), None),
    ("POST", BID + ":cancel", in_us01(PREPARERS), None),  # only its creator; the controller cancels journals
    ("GET", "/api/v1/journal-population", in_an_entity(EXPORTERS), None),
    ("GET", "/api/v1/journal-population/summary", in_an_entity(EXPORTERS), None),
    ("GET", "/api/v1/journal-exceptions", in_an_entity(EXPORTERS), None),
    ("GET", P, in_us01(READERS), None),
    ("POST", P, in_us01(CONTROLLERS), "period"),
    ("GET", PID, in_us01(READERS), None),
    ("POST", PID + ":soft-close", in_us01(CONTROLLERS), None),  # Stage 8b; the reopen below opens it again
    ("POST", PID + ":close", in_us01(CONTROLLERS), None),
    ("POST", PID + ":lock", in_us01(CONTROLLERS), None),
    ("POST", PID + ":reopen", in_us01(CONTROLLERS), {"reason": "matrix"}),
    ("GET", "/api/v1/delegations", anywhere(EVERYONE), None),
    ("POST", "/api/v1/delegations", in_us01(APPROVE_ANY), "delegation"),
    ("POST", "/api/v1/delegations/{delegation_id}:revoke", global_(ADMINS), None),
    ("GET", "/api/v1/notifications", anywhere(EVERYONE), None),
    ("POST", "/api/v1/notifications/{notification_id}:read", nobody, None),  # only its owner, who isn't in the matrix
]
POSTING_ROWS: list[tuple[str, str, Rule, Any]] = [
    ("POST", R, in_us01(CONTROLLERS), {"entity_id": "US01"}),
    ("GET", R, in_us01(READERS), None),
    ("GET", R + "/{run_id}", in_us01(READERS), None),
    ("POST", "/api/v1/posting/status", in_us01(INTEGRATIONS), "write-back"),
    ("GET", "/api/v1/posting/tie-out", in_an_entity(EXPORTERS), None),
    ("GET", "/api/v1/gl/trial-balance", in_us01(READERS), None),
    ("GET", "/api/v1/gl/lines", in_us01(READERS), None),
    ("GET", "/api/v1/gl/postings", in_us01(INTEGRATIONS), None),
    # Stage 7c: any journal reader, in an entity of theirs; the world's pair and day have a seeded rate
    ("GET", "/api/v1/fx-rates/{from_currency}/{to_currency}/{on}", in_an_entity(READERS), None),
    # Stage 8a: the general ledger
    ("POST", JR, in_us01(CONTROLLERS), {"entity_id": "US01"}),
    ("GET", JR, in_us01(READERS), None),
    ("GET", JR + "/{run_id}", in_us01(READERS), None),
    ("GET", "/api/v1/gl/balances", in_us01(READERS), None),
    ("GET", "/api/v1/gl/roll-forward", in_us01(READERS), None),
    ("POST", "/api/v1/erp/trial-balances", in_us01(INTEGRATIONS), "erp-csv"),
    ("GET", "/api/v1/erp/trial-balances", in_us01(EXPORTERS), None),
    ("GET", "/api/v1/gl/erp-tie-out", in_us01(EXPORTERS), None),
]

FLUX_LINE = {"entity_id": "US01", "period": "2026-09", "account": "1110", "explanation": "matrix"}
CLOSE_ROWS: list[tuple[str, str, Rule, Any]] = [  # Stage 8b: on an automatic task, so assignees never matter
    ("GET", "/api/v1/close/checklists", in_us01(READERS), None),
    ("POST", "/api/v1/close/checklists", in_us01(CONTROLLERS), {"entity_id": "US01", "period": "2026-09"}),
    ("GET", T, in_us01(READERS), None),
    ("POST", T + ":prepare", in_us01(CLOSE_PREPARERS), None),
    ("POST", T + ":check", in_us01(CLOSE_PREPARERS), None),
    ("POST", T + ":review", in_us01(CLOSE_REVIEWERS), None),
    ("POST", T + ":send-back", in_us01(CLOSE_REVIEWERS), {"reason": "matrix"}),
    ("POST", T + ":reopen", in_us01(CONTROLLERS), {"reason": "matrix"}),
    ("POST", T + ":reassign", in_us01(CONTROLLERS), {"reviewer": "matrix-reviewer"}),
    ("GET", "/api/v1/flux", in_us01(READERS), None),
    ("POST", "/api/v1/flux/explanations", in_us01(CLOSE_PREPARERS), FLUX_LINE),
    ("POST", "/api/v1/flux/reviews", in_us01(CLOSE_REVIEWERS), {"entity_id": "US01", "period": "2026-09"}),
]

MATRIX: list[tuple[str, str, Rule, Any]] = [
    *(row for resource in GLOBAL_RESOURCES for row in _global_rows(resource)),
    *_entity_rows("recurring-journals", "rj"),
    *JOURNAL_ROWS,
    *POSTING_ROWS,
    *CLOSE_ROWS,
    ("GET", MS, in_us01(EVERYONE), None),
    ("POST", MS, in_us01(AUTHORS), "ms"),
    ("GET", MS + "/{config_id}/versions", in_us01(EVERYONE), None),
    ("POST", MS + "/{config_id}/versions", in_us01(AUTHORS), "ms"),
    ("GET", MS + "/{config_id}/versions/{version}", in_us01(EVERYONE), None),
    ("PUT", MS + "/{config_id}/versions/{version}", in_us01(AUTHORS), "ms"),
    ("POST", MS + "/{config_id}/versions/{version}:submit", in_us01(AUTHORS), None),
    ("POST", MS + "/{config_id}/versions/{version}:approve", in_us01(APPROVERS), None),
    ("POST", MS + "/{config_id}/versions/{version}:reject", in_us01(APPROVERS), {"reason": "matrix"}),
    ("POST", MS + "/{config_id}/versions/{version}:activate", in_us01(APPROVERS), None),
    ("POST", MS + "/{config_id}/versions/{version}:retire", in_us01(APPROVERS), {"effective_to": "2027-01-01"}),
    ("GET", "/api/v1/audit-events", anywhere(OVERSEERS), None),
    ("GET", "/api/v1/users", global_(OVERSEERS), None),
    ("GET", "/api/v1/role-assignments", global_(OVERSEERS), None),
    ("POST", "/api/v1/role-assignments", global_(ADMINS), "grant"),
    ("POST", "/api/v1/role-assignments/{assignment_id}:revoke", global_(ADMINS), None),
    ("POST", "/api/v1/smoke/runs", anywhere(RUNNERS), {"rows": 10}),
    ("POST", "/api/v1/smoke/runs/{run_id}:cancel", anywhere(RUNNERS), None),
    ("GET", "/api/v1/whoami", anywhere(EVERYONE), None),
    ("GET", "/api/v1/roles", anywhere(EVERYONE), None),
    ("GET", "/api/v1/smoke/runs/{run_id}", anywhere(EVERYONE), None),
    ("GET", "/api/v1/smoke/runs/{run_id}/rows", anywhere(EVERYONE), None),
    ("GET", "/api/v1/smoke/runs/{run_id}/totals", anywhere(EVERYONE), None),
]


def _headers(user: str, **extra: str) -> dict[str, str]:
    return {"X-Forwarded-User": user, "X-Forwarded-Email": f"{user}@example.com", **extra}


def _ms_spec() -> dict[str, Any]:
    return TEMPLATES[MS_ID].model_dump(mode="json") | {"entity_ids": ["US01"]}


_IDS = (
    "recurring_id", "type_id", "definition_id", "mapping_id", "chart_id", "dimension_set_id", "entity_set_id",
    "matrix_id", "template_id", "checklist_id", "rules_id",
)  # fmt: skip
SAMPLE_BODIES = {
    (res, next((body["spec"][k] for k in _IDS if k in body["spec"]), "accounting_policy")): body
    for res, body in journal_samples()
}
CSV = "journal_key,template_id,accounting_date,description,account,side,amount\nJ1,nope,2026-09-10,x,5020,DR,1\n"
DELEGATION = {"delegate": PREPARER, "entity_ids": ["US01"], "starts_on": "2026-01-01", "ends_on": "2027-12-31"}


def _body(kind: Any) -> Any:
    if isinstance(kind, str) and kind in {"body:data-sources", "ds"}:
        return {"effective_from": "2026-10-01", "spec": SOURCES["orders"].model_dump(mode="json")}
    if isinstance(kind, str) and kind.startswith("body:"):
        resource = kind.removeprefix("body:")
        return SAMPLE_BODIES[(resource, GLOBAL_RESOURCES[resource][1])]
    if kind == "ms":
        return {"effective_from": "2026-10-01", "spec": _ms_spec()}
    if kind == "rj":
        return SAMPLE_BODIES[("recurring-journals", "sample_reserve_top_up")]
    if kind == "journal":
        return journal_body()
    if kind == "attachment":
        pdf = base64.b64encode(b"%PDF matrix").decode()
        return {"file_name": "m.pdf", "content_type": "application/pdf", "content_base64": pdf}
    if kind == "batch":
        return {"entity_id": "US01", "description": "matrix batch"}
    if kind == "period":
        return {"entity_id": "US01", "period": "2027-01"}
    if kind == "delegation":
        return DELEGATION
    if kind == "grant":
        return {"user_id": "matrix-target", "role": "auditor", "scope": "US01"}
    return kind


def _etag(engine: Engine, kind: str, config_id: str) -> str:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT row_version, content_hash FROM config_versions "
                "WHERE kind = :k AND config_id = :c AND version = 1"
            ),
            {"k": kind, "c": config_id},
        ).one()
    return f'"{row.row_version}-{row.content_hash[:16]}"'


def _fresh_assignment(engine: Engine) -> str:
    assignment_id = uuid.uuid4()
    with engine.begin() as tx:
        tx.execute(
            text(
                "INSERT INTO role_assignments (assignment_id, user_id, role, scope, granted_by) "
                "VALUES (:id, :u, 'auditor', 'EU01', 'matrix')"
            ),
            {"id": assignment_id, "u": f"revoke-{assignment_id}"},
        )
    return str(assignment_id)


def _journal_world(client: TestClient, engine: Engine) -> dict[str, str]:
    """The Stage 7 objects: an open period, a draft journal with an attachment, a batch, a rejected import, a
    delegation and a notification, all in US01."""
    open_period(engine)
    journal = client.post("/api/v1/journals", json=journal_body(), headers=journal_headers(PREPARER))
    assert journal.status_code == 201, journal.text
    journal_id = journal.json()["journal_id"]
    attached = client.post(
        f"/api/v1/journals/{journal_id}/attachments", json=_body("attachment"), headers=journal_headers(PREPARER)
    )
    batch = client.post("/api/v1/journal-batches", json=_body("batch"), headers=journal_headers(PREPARER))
    imported = client.post(
        "/api/v1/journal-imports?entity_id=US01",
        content=CSV.encode(),
        headers=journal_headers(PREPARER) | {"Content-Type": "text/csv"},
    )
    delegation = client.post("/api/v1/delegations", json=DELEGATION, headers=journal_headers(APPROVER_1))
    assert (attached.status_code, batch.status_code, imported.status_code, delegation.status_code) == (
        201,
        201,
        422,
        201,
    )
    posted = approved(client, create(client))
    run = client.post("/api/v1/posting/runs", json={"entity_id": "US01"}, headers=journal_headers(CONTROLLER))
    assert (run.status_code, run.json()["journal_count"]) == (202, 1), run.text
    assert posted["status"] == "approved"
    notification_id = uuid.uuid4()
    with engine.begin() as tx:
        tx.execute(
            text(
                "INSERT INTO notifications (notification_id, user_id, kind, dedup_key, entity_id, message) "
                "VALUES (:id, :u, 'reminder', :k, 'US01', 'matrix')"
            ),
            {"id": notification_id, "u": APPROVER_1, "k": f"matrix:{notification_id}"},
        )
    return {
        "journal_id": journal_id,
        "attachment_id": attached.json()["attachment_id"],
        "batch_id": batch.json()["batch_id"],
        "import_id": imported.json()["import_id"],
        "delegation_id": delegation.json()["delegation_id"],
        "notification_id": str(notification_id),
        "posting_run_id": run.json()["run_id"],
        **_ledger_world(engine),
        **_close_world(engine),
        "from_currency": "EUR",
        "to_currency": "USD",
        "on": "2026-09-15",
        "entity_id": "US01",
        "period": "2026-09",
    }


def _ledger_world(engine: Engine) -> dict[str, str]:
    """Stage 8a: a settled journaling run and an accepted ERP extract for US01, written directly."""
    run_id, extract_id, digest = uuid.uuid4(), uuid.uuid4(), "e" * 64
    with engine.begin() as tx:
        tx.execute(
            text(
                "INSERT INTO config_snapshots (snapshot_hash, purpose, configuration_hash, entity_id, content, "
                "created_by) VALUES (:h, 'journaling', :h, 'US01', '{}'::jsonb, 'matrix') ON CONFLICT DO NOTHING"
            ),
            {"h": digest},
        )
        tx.execute(
            text(
                "INSERT INTO journaling_runs (run_id, entity_id, requested_by, snapshot_hash, configuration_hash, "
                "status, reconciled_at) VALUES (:id, 'US01', 'matrix', :h, :h, 'reconciled', clock_timestamp())"
            ),
            {"id": run_id, "h": digest},
        )
        tx.execute(
            text(
                "INSERT INTO erp_trial_balances (extract_id, entity_id, period, version, status, sha256, currency, "
                "line_count, uploaded_by) VALUES (:id, 'US01', '2026-09', 1, 'accepted', :h, 'USD', 1, 'matrix')"
            ),
            {"id": extract_id, "h": digest},
        )
        tx.execute(
            text("INSERT INTO erp_trial_balance_lines (extract_id, account, balance_minor) VALUES (:id, '1110', 0)"),
            {"id": extract_id},
        )
    return {"journaling_run_id": str(run_id)}


def _close_world(engine: Engine) -> dict[str, str]:
    """Stage 8b: US01's September checklist with one automatic task, written directly."""
    with engine.begin() as tx:
        tx.execute(
            text(
                "INSERT INTO close_checklists (entity_id, period, checklist_id, checklist_version, calendar_code, "
                "escalate_after_days, generated_by) VALUES ('US01', '2026-09', 'matrix', 1, 'US_FED', 2, 'matrix')"
            )
        )
        tx.execute(
            text(
                "INSERT INTO close_tasks (entity_id, period, task_id, position, name, due_day, due_on, check_name, "
                "status) VALUES ('US01', '2026-09', 'matrix_check', 1, 'Matrix', 1, '2026-10-01', "
                "'journals_settled', 'open')"
            )
        )
    return {"task_id": "matrix_check"}


@pytest.fixture
def world(client: TestClient, engine: Engine) -> dict[str, str]:
    set_up(engine)  # the Stage 7 configuration, active, and the journal people (journalkit)
    for scope in ("US01", "GLOBAL"):
        assign(engine, SETUP_AUTHOR, "config_author", scope)
    assign(engine, SETUP_AUTHOR, "match_preparer", "US01")
    for resource in GLOBAL_RESOURCES:  # in dependency order: a definition needs its event type and mapping
        key = {"Idempotency-Key": f"setup-{uuid.uuid4()}"}
        r = client.post(f"/api/v1/{resource}", json=_body(f"body:{resource}"), headers=_headers(SETUP_AUTHOR, **key))
        assert r.status_code == 201, r.text
    for resource, spec in ((MS, _body("ms")), ("/api/v1/recurring-journals", _body("rj"))):
        key = {"Idempotency-Key": f"setup-{uuid.uuid4()}"}
        created = client.post(resource, json=spec, headers=_headers(SETUP_AUTHOR, **key))
        assert created.status_code == 201, created.text
    key = {"Idempotency-Key": f"setup-{uuid.uuid4()}"}
    run = client.post("/api/v1/smoke/runs", json={"rows": 10}, headers=_headers(SETUP_AUTHOR, **key))
    assert run.status_code == 202
    return {"run_id": run.json()["run_id"], **_journal_world(client, engine)}


def _call(client: TestClient, engine: Engine, world: dict[str, str], user: str, entry: tuple[Any, ...]) -> Any:
    method, path, _, body = entry
    headers = _headers(user, **{"Idempotency-Key": f"m-{uuid.uuid4()}"})
    resource = path.split("/")[3]
    kind, config_id = GLOBAL_RESOURCES.get(resource) or ENTITY_RESOURCES.get(resource, ("match_set", MS_ID))
    values = {**world, "config_id": config_id, "version": "1"}
    if "{assignment_id}" in path:
        values["assignment_id"] = _fresh_assignment(engine)
    if "{version}" in path and method != "GET":
        headers["If-Match"] = _etag(engine, kind, config_id)
    if path.startswith(R):
        values["run_id"] = world["posting_run_id"]
    if path.startswith(JR):
        values["run_id"] = world["journaling_run_id"]
    url = path.format(**values)
    if path.startswith(("/api/v1/gl/", "/api/v1/erp/")) or path in QUERY:
        url += "?" + QUERY.get(path, "entity_id=US01")
    if body == "csv":
        headers["Content-Type"] = "text/csv"
        response = client.post(url + "?entity_id=US01", content=CSV.encode(), headers=headers)
    elif body == "erp-csv":  # Stage 8a: a valid extract, so an allowed caller gets a 201
        headers["Content-Type"] = "text/csv"
        response = client.post(url, content=b"account,balance\n1110,1.00\n4010,-1.00\n", headers=headers)
    elif body == "member":
        response = client.post(url, json={"journal_id": world["journal_id"]}, headers=headers)
    elif body == "write-back":  # the draft journal: past authorization it is refused as not posted (409)
        entries = [{"journal_id": world["journal_id"], "document_id": "ERP-MATRIX"}]
        response = client.post(url, json={"entries": entries}, headers=headers)
    else:
        response = client.request(method, url, json=_body(body), headers=headers)
    if method == "GET" and path in (MS, "/api/v1/recurring-journals"):  # filtered: allowed means it is listed
        listed = [i["config_id"] for i in response.json()["items"]]
        return 200 if config_id in listed else 403
    if method == "GET" and path in LISTED:
        if response.status_code != 200:
            return response.status_code
        return 200 if values[LISTED[path]] in [i[LISTED[path]] for i in response.json()["items"]] else 403
    return response.status_code


def test_the_matrix_covers_every_operation() -> None:
    covered = {(m, p) for m, p, _, _ in MATRIX}
    served = {(m, p) for m, p, op in operations() if op.get("x-close-access") != "public"}
    assert covered == served


def test_authorization_matrix(client: TestClient, engine: Engine, world: dict[str, str]) -> None:
    unexpected: list[str] = []
    checks = 0
    for role in ROLES:
        for scope in SCOPES:
            user = f"{role}@{scope}"
            assign(engine, user, role, scope)
            for entry in MATRIX:
                status = _call(client, engine, world, user, entry)
                checks += 1
                expect_allow = entry[2](role, scope)
                assert status < 500, (user, entry[:2], status)
                allowed = status not in {401, 403, 404}
                if allowed != expect_allow:
                    kind = "unexpected allow" if allowed else "unexpected deny"
                    unexpected.append(f"{kind}: {user} {entry[0]} {entry[1]} -> {status}")
    assert checks == len(ROLES) * len(SCOPES) * len(MATRIX)
    assert unexpected == [], "\n".join(unexpected)
