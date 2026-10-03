"""The journal workflow's configuration in the maker-checker API (Stage 7a, ADR-0016).

- **Kinds.** The entity set, journal templates and the approval matrix are global configurations. A recurring
  journal belongs to its entity, like a match set: authoring, approving and activating it needs the role there.
- **One at a time.** At most one entity set and one approval matrix are active.
- **On save.** A template's listed accounts must be in the latest chart. A recurring journal's lines are checked
  against the latest versions of its template, the chart, the dimension set and its entity's currency.
- **On activation:**
  - a template needs an active chart holding the accounts it lists;
  - the approval matrix needs an active entity set, and thresholds for every entity's functional currency;
  - a recurring journal needs its template, the chart and its entity active, valid lines, a preparer holding
    ``journal_preparer`` in the entity, and, to auto-approve, a matrix that lets recurring journals bypass approval;
  - a new chart version must keep every account an active template lists or a recurring journal uses;
  - a new entity set must keep every entity that has periods or journals, or an active recurring journal.
- **On retirement.** A template can't be retired while an active recurring journal uses it. The entity set and
  the approval matrix are replaced by new versions, never retired while journals can use them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Final

from pydantic import BaseModel, ValidationError
from sqlalchemy import Connection, RowMapping, text

from close_app.errors import Problem
from close_contracts.config.envelope import ConfigKind
from close_contracts.config.journal_workflow import ApprovalMatrix, EntitySet, JournalTemplate, RecurringJournal
from close_contracts.config.journaling import ChartOfAccounts, DimensionSet
from close_contracts.dsl import Issue
from close_contracts.journals import from_recurring, journal_issues, template_issues

KINDS: Final[dict[ConfigKind, tuple[type[BaseModel], str]]] = {
    ConfigKind.ENTITY_SET: (EntitySet, "entity_set_id"),
    ConfigKind.JOURNAL_TEMPLATE: (JournalTemplate, "template_id"),
    ConfigKind.APPROVAL_MATRIX: (ApprovalMatrix, "matrix_id"),
    ConfigKind.RECURRING_JOURNAL: (RecurringJournal, "recurring_id"),
}
SINGLETONS: Final = frozenset({ConfigKind.ENTITY_SET, ConfigKind.APPROVAL_MATRIX})
LIVE: Final = ("draft", "submitted", "approved", "active")
_BY_STATUS: Final = text(
    "SELECT DISTINCT ON (config_id) * FROM config_versions WHERE kind = :k AND status = ANY (:statuses) "
    "ORDER BY config_id, version DESC"
)


def _problem_list(issues: list[Issue]) -> list[dict[str, str]]:
    return [{"code": i.code, "path": i.path, "message": i.message} for i in issues]


def _invalid(issues: list[Issue] | list[dict[str, str]]) -> Problem:
    listed = [(_problem_list([i])[0] if isinstance(i, Issue) else i) for i in issues]
    return Problem(422, "The configuration is invalid", kind="validation-failed", issues=listed)


def parse(kind: ConfigKind, spec: object) -> tuple[BaseModel, str, tuple[str, ...] | None]:
    """The parsed model, its configuration id, and its entities (None for a global configuration)."""
    model_type, id_field = KINDS[kind]
    try:
        model = model_type.model_validate(spec)
    except ValidationError as exc:
        issues = [
            {"code": e["type"], "path": "/" + "/".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()
        ]
        raise _invalid(issues) from None
    entities = (model.entity_id,) if isinstance(model, RecurringJournal) else None
    return model, str(getattr(model, id_field)), entities


@dataclass(frozen=True)
class Context:
    """The latest version of each configuration in some statuses (the active ones, or every live one)."""

    entities: EntitySet | None
    templates: Mapping[str, tuple[JournalTemplate, int]]  # id -> (template, version)
    matrix: tuple[ApprovalMatrix, int] | None
    recurring: Mapping[str, RecurringJournal]
    chart: ChartOfAccounts | None
    dimensions: DimensionSet | None


def _rows(tx: Connection, kind: ConfigKind, statuses: tuple[str, ...]) -> list[RowMapping]:
    return list(tx.execute(_BY_STATUS, {"k": kind.value, "statuses": list(statuses)}).mappings())


def context(tx: Connection, statuses: tuple[str, ...]) -> Context:
    entity_sets = [EntitySet.model_validate(r["spec"]) for r in _rows(tx, ConfigKind.ENTITY_SET, statuses)]
    matrices = [
        (ApprovalMatrix.model_validate(r["spec"]), int(r["version"]))
        for r in _rows(tx, ConfigKind.APPROVAL_MATRIX, statuses)
    ]
    charts = [ChartOfAccounts.model_validate(r["spec"]) for r in _rows(tx, ConfigKind.CHART_OF_ACCOUNTS, statuses)]
    dims = [DimensionSet.model_validate(r["spec"]) for r in _rows(tx, ConfigKind.DIMENSION_SET, statuses)]
    return Context(
        entities=entity_sets[0] if entity_sets else None,
        templates={
            r["config_id"]: (JournalTemplate.model_validate(r["spec"]), int(r["version"]))
            for r in _rows(tx, ConfigKind.JOURNAL_TEMPLATE, statuses)
        },
        matrix=matrices[0] if matrices else None,
        recurring={
            r["config_id"]: RecurringJournal.model_validate(r["spec"])
            for r in _rows(tx, ConfigKind.RECURRING_JOURNAL, statuses)
        },
        chart=charts[0] if charts else None,
        dimensions=dims[0] if dims else None,
    )


def _recurring_issues(model: RecurringJournal, ctx: Context) -> list[Issue]:
    issues: list[Issue] = []
    entity = ctx.entities.entity(model.entity_id) if ctx.entities else None
    template = ctx.templates.get(model.template_id)
    if entity is None:
        issues.append(Issue("unknown_entity", "/entity_id", f"entity {model.entity_id} is not in the entity set"))
    if template is None:
        issues.append(Issue("unknown_template", "/template_id", f"template {model.template_id} does not exist"))
    if ctx.chart is None:
        issues.append(Issue("no_chart", "/", "no chart of accounts exists"))
    if entity is not None and template is not None and ctx.chart is not None:
        draft = from_recurring(model, date.fromisoformat(f"{model.first_period}-{model.day:02d}"))
        issues += journal_issues(
            draft,
            template=template[0],
            chart=ctx.chart,
            dimensions=ctx.dimensions,
            currency=entity.functional_currency,
        )
    return issues


def save_checks(tx: Connection, model: BaseModel) -> None:
    """A new version's references must exist in some live version."""
    if not isinstance(model, JournalTemplate | RecurringJournal):
        return
    ctx = context(tx, LIVE)
    if isinstance(model, JournalTemplate):
        problems = template_issues(model, ctx.chart) if ctx.chart is not None else []
    else:
        problems = _recurring_issues(model, ctx)
    if problems:
        raise _invalid(problems)


def _holds_preparer(tx: Connection, user_id: str, entity_id: str) -> bool:
    found = tx.execute(
        text(
            "SELECT 1 FROM role_assignments WHERE user_id = :u AND role = 'journal_preparer' AND scope = :e "
            "AND revoked_at IS NULL"
        ),
        {"u": user_id, "e": entity_id},
    ).first()
    return found is not None


_ENTITY_USERS: Final = text(
    "SELECT DISTINCT entity_id, 'accounting_periods' AS used_by FROM accounting_periods "
    "UNION SELECT DISTINCT entity_id, 'journals' FROM journals"
)


def _entities_in_use(tx: Connection, active: Context) -> dict[str, list[str]]:
    users: dict[str, list[str]] = {}
    for entity_id, used_by in tx.execute(_ENTITY_USERS):
        users.setdefault(str(entity_id), []).append(str(used_by))
    for recurring_id, r in active.recurring.items():
        users.setdefault(r.entity_id, []).append(recurring_id)
    return users


def _accounts_in_use(active: Context) -> dict[str, list[str]]:
    users: dict[str, list[str]] = {}
    for template_id, (template, _) in active.templates.items():
        for code in template.allowed_accounts.codes:
            users.setdefault(code, []).append(template_id)
    for recurring_id, r in active.recurring.items():
        for line in r.lines:
            users.setdefault(line.account, []).append(recurring_id)
    return users


def activation_checks(tx: Connection, row: RowMapping) -> None:
    """The version's references must be active, and a new version must keep what active configurations use."""
    kind = ConfigKind(row["kind"])
    if kind not in KINDS and kind != ConfigKind.CHART_OF_ACCOUNTS:
        return
    active = context(tx, ("active",))
    problems: list[Issue] = []
    if kind == ConfigKind.CHART_OF_ACCOUNTS:
        chart = ChartOfAccounts.model_validate(row["spec"])
        for code, users in sorted(_accounts_in_use(active).items()):
            account = chart.account(code)
            if account is None or account.status != "active" or not account.postable:
                problems.append(
                    Issue("account_in_use", "/accounts", f"account {code} is used by {', '.join(sorted(users))}")
                )
    else:
        model, _, _ = parse(kind, row["spec"])
        if kind in SINGLETONS:
            others = [r["config_id"] for r in _rows(tx, kind, ("active",)) if r["config_id"] != row["config_id"]]
            if others:
                detail = f"{others[0]} is active: add a version to it instead"
                raise Problem(409, "Another one is active", detail, kind="conflict")
        problems += _activation_problems(tx, model, active)
    if problems:
        title = "The configuration's references are not ready"
        raise Problem(409, title, kind="conflict", issues=_problem_list(problems))


def _activation_problems(tx: Connection, model: BaseModel, active: Context) -> list[Issue]:
    problems: list[Issue] = []
    if isinstance(model, JournalTemplate):
        if active.chart is None:
            problems.append(Issue("no_active_chart", "/", "no chart of accounts is active"))
        else:
            problems += template_issues(model, active.chart)
    elif isinstance(model, ApprovalMatrix):
        if active.entities is None:
            problems.append(Issue("no_active_entities", "/", "no entity set is active"))
        else:
            currencies = {e.functional_currency for e in active.entities.entities}
            problems += [
                Issue("missing_thresholds", "/thresholds", f"no thresholds for {c}")
                for c in sorted(currencies)
                if model.thresholds_for(c) is None
            ]
    elif isinstance(model, EntitySet):
        kept = {e.entity_id for e in model.entities}
        for entity_id, users in sorted(_entities_in_use(tx, active).items()):
            if entity_id not in kept:
                message = f"entity {entity_id} is used by {', '.join(sorted(set(users)))}"
                problems.append(Issue("entity_in_use", "/entities", message))
    elif isinstance(model, RecurringJournal):
        problems += _recurring_issues(model, active)
        if not _holds_preparer(tx, model.preparer, model.entity_id):
            problems.append(
                Issue("preparer_lacks_role", "/preparer", f"{model.preparer} is not a preparer in {model.entity_id}")
            )
        if model.auto_approve and (active.matrix is None or not active.matrix[0].bypass.recurring):
            message = "the approval matrix doesn't let recurring journals skip approval"
            problems.append(Issue("bypass_not_allowed", "/auto_approve", message))
    return problems


def _users(tx: Connection, model: BaseModel, active: Context) -> list[str]:
    if isinstance(model, JournalTemplate):
        return sorted(rid for rid, r in active.recurring.items() if r.template_id == model.template_id)
    if isinstance(model, ApprovalMatrix):
        return sorted(active.templates)  # no template's journal could be routed
    if isinstance(model, EntitySet):
        return sorted({u for users in _entities_in_use(tx, active).values() for u in users})
    if isinstance(model, ChartOfAccounts):
        return sorted({u for users in _accounts_in_use(active).values() for u in users} | set(active.templates))
    return []


def retirement_checks(tx: Connection, row: RowMapping) -> None:
    """Nothing active may still use a version being retired."""
    kind = ConfigKind(row["kind"])
    if kind == ConfigKind.CHART_OF_ACCOUNTS:
        model: BaseModel = ChartOfAccounts.model_validate(row["spec"])
    elif kind in KINDS:
        model = parse(kind, row["spec"])[0]
    else:
        return
    users = _users(tx, model, context(tx, ("active",)))
    if users:
        detail = f"active configurations use it: {', '.join(users)}; retire them first, or add a version to it"
        raise Problem(409, "The configuration is in use", detail, kind="conflict")
