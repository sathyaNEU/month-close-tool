"""Journaling configuration: the samples validate, and the validators catch bad expressions and references."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from close_contracts.config.journal_validate import (
    chart_change_issues,
    definition_accounts,
    definition_issues,
    event_type_issues,
    mapping_issues,
)
from close_contracts.config.journaling import AutoJournalDefinition, ChartOfAccounts, EventType
from close_contracts.default_sources import col, fn, s
from close_contracts.samples import journaling as sample


def test_every_sample_validates() -> None:
    types = {e.event_type: e for e in sample.SAMPLE_EVENT_TYPES}
    mappings = {m.mapping_id: m for m in sample.SAMPLE_MAPPINGS}
    for e in sample.SAMPLE_EVENT_TYPES:
        assert event_type_issues(e) == [], e.type_id
    for d in sample.SAMPLE_DEFINITIONS:
        assert (
            definition_issues(
                d, types, mappings=mappings, chart=sample.SAMPLE_CHART, dimensions=sample.SAMPLE_DIMENSIONS
            )
            == []
        )
    for m in sample.SAMPLE_MAPPINGS:
        assert mapping_issues(m, sample.SAMPLE_CHART) == []
    ids = [e.type_id for e in sample.SAMPLE_EVENT_TYPES] + [d.definition_id for d in sample.SAMPLE_DEFINITIONS]
    assert all(i.startswith("sample_") for i in ids)


def _event_type(**changes: Any) -> dict[str, Any]:
    base = sample.SAMPLE_EVENT_TYPES[0].model_dump(mode="json")
    return base | changes


def test_event_type_shape_rules() -> None:
    undeclared = _event_type()
    undeclared["sources"][0]["mapping"]["amount_unknown"] = col("tax_minor")
    with pytest.raises(ValidationError, match="undeclared"):
        EventType.model_validate(undeclared)
    incomplete = _event_type()
    del incomplete["sources"][0]["mapping"]["currency"]
    with pytest.raises(ValidationError, match="every source maps"):
        EventType.model_validate(incomplete)


def test_event_sources_typecheck_against_landing_tables() -> None:
    spec = _event_type()
    spec["sources"][0]["mapping"]["amount_tax"] = col("no_such_column")
    spec["sources"][0]["lookups"] = [
        {"alias": "seller", "table": "landing.sellers", "on": {"country": col("entity_id")}}
    ]
    issues = {(i.code, i.path.split("/")[-1]) for i in event_type_issues(EventType.model_validate(spec))}
    assert ("lookup_key", "on") in issues  # a lookup must name the target's identity key
    assert any(code == "unknown_column" for code, _ in issues)


def test_definitions_check_every_covered_event_type_and_every_reference() -> None:
    types = {e.event_type: e for e in sample.SAMPLE_EVENT_TYPES}
    spec = sample.SAMPLE_DEFINITIONS[0].model_dump(mode="json")
    spec["event_types"] = ["ORDER_CAPTURED", "PSP_PAYOUT"]  # PSP_PAYOUT has no amount_card
    spec["lines"][0]["dimensions"] = {"channel": s("web")}
    spec["lines"][1]["account"] = {"account": "0000"}
    spec["lines"][0]["account"] = {"mapping": "missing_mapping", "keys": [col("policy_psp_receivable")]}
    issues = definition_issues(
        AutoJournalDefinition.model_validate(spec),
        types,
        mappings={m.mapping_id: m for m in sample.SAMPLE_MAPPINGS},
        chart=sample.SAMPLE_CHART,
        dimensions=sample.SAMPLE_DIMENSIONS,
    )
    codes = {i.code for i in issues}
    assert {"unknown_column", "unknown_account", "unknown_mapping", "unknown_dimension"} <= codes
    unknown = AutoJournalDefinition.model_validate(spec | {"event_types": ["NOT_A_TYPE"]})
    assert {i.code for i in definition_issues(unknown, types)} == {"unknown_event_type"}


def test_a_chart_version_cannot_retire_an_account_in_use() -> None:
    mappings = {m.mapping_id: m for m in sample.SAMPLE_MAPPINGS}
    in_use: dict[str, list[str]] = {}
    for d in sample.SAMPLE_DEFINITIONS:
        for account in definition_accounts(d, mappings):
            in_use.setdefault(account, []).append(d.definition_id)
    assert chart_change_issues(sample.SAMPLE_CHART, in_use) == []
    spec = sample.SAMPLE_CHART.model_dump(mode="json")
    spec["accounts"] = [a | {"status": "inactive"} if a["code"] == "4010" else a for a in spec["accounts"]]
    issues = chart_change_issues(ChartOfAccounts.model_validate(spec), in_use)
    assert [i.code for i in issues] == ["account_not_postable"]
    assert "sample_revenue_at_trigger" in issues[0].message


def test_chart_and_mapping_shape_rules() -> None:
    spec = sample.SAMPLE_CHART.model_dump(mode="json")
    with pytest.raises(ValidationError, match="suspense_account"):
        ChartOfAccounts.model_validate(spec | {"suspense_account": "0000"})
    with pytest.raises(ValidationError, match="unique"):
        ChartOfAccounts.model_validate(spec | {"accounts": [*spec["accounts"], spec["accounts"][0]]})
    rule = fn("eq", col("a"), s("b"))
    assert rule["fn"] == "eq"
