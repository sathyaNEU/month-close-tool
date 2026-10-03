"""Match-set expression validation: every template passes, and bad operands are reported with their paths."""

from __future__ import annotations

from typing import Any

from close_contracts.config.match_set import MatchSet
from close_contracts.config.validate import match_set_issues
from close_contracts.default_sources import col, fn, s
from close_contracts.templates import MATCH_SETS


def test_every_template_match_set_validates() -> None:
    for m in MATCH_SETS:
        assert match_set_issues(m) == [], m.match_set_id


def _set(conditions: list[Any], **extra: Any) -> MatchSet:
    rule = {"rule_id": "rule_one", "name": "r", "kind": "1:1", "sides": ["a", "b"], "outcome": "auto"}
    return MatchSet.model_validate(
        {
            "match_set_id": "test_set",
            "name": "t",
            "entity_ids": ["US01"],
            "sources": [{"role": "a", "source_id": "src_a"}, {"role": "b", "source_id": "src_b"}],
            "pass_rules": [rule | {"conditions": conditions}],
            "owners": {"preparer": "p", "reviewer": "r"},
            **extra,
        }
    )


def _key(left: str, right: str) -> dict[str, Any]:
    return {"condition": "key_equals", "left": col(left), "right": col(right)}


def test_bad_operands_are_reported_with_paths() -> None:
    issues = match_set_issues(
        _set(
            [
                _key("a.reference", "b.amount_minor"),  # a string key against an integer
                {"condition": "amount", "left": col("a.reference"), "right": col("b.amount_minor"), "mode": "exact"},
                {"condition": "expression", "expr": fn("eq", col("c.reference"), s("x"))},  # role c is not in the rule
            ]
        )
    )
    paths = {(i.code, i.path) for i in issues}
    assert ("type_mismatch", "/pass_rules/0/conditions/0") in paths
    assert any(p.startswith("/pass_rules/0/conditions/1/left") for _, p in paths)
    assert any(p.startswith("/pass_rules/0/conditions/2/expr") for _, p in paths)


def test_scopes_and_timing_conditions_must_be_boolean() -> None:
    ms = _set(
        [_key("a.reference", "b.reference")],
        sources=[{"role": "a", "source_id": "src_a", "scope": col("a.reference")}, {"role": "b", "source_id": "src_b"}],
        timing_rules=[
            {
                "rule_id": "late",
                "role": "b",
                "when": col("b.amount_minor"),
                "expected_clear_business_days": 2,
                "calendar": "US_FED",
            }
        ],
    )
    paths = {i.path for i in match_set_issues(ms)}
    assert "/sources/0/scope" in paths
    assert "/timing_rules/0/when" in paths
