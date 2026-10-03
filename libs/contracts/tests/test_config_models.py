"""Configuration models: 1,000 generated examples per model round-trip exactly through canonical JSON
(Gate 2a), the validation rules hold, and the JSON Schema snapshots have not drifted."""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from configkit import accounting_policies, data_sources, envelopes, exprs, match_sets
from hypothesis import HealthCheck, given, settings
from pydantic import BaseModel, ValidationError

from close_contracts.canonical_json import canonical_json
from close_contracts.config.data_source import DataSource
from close_contracts.config.envelope import ConfigKind, ConfigStatus, Envelope, spec_hash
from close_contracts.config.match_set import AmountTolerance, DateWindow, KeyEquals, MatchSet, PassRule, TextMatch
from close_contracts.dsl.ast import EXPR_ADAPTER, dump, parse

ROUND_TRIP = settings(max_examples=1000, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def _round_trip(model: BaseModel) -> None:
    text = canonical_json(model.model_dump(mode="json"))
    again = type(model).model_validate_json(text)
    assert again == model
    assert canonical_json(again.model_dump(mode="json")) == text


@ROUND_TRIP
@given(exprs)
def test_dsl_expressions_round_trip(expr: Any) -> None:
    text = canonical_json(dump(expr))
    assert EXPR_ADAPTER.validate_json(text) == expr
    assert parse(dump(expr)) == expr


@ROUND_TRIP
@given(data_sources())
def test_data_sources_round_trip(model: DataSource) -> None:
    _round_trip(model)


@ROUND_TRIP
@given(match_sets())
def test_match_sets_round_trip(model: MatchSet) -> None:
    _round_trip(model)


@ROUND_TRIP
@given(accounting_policies())
def test_accounting_policies_round_trip(model: BaseModel) -> None:
    _round_trip(model)


@ROUND_TRIP
@given(envelopes())
def test_envelopes_round_trip_and_keep_their_hash(model: Envelope[Any]) -> None:
    _round_trip(model)
    assert model.content_hash == spec_hash(model.spec)


KEY = {"condition": "key_equals", "left": {"col": "a.id"}, "right": {"col": "b.id"}}
RULE = {"rule_id": "rule_one", "name": "r", "kind": "1:1", "sides": ["a", "b"], "outcome": "auto", "conditions": [KEY]}
MATCH_SET = {
    "match_set_id": "set_one",
    "name": "s",
    "entity_ids": ["US01"],
    "sources": [{"role": "a", "source_id": "src_a"}, {"role": "b", "source_id": "src_b"}],
    "pass_rules": [RULE],
    "owners": {"preparer": "alice", "reviewer": "bob"},
}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            {"pass_rules": [{**RULE, "kind": "M:M", "conditions": [{"condition": "sign", "mode": "same"}]}]},
            "M:M rule needs an exact key",
        ),
        ({"pass_rules": [{**RULE, "conditions": [{"condition": "sign", "mode": "same"}]}]}, "must be able to block"),
        ({"pass_rules": [{**RULE, "kind": "1:M"}]}, "need a group_by key"),
        ({"pass_rules": [{**RULE, "kind": "self_net"}]}, "self_net rules have one side"),
        ({"pass_rules": [{**RULE, "sides": ["a", "c"]}]}, "no bound source"),
        ({"pass_rules": [RULE, RULE]}, "rule ids must be unique"),
        ({"pass_rules": [{**RULE, "allow_ambiguous_auto": True}]}, "allow_ambiguous_auto only applies"),
        ({"owners": {"preparer": "alice", "reviewer": "alice"}}, "must be different people"),
        ({"entity_ids": ["US01", "US01"]}, "entity ids must be distinct"),
        ({"sources": [{"role": "a", "source_id": "x_one"}, {"role": "a", "source_id": "x_two"}]}, "each role is bound"),
        ({"schedule": {"mode": "daily"}}, "needs at_utc"),
    ],
)
def test_match_set_rules(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        MatchSet.model_validate({**MATCH_SET, **change})


def test_keyless_rules_block_on_a_date_window_with_an_amount() -> None:
    window = {
        "condition": "date_window",
        "left": {"col": "a.d"},
        "right": {"col": "b.d"},
        "unit": "calendar_days",
        "before": 0,
        "after": 2,
    }
    amount = {"condition": "amount", "left": {"col": "a.x"}, "right": {"col": "b.x"}, "mode": "exact"}
    rule = PassRule.model_validate({**RULE, "conditions": [window, amount]})
    assert {type(c) for c in rule.conditions} == {DateWindow, AmountTolerance}


@pytest.mark.parametrize(
    ("condition", "message"),
    [
        (
            {"condition": "text", "op": "regex_extract_equals", "left": {"col": "a.r"}, "right": {"col": "b.r"}},
            "pattern is required",
        ),
        (
            {"condition": "text", "op": "equals", "left": {"col": "a.r"}, "right": {"col": "b.r"}, "pattern": "x(y)"},
            "pattern is required",
        ),
        (
            {
                "condition": "text",
                "op": "regex_extract_equals",
                "left": {"col": "a.r"},
                "right": {"col": "b.r"},
                "pattern": "(a+)+",
            },
            "nested_quantifier",
        ),
        (
            {
                "condition": "text",
                "op": "fuzzy",
                "left": {"col": "a.r"},
                "right": {"col": "b.r"},
                "algorithm": "levenshtein",
            },
            "fuzzy needs both",
        ),
        (
            {
                "condition": "date_window",
                "left": {"col": "a.d"},
                "right": {"col": "b.d"},
                "unit": "business_days",
                "before": 0,
                "after": 1,
            },
            "calendar is required",
        ),
        ({"condition": "amount", "left": {"col": "a.x"}, "right": {"col": "b.x"}, "mode": "bps"}, "takes exactly"),
        (
            {"condition": "amount", "left": {"col": "a.x"}, "right": {"col": "b.x"}, "mode": "exact", "bps": 5},
            "takes exactly",
        ),
        ({"condition": "currency", "mode": "equal", "tolerance_bps": 5}, "take no FX tolerance"),
    ],
)
def test_condition_rules(condition: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        PassRule.model_validate({**RULE, "conditions": [KEY, condition]})


def test_conditions_parse_to_their_types() -> None:
    text = {
        "condition": "text",
        "op": "contains",
        "left": {"col": "a.r"},
        "right": {"col": "b.r"},
        "min_needle_length": 6,
    }
    rule = PassRule.model_validate({**RULE, "conditions": [KEY, text]})
    assert isinstance(rule.conditions[0], KeyEquals)
    assert isinstance(rule.conditions[1], TextMatch)


DATA_SOURCE = {
    "source_id": "stripe_payouts",
    "landing_table": "landing.stripe_payouts",
    "identity_key": ["id"],
    "mapping": {"amount_minor": {"col": "amount"}},
}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"identity_key": ["a.id"]}, "plain landing column names"),
        ({"identity_key": ["id", "id"]}, "must be distinct"),
        ({"mapping": {"Amount": {"col": "amount"}}}, "canonical field names"),
        ({"landing_table": "ops.stripe_payouts"}, "String should match pattern"),
        ({"incremental": {"mode": "column"}}, "column is required"),
        ({"incremental": {"mode": "cdf", "column": "x"}}, "column is required"),
        ({"unknown": 1}, "Extra inputs are not permitted"),
    ],
)
def test_data_source_rules(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        DataSource.model_validate({**DATA_SOURCE, **change})


def _envelope(**fields: Any) -> Envelope[DataSource]:
    base: dict[str, Any] = {
        "kind": ConfigKind.DATA_SOURCE,
        "config_id": "stripe_payouts",
        "version": 1,
        "status": ConfigStatus.APPROVED,
        "effective_from": date(2026, 10, 1),
        "author": "alice",
        "approver": "carol",
    }
    return Envelope[DataSource].seal(DataSource.model_validate(DATA_SOURCE), **{**base, **fields})


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"approver": "alice"}, "maker-checker"),
        ({"status": ConfigStatus.ACTIVE, "approver": None}, "needs an approver"),
        ({"status": ConfigStatus.SUBMITTED}, "cannot have an approver yet"),
        ({"effective_to": date(2026, 10, 1)}, "effective_to must be after"),
    ],
)
def test_envelope_rules(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _envelope(**fields)


def test_a_tampered_specification_breaks_the_hash() -> None:
    sealed = _envelope().model_dump(mode="json")
    sealed["spec"]["identity_key"] = ["other"]
    with pytest.raises(ValidationError, match="content_hash does not match"):
        Envelope[DataSource].model_validate(sealed)


def test_json_schema_snapshots_have_not_drifted() -> None:
    path = Path(__file__).resolve().parents[3] / "tools" / "gen_schemas.py"
    spec = importlib.util.spec_from_file_location("gen_schemas", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--check"]) == 0
