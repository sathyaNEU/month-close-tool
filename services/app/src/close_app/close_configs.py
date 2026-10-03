"""The period close's configuration in the maker-checker API (Stage 8b, ADR-0020).

- **Kinds.** The close checklist and the flux rules are global configurations, one of each active at a time.
- **On save.** A checklist's entities must be in the latest entity set, and the flux rules' overrides must name
  accounts in the latest chart.
- **On activation:**
  - both need an active entity set, holding every entity a task is limited to;
  - every preparer must hold ``close.prepare``, and every reviewer ``close.review``, in each entity their task
    applies to;
  - the flux rules need an amount for every entity's functional currency in every threshold, and overrides on
    accounts of the active chart.
- **On retirement.** Nothing blocks it: a period's checklist is a frozen copy, and flux needs rules only while a
  close is in progress.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ValidationError
from sqlalchemy import Connection, RowMapping, text

from close_app.authz import POLICY, Action, Role
from close_app.errors import Problem
from close_contracts.config.close import CloseChecklist, FluxRules
from close_contracts.config.envelope import ConfigKind
from close_contracts.config.journal_workflow import EntitySet
from close_contracts.config.journaling import ChartOfAccounts
from close_contracts.dsl import Issue

KINDS: Final[dict[ConfigKind, tuple[type[BaseModel], str]]] = {
    ConfigKind.CLOSE_CHECKLIST: (CloseChecklist, "checklist_id"),
    ConfigKind.FLUX_RULES: (FluxRules, "rules_id"),
}
SINGLETONS: Final = frozenset(KINDS)
LIVE: Final = ("draft", "submitted", "approved", "active")
_LATEST: Final = text(
    "SELECT spec FROM config_versions WHERE kind = :k AND status = ANY (:statuses) ORDER BY version DESC LIMIT 1"
)
_OTHERS_ACTIVE: Final = text(
    "SELECT config_id FROM config_versions WHERE kind = :k AND status = 'active' AND config_id <> :c"
)
_ROLES: Final = text("SELECT role FROM role_assignments WHERE user_id = :u AND scope = :e AND revoked_at IS NULL")


def _invalid(issues: list[dict[str, str]]) -> Problem:
    return Problem(422, "The configuration is invalid", kind="validation-failed", issues=issues)


def _listed(issues: list[Issue]) -> list[dict[str, str]]:
    return [{"code": i.code, "path": i.path, "message": i.message} for i in issues]


def parse(kind: ConfigKind, spec: object) -> tuple[BaseModel, str]:
    model_type, id_field = KINDS[kind]
    try:
        model = model_type.model_validate(spec)
    except ValidationError as exc:
        issues = [
            {"code": e["type"], "path": "/" + "/".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()
        ]
        raise _invalid(issues) from None
    return model, str(getattr(model, id_field))


def _latest(tx: Connection, kind: ConfigKind, statuses: tuple[str, ...]) -> dict[str, object] | None:
    found = tx.execute(_LATEST, {"k": kind.value, "statuses": list(statuses)}).scalar()
    return dict(found) if found is not None else None


def _entities(tx: Connection, statuses: tuple[str, ...]) -> EntitySet | None:
    spec = _latest(tx, ConfigKind.ENTITY_SET, statuses)
    return EntitySet.model_validate(spec) if spec is not None else None


def _chart(tx: Connection, statuses: tuple[str, ...]) -> ChartOfAccounts | None:
    spec = _latest(tx, ConfigKind.CHART_OF_ACCOUNTS, statuses)
    return ChartOfAccounts.model_validate(spec) if spec is not None else None


def holds(tx: Connection, user_id: str, action: Action, entity_id: str) -> bool:
    """Whether ``user_id`` holds ``action`` in ``entity_id`` through a role assigned there."""
    roles = tx.execute(_ROLES, {"u": user_id, "e": entity_id}).scalars()
    return any(action in POLICY[Role(r)] for r in roles)


def _checklist_issues(tx: Connection, model: CloseChecklist, entities: EntitySet | None, *, roles: bool) -> list[Issue]:
    if entities is None:
        return [Issue("no_entities", "/", "no entity set exists")]
    known = {e.entity_id for e in entities.entities}
    issues: list[Issue] = []
    for n, task in enumerate(model.tasks):
        where = f"/tasks/{n}"
        unknown = sorted(set(task.entities) - known)
        if unknown:
            issues.append(Issue("unknown_entity", f"{where}/entities", f"not in the entity set: {', '.join(unknown)}"))
        if not roles:
            continue
        for entity_id in sorted(set(task.entities) & known if task.entities else known):
            if task.preparer is not None and not holds(tx, task.preparer, Action.CLOSE_PREPARE, entity_id):
                message = f"{task.preparer} can't prepare close tasks in {entity_id}"
                issues.append(Issue("preparer_lacks_role", f"{where}/preparer", message))
            if task.reviewer is not None and not holds(tx, task.reviewer, Action.CLOSE_REVIEW, entity_id):
                message = f"{task.reviewer} can't review close tasks in {entity_id}"
                issues.append(Issue("reviewer_lacks_role", f"{where}/reviewer", message))
    return issues


def _flux_issues(model: FluxRules, entities: EntitySet | None, chart: ChartOfAccounts | None) -> list[Issue]:
    issues: list[Issue] = []
    if entities is None:
        issues.append(Issue("no_entities", "/", "no entity set exists"))
    else:
        currencies = sorted({e.functional_currency for e in entities.entities})
        thresholds = [(f"/by_type/{t}", v) for t, v in model.by_type.items()]
        thresholds += [(f"/accounts/{a}", v) for a, v in model.accounts.items()]
        for path, threshold in thresholds:
            missing = [c for c in currencies if c not in threshold.amounts]
            if missing:
                issues.append(Issue("missing_amounts", f"{path}/amounts", f"no amount for {', '.join(missing)}"))
    if chart is not None:
        issues += [
            Issue("unknown_account", f"/accounts/{a}", f"account {a} is not in the chart")
            for a in sorted(model.accounts)
            if chart.account(a) is None
        ]
    return issues


def save_checks(tx: Connection, model: BaseModel) -> None:
    """A new version's references must exist in some live version."""
    if isinstance(model, CloseChecklist):
        problems = _checklist_issues(tx, model, _entities(tx, LIVE), roles=False)
    elif isinstance(model, FluxRules):
        problems = [i for i in _flux_issues(model, None, _chart(tx, LIVE)) if i.code != "no_entities"]
    else:
        return
    if problems:
        raise _invalid(_listed(problems))


def activation_checks(tx: Connection, row: RowMapping) -> None:
    kind = ConfigKind(row["kind"])
    if kind not in KINDS:
        return
    others = list(tx.execute(_OTHERS_ACTIVE, {"k": kind.value, "c": row["config_id"]}).scalars())
    if others:
        raise Problem(
            409, "Another one is active", f"{others[0]} is active: add a version to it instead", kind="conflict"
        )
    model, _ = parse(kind, row["spec"])
    entities = _entities(tx, ("active",))
    if isinstance(model, CloseChecklist):
        problems = _checklist_issues(tx, model, entities, roles=True)
    elif isinstance(model, FluxRules):
        problems = _flux_issues(model, entities, _chart(tx, ("active",)))
    else:
        raise TypeError(f"not a close configuration: {type(model).__name__}")
    if problems:
        raise Problem(409, "The configuration's references are not ready", kind="conflict", issues=_listed(problems))


def active_checklist(tx: Connection) -> tuple[CloseChecklist, str, int] | None:
    """The active close checklist, its id and version."""
    row = tx.execute(
        text(
            "SELECT config_id, version, spec FROM config_versions WHERE kind = 'close_checklist' AND status = 'active'"
        )
    ).first()
    if row is None:
        return None
    return CloseChecklist.model_validate(row.spec), str(row.config_id), int(row.version)


def active_flux_rules(tx: Connection) -> tuple[FluxRules, str, int] | None:
    """The active flux rules, their id and version."""
    row = tx.execute(
        text("SELECT config_id, version, spec FROM config_versions WHERE kind = 'flux_rules' AND status = 'active'")
    ).first()
    if row is None:
        return None
    return FluxRules.model_validate(row.spec), str(row.config_id), int(row.version)
