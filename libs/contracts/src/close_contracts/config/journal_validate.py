"""Validating journaling configuration: expressions against their schemas, and references between
configurations (ADR-0015). The app runs these when a version is saved and activated; the engine journals
only configuration that passes them.

- **Event sources** typecheck against their landing table, plus the columns of each lookup (``<alias>_<column>``).
  A lookup must name the target table's full identity key, so it finds at most one row.
- **Event fields.** Mappings produce the declared types: amounts are BIGINT minor units, attributes and the
  standard ids are strings, and the accounting date is a date. Identities are boolean expressions over the
  event's fields.
- **Rules** (definitions and their lines) typecheck against every event type they cover. The schema is the
  event's fields, its ``event_type``, and the policy switches (``policy_*``).
- **References.** Accounts must exist, be postable and be active in the chart. Mappings must exist and take the
  right number of keys, and dimensions must exist.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from close_contracts.binding import dsl_schema
from close_contracts.config.journaling import (
    AccountMapping,
    AutoJournalDefinition,
    ChartOfAccounts,
    DimensionSet,
    EventType,
)
from close_contracts.default_sources import IDENTITY_KEYS
from close_contracts.dsl import DslType, DslTypeError, Ir, Issue, typecheck
from close_contracts.table_schemas import REGISTRY

POLICY_FIELDS: Final = (
    "policy_trigger",
    "policy_model",
    "policy_shipping",
    "policy_incentives",
    "policy_psp_receivable",
    "policy_processing_fees",
    "policy_chargebacks",
)
_STANDARD_TYPES: Final = {
    "entity_id": DslType.STRING,
    "currency": DslType.STRING,
    "accounting_date": DslType.DATE,
    "program": DslType.STRING,
    "order_id": DslType.STRING,
    "seller_id": DslType.STRING,
}


def landing_schema(table: str) -> dict[str, DslType] | None:
    logical = next((t for t in REGISTRY if t.key == table), None)
    return None if logical is None else dsl_schema(REGISTRY[logical])


def field_types(event_type: EventType) -> dict[str, DslType]:
    """The declared fields of an event, as a DSL schema."""
    out = dict(_STANDARD_TYPES)
    out |= {f"amount_{a}": DslType.BIGINT for a in event_type.amounts}
    out |= {f"attr_{a}": DslType.STRING for a in event_type.attributes}
    return out


def rule_schema(event_type: EventType) -> dict[str, DslType]:
    """What a rule expression can read: the event's fields, its code and the policy switches."""
    return field_types(event_type) | {"event_type": DslType.STRING} | dict.fromkeys(POLICY_FIELDS, DslType.STRING)


class _Checker:
    def __init__(self) -> None:
        self.issues: list[Issue] = []

    def check(self, expr: Any, schema: Mapping[str, DslType], path: str, expect: DslType | None = None) -> Ir | None:
        try:
            return typecheck(expr, schema, expect=expect)
        except DslTypeError as exc:
            self.issues.extend(Issue(i.code, f"{path}{i.path}", i.message) for i in exc.issues)
            return None

    def add(self, code: str, path: str, message: str) -> None:
        self.issues.append(Issue(code, path, message))


def source_schema(source_table: str, lookups: Any, c: _Checker, path: str) -> dict[str, DslType] | None:
    """The columns a source's filter and mapping can read: its own, plus each lookup's with the alias prefix."""
    base = landing_schema(source_table)
    if base is None:
        c.add("unknown_table", f"{path}/landing_table", f"{source_table} is not a registered landing table")
        return None
    schema = dict(base)
    for i, lookup in enumerate(lookups):
        target = landing_schema(lookup.table)
        where = f"{path}/lookups/{i}"
        if target is None:
            c.add("unknown_table", f"{where}/table", f"{lookup.table} is not a registered landing table")
            continue
        identity = IDENTITY_KEYS.get(lookup.table, ())
        if sorted(lookup.on) != sorted(identity):
            c.add("lookup_key", f"{where}/on", f"a lookup names the full identity key of {lookup.table}: {identity}")
        for column, expr in lookup.on.items():
            if column in target:
                c.check(expr, base, f"{where}/on/{column}", target[column])
        schema |= {f"{lookup.alias}_{col}": t for col, t in target.items()}
    return schema


def event_type_issues(event_type: EventType) -> list[Issue]:
    c = _Checker()
    types = field_types(event_type)
    for i, source in enumerate(event_type.sources):
        path = f"/sources/{i}"
        schema = source_schema(source.landing_table, source.lookups, c, path)
        if schema is None:
            continue
        if source.filter is not None:
            c.check(source.filter, schema, f"{path}/filter", DslType.BOOLEAN)
        for field, expr in source.mapping.items():
            c.check(expr, schema, f"{path}/mapping/{field}", types[field])
    for i, identity in enumerate(event_type.identities):
        c.check(identity.expr, types, f"/identities/{i}/expr", DslType.BOOLEAN)
    return c.issues


def definition_issues(
    definition: AutoJournalDefinition,
    event_types: Mapping[str, EventType],
    *,
    mappings: Mapping[str, AccountMapping] | None = None,
    chart: ChartOfAccounts | None = None,
    dimensions: DimensionSet | None = None,
) -> list[Issue]:
    """Typecheck every expression against each covered event type; check references when they are given."""
    c = _Checker()
    for code in definition.event_types:
        event_type = event_types.get(code)
        if event_type is None:
            c.add("unknown_event_type", "/event_types", f"event type {code} does not exist")
            continue
        schema = rule_schema(event_type)
        at = f" (for {code})"
        if definition.condition is not None:
            c.check(definition.condition, schema, "/condition" + at, DslType.BOOLEAN)
        for i, line in enumerate(definition.lines):
            path = f"/lines/{i}"
            if line.condition is not None:
                c.check(line.condition, schema, f"{path}/condition" + at, DslType.BOOLEAN)
            c.check(line.amount, schema, f"{path}/amount" + at, DslType.BIGINT)
            for k, key in enumerate(line.account.keys):
                c.check(key, schema, f"{path}/account/keys/{k}" + at, DslType.STRING)
            for name, expr in line.dimensions.items():
                c.check(expr, schema, f"{path}/dimensions/{name}" + at, DslType.STRING)
            if line.description is not None:
                c.check(line.description, schema, f"{path}/description" + at, DslType.STRING)
    for i, line in enumerate(definition.lines):
        path = f"/lines/{i}"
        ref = line.account
        if ref.mapping is not None and mappings is not None:
            mapping = mappings.get(ref.mapping)
            if mapping is None:
                c.add("unknown_mapping", f"{path}/account/mapping", f"account mapping {ref.mapping} does not exist")
            elif len(mapping.keys) != len(ref.keys):
                c.add("mapping_keys", f"{path}/account/keys", f"{ref.mapping} takes {len(mapping.keys)} keys")
        if ref.account is not None and chart is not None:
            c.issues.extend(_account_issues(chart, ref.account, f"{path}/account/account"))
        if dimensions is not None:
            known = {d.code for d in dimensions.dimensions}
            for name in line.dimensions:
                if name not in known:
                    c.add("unknown_dimension", f"{path}/dimensions/{name}", f"dimension {name} does not exist")
    return c.issues


def _account_issues(chart: ChartOfAccounts, code: str, path: str) -> list[Issue]:
    account = chart.account(code)
    if account is None:
        return [Issue("unknown_account", path, f"account {code} is not in the chart")]
    if account.status != "active" or not account.postable:
        return [Issue("account_not_postable", path, f"account {code} is inactive or not postable")]
    return []


def mapping_issues(mapping: AccountMapping, chart: ChartOfAccounts) -> list[Issue]:
    issues: list[Issue] = []
    for i, row in enumerate(mapping.rows):
        issues += _account_issues(chart, row.account, f"/rows/{i}/account")
    if mapping.default_account is not None:
        issues += _account_issues(chart, mapping.default_account, "/default_account")
    return issues


def chart_change_issues(chart: ChartOfAccounts, in_use: Mapping[str, list[str]]) -> list[Issue]:
    """Accounts that active definitions or mappings use must stay active and postable in a new chart version."""
    issues: list[Issue] = []
    for code, users in sorted(in_use.items()):
        used_by = ", ".join(sorted(users))
        issues.extend(
            Issue(i.code, i.path, f"{i.message}; it is used by {used_by}")
            for i in _account_issues(chart, code, "/accounts")
        )
    return issues


def definition_accounts(definition: AutoJournalDefinition, mappings: Mapping[str, AccountMapping]) -> set[str]:
    """Every account a definition can post to (its literals and its mappings' accounts)."""
    out: set[str] = set()
    for line in definition.lines:
        ref = line.account
        if ref.account is not None:
            out.add(ref.account)
        elif ref.mapping is not None and ref.mapping in mappings:
            out |= mappings[ref.mapping].accounts()
    return out


def source_row_schema(source: Any) -> dict[str, DslType]:
    """The schema a valid source's filter and mapping read; raises with every problem when it is invalid."""
    c = _Checker()
    schema = source_schema(source.landing_table, source.lookups, c, "")
    if c.issues or schema is None:
        raise DslTypeError(c.issues or [Issue("unknown_table", "/landing_table", source.landing_table)])
    return schema
