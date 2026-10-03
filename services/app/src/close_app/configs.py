"""Maker-checker configuration versions (roadmap engineering rule 2): data sources, match sets and, from Stage 6,
the journaling configurations (``journal_configs``).

- **Life cycle.** A version moves ``draft -> submitted -> approved -> active -> retired``; ``reject`` returns a
  submitted version to draft. Only a draft can be edited.
- **Authors.** A version's authors are its creator and every editor. None of them may approve or activate
  it (separation of duties), which the table's constraints also enforce.
- **One version in progress.** A configuration has at most one version in progress (draft, submitted or
  approved) and at most one active version. Activating a version retires the active one, whose
  ``effective_to`` becomes the new version's ``effective_from``.
- **References between configurations.** A match set can be submitted only when every data source it names
  has a version, and activated only when each has an active one. A version that an active configuration uses
  cannot be retired (a new version can replace it).
- **Audit.** Every change appends one audit event in the same transaction, as the last lock it takes.

Validation uses ``close_contracts`` only (the app does not ship the engine): the pydantic models, ``bind()`` for a
data source, and :func:`close_contracts.config.validate.match_set_issues` for a match set.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final

from pydantic import BaseModel, ValidationError
from sqlalchemy import Connection, RowMapping, text

from close_app import audit, close_configs, journal_configs, workflow_configs
from close_app.errors import Problem
from close_contracts.binding import bind
from close_contracts.config.data_source import DataSource
from close_contracts.config.envelope import ConfigKind, ConfigStatus, spec_hash
from close_contracts.config.match_set import MatchSet
from close_contracts.config.validate import match_set_issues
from close_contracts.dsl import DslTypeError

RESOURCES: Final = {
    "data-sources": ConfigKind.DATA_SOURCE,
    "match-sets": ConfigKind.MATCH_SET,
    "event-types": ConfigKind.EVENT_TYPE,
    "auto-journal-definitions": ConfigKind.AUTO_JOURNAL_DEFINITION,
    "account-mappings": ConfigKind.ACCOUNT_MAPPING,
    "accounts": ConfigKind.CHART_OF_ACCOUNTS,
    "dimensions": ConfigKind.DIMENSION_SET,
    "programs": ConfigKind.ACCOUNTING_POLICY,
    "entities": ConfigKind.ENTITY_SET,
    "journal-templates": ConfigKind.JOURNAL_TEMPLATE,
    "approval-matrices": ConfigKind.APPROVAL_MATRIX,
    "recurring-journals": ConfigKind.RECURRING_JOURNAL,
    "close-checklists": ConfigKind.CLOSE_CHECKLIST,  # Stage 8b
    "flux-rules": ConfigKind.FLUX_RULES,
}
# Configurations that belong to entities; every other kind is global.
ENTITY_SCOPED: Final = frozenset({ConfigKind.MATCH_SET, ConfigKind.RECURRING_JOURNAL})
IN_PROGRESS: Final = frozenset({ConfigStatus.DRAFT, ConfigStatus.SUBMITTED, ConfigStatus.APPROVED})
FIELDS: Final = (
    "kind", "config_id", "version", "status", "effective_from", "effective_to", "content_hash", "entity_ids",
    "authors", "created_by", "created_at", "submitted_by", "submitted_at", "approved_by", "approved_at",
    "activated_by", "activated_at", "retired_by", "retired_at", "row_version",
)  # fmt: skip
_ONE: Final = text("SELECT * FROM config_versions WHERE kind = :k AND config_id = :c AND version = :v")
_ONE_LOCKED: Final = text(
    "SELECT * FROM config_versions WHERE kind = :k AND config_id = :c AND version = :v FOR UPDATE"
)
_ALL: Final = text("SELECT * FROM config_versions WHERE kind = :k AND config_id = :c ORDER BY version")
_LATEST: Final = text(
    "SELECT DISTINCT ON (config_id) * FROM config_versions WHERE kind = :k ORDER BY config_id, version DESC"
)
_ACTIVE_LOCKED: Final = text(
    "SELECT * FROM config_versions WHERE kind = :k AND config_id = :c AND status = 'active' FOR UPDATE"
)
_INSERT: Final = text(
    "INSERT INTO config_versions (kind, config_id, version, status, effective_from, spec, content_hash, "
    "entity_ids, authors, created_by) VALUES (:k, :c, :v, 'draft', :ef, CAST(:spec AS jsonb), :h, :e, :a, :by)"
)
_EDIT: Final = text(
    "UPDATE config_versions SET spec = CAST(:spec AS jsonb), content_hash = :h, effective_from = :ef, "
    "entity_ids = :e, authors = CASE WHEN :by = ANY (authors) THEN authors ELSE authors || :by END, "
    "row_version = row_version + 1 WHERE kind = :k AND config_id = :c AND version = :v"
)
_STEPS: Final = {
    "submit": text(
        "UPDATE config_versions SET status = 'submitted', submitted_by = :by, submitted_at = now(), "
        "row_version = row_version + 1 WHERE kind = :k AND config_id = :c AND version = :v"
    ),
    "approve": text(
        "UPDATE config_versions SET status = 'approved', approved_by = :by, approved_at = now(), "
        "row_version = row_version + 1 WHERE kind = :k AND config_id = :c AND version = :v"
    ),
    "reject": text(
        "UPDATE config_versions SET status = 'draft', submitted_by = NULL, submitted_at = NULL, "
        "row_version = row_version + 1 WHERE kind = :k AND config_id = :c AND version = :v"
    ),
    "activate": text(
        "UPDATE config_versions SET status = 'active', activated_by = :by, activated_at = now(), "
        "row_version = row_version + 1 WHERE kind = :k AND config_id = :c AND version = :v"
    ),
    "retire": text(
        "UPDATE config_versions SET status = 'retired', effective_to = :eto, retired_by = :by, retired_at = now(), "
        "row_version = row_version + 1 WHERE kind = :k AND config_id = :c AND version = :v"
    ),
}
FROM_STATUS: Final = {
    "submit": ConfigStatus.DRAFT,
    "approve": ConfigStatus.SUBMITTED,
    "reject": ConfigStatus.SUBMITTED,
    "activate": ConfigStatus.APPROVED,
    "retire": ConfigStatus.ACTIVE,
}


@dataclass(frozen=True)
class Parsed:
    model: BaseModel
    config_id: str
    entities: tuple[str, ...] | None  # None for a global configuration (a data source)


def _invalid(issues: list[dict[str, Any]]) -> Problem:
    return Problem(422, "The configuration is invalid", kind="validation-failed", issues=issues)


def parse(kind: ConfigKind, spec: Any) -> Parsed:
    if kind in journal_configs.KINDS:
        model, config_id = journal_configs.parse(kind, spec)
        return Parsed(model, config_id, None)
    if kind in close_configs.KINDS:
        model, config_id = close_configs.parse(kind, spec)
        return Parsed(model, config_id, None)
    if kind in workflow_configs.KINDS:
        model, config_id, scoped = workflow_configs.parse(kind, spec)
        return Parsed(model, config_id, scoped)
    try:
        if kind == ConfigKind.DATA_SOURCE:
            source = DataSource.model_validate(spec)
            bind(source)
            return Parsed(source, source.source_id, None)
        match_set = MatchSet.model_validate(spec)
    except ValidationError as exc:
        issues = [
            {"code": e["type"], "path": "/" + "/".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()
        ]
        raise _invalid(issues) from None
    except DslTypeError as exc:
        raise _invalid([{"code": i.code, "path": i.path, "message": i.message} for i in exc.issues]) from None
    problems = match_set_issues(match_set)
    if problems:
        raise _invalid([{"code": i.code, "path": i.path, "message": i.message} for i in problems])
    return Parsed(match_set, match_set.match_set_id, tuple(sorted(match_set.entity_ids)))


def etag(row: RowMapping) -> str:
    return f'"{row["row_version"]}-{row["content_hash"][:16]}"'


def target(row: RowMapping) -> str:
    return f"{row['kind']}:{row['config_id']}:v{row['version']}"


def entities(row: RowMapping) -> tuple[str, ...] | None:
    """An entity-scoped configuration's entities; None for every global configuration."""
    return tuple(row["entity_ids"]) if row["kind"] in ENTITY_SCOPED else None


def as_json(row: RowMapping, *, with_spec: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in FIELDS:
        value = row[name]
        out[name] = value.isoformat() if isinstance(value, date | datetime) else value
    out["entity_ids"], out["authors"], out["etag"] = list(row["entity_ids"]), list(row["authors"]), etag(row)
    if with_spec:
        out["spec"] = row["spec"]
    return out


def _params(row: RowMapping) -> dict[str, Any]:
    return {"k": row["kind"], "c": row["config_id"], "v": row["version"]}


def load(tx: Connection, kind: ConfigKind, config_id: str, version: int, *, lock: bool = False) -> RowMapping | None:
    return tx.execute(_ONE_LOCKED if lock else _ONE, {"k": kind.value, "c": config_id, "v": version}).mappings().first()


def _reload(tx: Connection, row: RowMapping | dict[str, Any]) -> RowMapping:
    new = tx.execute(_ONE, {"k": row["kind"], "c": row["config_id"], "v": row["version"]}).mappings().first()
    if new is None:  # the row was locked or written in this transaction, so it cannot vanish
        raise RuntimeError(f"{row['kind']}:{row['config_id']}:v{row['version']} disappeared")
    return new


def versions(conn: Connection, kind: ConfigKind, config_id: str) -> list[RowMapping]:
    return list(conn.execute(_ALL, {"k": kind.value, "c": config_id}).mappings())


def latest_per_config(conn: Connection, kind: ConfigKind) -> list[RowMapping]:
    return list(conn.execute(_LATEST, {"k": kind.value}).mappings())


def _audit(tx: Connection, actor: str, verb: str, row: RowMapping, request_id: str | None, **payload: Any) -> None:
    event = audit.Event(
        actor,
        f"{row['kind']}.{verb}",
        entity_ids=tuple(row["entity_ids"]),
        target=target(row),
        payload={"version": row["version"], "status": row["status"], "content_hash": row["content_hash"], **payload},
    )
    audit.append(tx, event, request_id)


def create(
    tx: Connection, kind: ConfigKind, parsed: Parsed, effective_from: date, *, actor: str, request_id: str | None
) -> RowMapping:
    """A new draft: version 1 of a new configuration, or the next version of an existing one."""
    lock = f"config:{kind.value}:{parsed.config_id}"
    tx.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": lock})
    current = versions(tx, kind, parsed.config_id)
    busy = [r for r in current if r["status"] in IN_PROGRESS]
    if busy:
        detail = f"version {busy[0]['version']} is {busy[0]['status']}"
        raise Problem(409, "A version is already in progress", detail, kind="conflict")
    version = (current[-1]["version"] + 1) if current else 1
    journal_configs.save_checks(tx, parsed.model)
    workflow_configs.save_checks(tx, parsed.model)
    close_configs.save_checks(tx, parsed.model)
    spec = parsed.model.model_dump(mode="json")
    tx.execute(
        _INSERT,
        {
            "k": kind.value,
            "c": parsed.config_id,
            "v": version,
            "ef": effective_from,
            "spec": json.dumps(spec),
            "h": spec_hash(parsed.model),
            "e": list(parsed.entities or ()),
            "a": [actor],
            "by": actor,
        },
    )
    row = _reload(tx, {"kind": kind.value, "config_id": parsed.config_id, "version": version})
    _audit(tx, actor, "create", row, request_id, effective_from=effective_from.isoformat(), spec=spec)
    return row


def edit(
    tx: Connection, row: RowMapping, parsed: Parsed, effective_from: date, *, actor: str, request_id: str | None
) -> RowMapping:
    if row["status"] != ConfigStatus.DRAFT:
        detail = f"version {row['version']} is {row['status']}"
        raise Problem(409, "Only a draft can be edited", detail, kind="conflict")
    if parsed.config_id != row["config_id"]:
        raise _invalid([{"code": "id_changed", "path": "/spec", "message": "the configuration id cannot change"}])
    journal_configs.save_checks(tx, parsed.model)
    workflow_configs.save_checks(tx, parsed.model)
    close_configs.save_checks(tx, parsed.model)
    spec = parsed.model.model_dump(mode="json")
    tx.execute(
        _EDIT,
        {
            "spec": json.dumps(spec),
            "h": spec_hash(parsed.model),
            "ef": effective_from,
            "e": list(parsed.entities or ()),
            "by": actor,
            **_params(row),
        },
    )
    new = _reload(tx, row)
    _audit(tx, actor, "edit", new, request_id, effective_from=effective_from.isoformat(), spec=spec)
    return new


def _source_statuses(tx: Connection, match_set: RowMapping) -> dict[str, set[str]]:
    source_ids = sorted({b["source_id"] for b in match_set["spec"]["sources"]})
    rows = tx.execute(
        text("SELECT config_id, status FROM config_versions WHERE kind = 'data_source' AND config_id = ANY (:ids)"),
        {"ids": source_ids},
    ).all()
    out: dict[str, set[str]] = {s: set() for s in source_ids}
    for r in rows:
        out[str(r.config_id)].add(str(r.status))
    return out


def _check_references(tx: Connection, row: RowMapping, verb: str) -> None:
    if row["kind"] != ConfigKind.MATCH_SET or verb not in {"submit", "activate"}:
        return
    statuses = _source_statuses(tx, row)
    if verb == "submit":
        missing, reason = sorted(s for s, found in statuses.items() if not found), "have no version"
    else:
        missing = sorted(s for s, found in statuses.items() if ConfigStatus.ACTIVE not in found)
        reason = "have no active version"
    if missing:
        raise Problem(409, "Referenced data sources are not ready", f"{', '.join(missing)} {reason}", kind="conflict")


_MATCH_SETS_USING = text(
    "SELECT DISTINCT config_id FROM config_versions WHERE kind = 'match_set' AND status = 'active' "
    "AND spec -> 'sources' @> jsonb_build_array(jsonb_build_object('source_id', CAST(:id AS text))) "
    "ORDER BY config_id"
)


def _check_users(tx: Connection, row: RowMapping) -> None:
    """Nothing active may still use a version being retired: a data source its match sets, and the journaling
    configurations their users (``journal_configs.retirement_checks``)."""
    if row["kind"] == ConfigKind.DATA_SOURCE:
        users = list(tx.execute(_MATCH_SETS_USING, {"id": row["config_id"]}).scalars())
        if users:
            detail = f"active match sets use it: {', '.join(users)}; retire them first, or add a version to it"
            raise Problem(409, "The configuration is in use", detail, kind="conflict")
    journal_configs.retirement_checks(tx, row)
    workflow_configs.retirement_checks(tx, row)


def _retire_active(tx: Connection, row: RowMapping, actor: str, request_id: str | None) -> None:
    active = tx.execute(_ACTIVE_LOCKED, {"k": row["kind"], "c": row["config_id"]}).mappings().first()
    if active is None:
        return
    if row["effective_from"] <= active["effective_from"]:
        detail = f"version {active['version']} is effective from {active['effective_from']}"
        raise Problem(409, "The new version must take effect after the active one", detail, kind="conflict")
    tx.execute(_STEPS["retire"], {"eto": row["effective_from"], "by": actor, **_params(active)})
    _audit(tx, actor, "retire", _reload(tx, active), request_id, superseded_by=row["version"])


def sod_violation(row: RowMapping, verb: str, actor: str) -> str | None:
    """Why ``actor`` may not ``verb`` this version (separation of duties), or None."""
    if verb in {"approve", "activate"} and actor in row["authors"]:
        return f"you authored version {row['version']}, so another person must {verb} it"
    return None


def transition(
    tx: Connection,
    row: RowMapping,
    verb: str,
    *,
    actor: str,
    request_id: str | None,
    reason: str | None = None,
    effective_to: date | None = None,
) -> RowMapping:
    wanted = FROM_STATUS[verb]
    if row["status"] != wanted:
        detail = f"{verb} needs a {wanted} version"
        raise Problem(409, f"Cannot {verb} a {row['status']} version", detail, kind="conflict")
    _check_references(tx, row, verb)
    if verb == "activate":
        journal_configs.activation_checks(tx, row)
        workflow_configs.activation_checks(tx, row)
        close_configs.activation_checks(tx, row)
        _retire_active(tx, row, actor, request_id)
    if verb == "retire" and (effective_to is None or effective_to <= row["effective_from"]):
        raise _invalid([{"code": "effective_to", "path": "/effective_to", "message": "must be after effective_from"}])
    if verb == "retire":
        _check_users(tx, row)
    tx.execute(_STEPS[verb], {"by": actor, "eto": effective_to, **_params(row)})
    new = _reload(tx, row)
    extra: dict[str, Any] = {"reason": reason} if reason else {}
    if effective_to is not None:
        extra["effective_to"] = effective_to.isoformat()
    _audit(tx, actor, verb, new, request_id, **extra)
    return new
