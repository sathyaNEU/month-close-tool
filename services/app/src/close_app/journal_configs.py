"""Journaling configuration in the maker-checker API (Stage 6, ADR-0015).

- **Kinds.** Event types, automated journal definitions, account mappings, the chart of accounts, dimension sets
  and the accounting policy are global configurations: authoring, approving and activating them needs the role
  at ``GLOBAL``.
- **On save.** Each version is parsed by its contract model. A definition's expressions are typechecked against
  the latest versions of the event types, mappings, chart and dimensions it names.
- **On activation.** The references must be active:
  - a definition's event types and mappings, its accounts in the active chart, and a dimension set and a policy;
  - a mapping's accounts in the active chart.
- **One at a time.** At most one chart, one dimension set and one policy are active. An event type's code is
  unique among active event types.
- **New versions keep active definitions valid.** A chart version can't make an account inactive or unpostable
  while an active definition or mapping uses it. A new version of an event type, a mapping or the dimension set
  can't break an active definition's expressions or references.
- **On retirement.** Nothing active may still use the version: an event type or mapping its definitions, the
  chart its definitions and mappings, the dimension set and the policy any definition.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Final

from pydantic import BaseModel, ValidationError
from sqlalchemy import Connection, RowMapping, text

from close_app.errors import Problem
from close_contracts.config.accounting_policy import AccountingPolicy
from close_contracts.config.envelope import ConfigKind
from close_contracts.config.journal_validate import (
    chart_change_issues,
    definition_accounts,
    definition_issues,
    event_type_issues,
    mapping_issues,
)
from close_contracts.config.journaling import (
    AccountMapping,
    AutoJournalDefinition,
    ChartOfAccounts,
    DimensionSet,
    EventType,
)
from close_contracts.dsl import Issue

POLICY_ID: Final = "accounting_policy"
KINDS: Final[dict[ConfigKind, tuple[type[BaseModel], str | None]]] = {
    ConfigKind.EVENT_TYPE: (EventType, "type_id"),
    ConfigKind.AUTO_JOURNAL_DEFINITION: (AutoJournalDefinition, "definition_id"),
    ConfigKind.ACCOUNT_MAPPING: (AccountMapping, "mapping_id"),
    ConfigKind.CHART_OF_ACCOUNTS: (ChartOfAccounts, "chart_id"),
    ConfigKind.DIMENSION_SET: (DimensionSet, "dimension_set_id"),
    ConfigKind.ACCOUNTING_POLICY: (AccountingPolicy, None),
}
SINGLETONS: Final = frozenset({ConfigKind.CHART_OF_ACCOUNTS, ConfigKind.DIMENSION_SET, ConfigKind.ACCOUNTING_POLICY})
LIVE: Final = ("draft", "submitted", "approved", "active")
_BY_STATUS: Final = text(
    "SELECT DISTINCT ON (config_id) * FROM config_versions WHERE kind = :k AND status = ANY (:statuses) "
    "ORDER BY config_id, version DESC"
)


def _issues(issues: list[Issue]) -> list[dict[str, str]]:
    return [{"code": i.code, "path": i.path, "message": i.message} for i in issues]


def invalid(issues: list[Issue] | list[dict[str, str]], title: str = "The configuration is invalid") -> Problem:
    listed = [(_issues([i])[0] if isinstance(i, Issue) else i) for i in issues]
    return Problem(422, title, kind="validation-failed", issues=listed)


def parse(kind: ConfigKind, spec: Any) -> tuple[BaseModel, str]:
    """The parsed model and its configuration id."""
    model_type, id_field = KINDS[kind]
    try:
        model = model_type.model_validate(spec)
    except ValidationError as exc:
        issues = [
            {"code": e["type"], "path": "/" + "/".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()
        ]
        raise invalid(issues) from None
    if isinstance(model, EventType):
        problems = event_type_issues(model)
        if problems:
            raise invalid(problems)
    return model, (getattr(model, id_field) if id_field else POLICY_ID)


@dataclass(frozen=True)
class Context:
    event_types: Mapping[str, EventType]  # by code
    mappings: Mapping[str, AccountMapping]
    chart: ChartOfAccounts | None
    dimensions: DimensionSet | None
    definitions: Mapping[str, AutoJournalDefinition]
    policy: AccountingPolicy | None


def _rows(tx: Connection, kind: ConfigKind, statuses: tuple[str, ...]) -> list[RowMapping]:
    return list(tx.execute(_BY_STATUS, {"k": kind.value, "statuses": list(statuses)}).mappings())


def context(tx: Connection, statuses: tuple[str, ...]) -> Context:
    """The latest version of every journaling configuration in one of ``statuses``."""
    event_types = [EventType.model_validate(r["spec"]) for r in _rows(tx, ConfigKind.EVENT_TYPE, statuses)]
    mappings = [AccountMapping.model_validate(r["spec"]) for r in _rows(tx, ConfigKind.ACCOUNT_MAPPING, statuses)]
    charts = [ChartOfAccounts.model_validate(r["spec"]) for r in _rows(tx, ConfigKind.CHART_OF_ACCOUNTS, statuses)]
    dims = [DimensionSet.model_validate(r["spec"]) for r in _rows(tx, ConfigKind.DIMENSION_SET, statuses)]
    definitions = [
        AutoJournalDefinition.model_validate(r["spec"]) for r in _rows(tx, ConfigKind.AUTO_JOURNAL_DEFINITION, statuses)
    ]
    policies = [AccountingPolicy.model_validate(r["spec"]) for r in _rows(tx, ConfigKind.ACCOUNTING_POLICY, statuses)]
    return Context(
        {e.event_type: e for e in event_types},
        {m.mapping_id: m for m in mappings},
        charts[0] if charts else None,
        dims[0] if dims else None,
        {d.definition_id: d for d in definitions},
        policies[0] if policies else None,
    )


def _definition_problems(model: AutoJournalDefinition, ctx: Context) -> list[Issue]:
    issues = definition_issues(
        model, ctx.event_types, mappings=ctx.mappings, chart=ctx.chart, dimensions=ctx.dimensions
    )
    for i, line in enumerate(model.lines):
        mapping = line.account.mapping
        if mapping is not None and mapping not in ctx.mappings:
            issues.append(
                Issue("unknown_mapping", f"/lines/{i}/account/mapping", f"account mapping {mapping} does not exist")
            )
    return issues


def save_checks(tx: Connection, model: BaseModel) -> None:
    """References a new version names must exist (in any live version)."""
    if isinstance(model, AutoJournalDefinition):
        problems = _definition_problems(model, context(tx, LIVE))
        if problems:
            raise invalid(problems)
    elif isinstance(model, AccountMapping):
        chart = context(tx, LIVE).chart
        if chart is not None and (problems := mapping_issues(model, chart)):
            raise invalid(problems)


def _in_use(ctx: Context) -> dict[str, list[str]]:
    users: dict[str, list[str]] = {}
    for d in ctx.definitions.values():
        for account in definition_accounts(d, ctx.mappings):
            users.setdefault(account, []).append(d.definition_id)
    for m in ctx.mappings.values():
        for account in m.accounts():
            users.setdefault(account, []).append(m.mapping_id)
    return users


def _with(ctx: Context, model: BaseModel) -> Context:
    """``ctx`` with ``model`` in place of the active version of its configuration."""
    if isinstance(model, EventType):
        others = {code: e for code, e in ctx.event_types.items() if e.type_id != model.type_id}
        return replace(ctx, event_types={**others, model.event_type: model})
    if isinstance(model, AccountMapping):
        return replace(ctx, mappings={**ctx.mappings, model.mapping_id: model})
    if isinstance(model, DimensionSet):
        return replace(ctx, dimensions=model)
    return ctx


def _broken_definitions(active: Context, model: BaseModel) -> list[Issue]:
    """Problems a new version would add to the active definitions (problems they already have don't count)."""
    candidate = _with(active, model)
    problems: list[Issue] = []
    for definition_id, d in sorted(active.definitions.items()):
        before = set(_definition_problems(d, active))
        problems += [
            Issue(i.code, i.path, f"{i.message}; active definition {definition_id} would break")
            for i in _definition_problems(d, candidate)
            if i not in before
        ]
    return problems


def _users(active: Context, model: BaseModel) -> list[str]:
    """The active configurations that use ``model``."""
    definitions = sorted(active.definitions.values(), key=lambda d: d.definition_id)
    if isinstance(model, EventType):
        return [d.definition_id for d in definitions if model.event_type in d.event_types]
    if isinstance(model, AccountMapping):
        return [d.definition_id for d in definitions if any(ln.account.mapping == model.mapping_id for ln in d.lines)]
    if isinstance(model, ChartOfAccounts):
        return [d.definition_id for d in definitions] + sorted(active.mappings)
    if isinstance(model, (DimensionSet, AccountingPolicy)):
        return [d.definition_id for d in definitions]
    return []


def retirement_checks(tx: Connection, row: RowMapping) -> None:
    """Nothing active may still use a version being retired."""
    kind = ConfigKind(row["kind"])
    if kind not in KINDS:
        return
    model, _ = parse(kind, row["spec"])
    users = _users(context(tx, ("active",)), model)
    if users:
        detail = f"active configurations use it: {', '.join(users)}; retire them first, or add a version to it"
        raise Problem(409, "The configuration is in use", detail, kind="conflict")


def activation_checks(tx: Connection, row: RowMapping) -> None:
    """References must be active; singletons stay single; a new version keeps the active definitions valid."""
    kind = ConfigKind(row["kind"])
    if kind not in KINDS:
        return
    model, _ = parse(kind, row["spec"])
    active = context(tx, ("active",))
    problems: list[Issue] = []
    if kind in SINGLETONS:
        others = [r["config_id"] for r in _rows(tx, kind, ("active",)) if r["config_id"] != row["config_id"]]
        if others:
            detail = f"{others[0]} is active: add a version to it, or retire it when nothing active uses it"
            raise Problem(409, "Another one is active", detail, kind="conflict")
    if isinstance(model, AutoJournalDefinition):
        for missing, code, what in (
            (active.chart is None, "no_active_chart", "chart of accounts"),
            (active.dimensions is None, "no_active_dimensions", "dimension set"),
            (active.policy is None, "no_active_policy", "accounting policy"),
        ):
            if missing:
                problems.append(Issue(code, "/", f"no {what} is active"))
        problems += _definition_problems(model, active)
    elif isinstance(model, AccountMapping):
        if active.chart is None:
            problems.append(Issue("no_active_chart", "/", "no chart of accounts is active"))
        else:
            problems += mapping_issues(model, active.chart)
    elif isinstance(model, ChartOfAccounts):
        problems += chart_change_issues(model, _in_use(active))
    elif isinstance(model, EventType):
        clash = active.event_types.get(model.event_type)
        if clash is not None and clash.type_id != model.type_id:
            problems.append(Issue("duplicate_code", "/event_type", f"{model.event_type} is active as {clash.type_id}"))
    if isinstance(model, (EventType, AccountMapping, DimensionSet)):
        problems += _broken_definitions(active, model)
    if problems:
        raise Problem(409, "The configuration's references are not ready", kind="conflict", issues=_issues(problems))
