"""Small builders for reference-matcher tests: items, conditions, rules and match sets."""

from __future__ import annotations

from datetime import date
from typing import Any

from close_contracts.config.match_set import MatchSet, PassRule
from close_contracts.default_sources import col, fn, lit, s
from close_engine.reference.items import Item

DAY = date(2026, 7, 1)
__all__ = ["col", "fn", "lit", "s"]


def item(item_id: str, amount: int, source: str = "", **fields: Any) -> Item:
    """An item whose source is named after its id's first letter unless given (a1 -> src_a)."""
    base = {"entity_id": "US01", "currency": "USD", "transaction_date": DAY, "amount_minor": amount}
    return Item(item_id, source or f"src_{item_id[0]}", f'["{item_id}"]', base | fields)


def key(left: str = "a.reference", right: str = "b.reference") -> dict[str, Any]:
    return {"condition": "key_equals", "left": col(left), "right": col(right)}


def amount(mode: str = "exact", left: Any = None, right: Any = None, **tolerance: int) -> dict[str, Any]:
    return {
        "condition": "amount",
        "left": left or col("a.amount_minor"),
        "right": right or col("b.amount_minor"),
        "mode": mode,
        **tolerance,
    }


def rule(rule_id: str, kind: str, conditions: list[Any], outcome: str = "auto", sides: str = "ab", **extra: Any) -> Any:
    return {"rule_id": rule_id, "name": rule_id, "kind": kind, "sides": list(sides), "outcome": outcome, **extra} | {
        "conditions": conditions
    }


def compiled_rule(kind: str, conditions: list[Any], **extra: Any) -> PassRule:
    return PassRule.model_validate(rule("rule_one", kind, conditions, **extra))


def match_set(rules: list[Any], *, sources: dict[str, Any] | None = None, **extra: Any) -> MatchSet:
    bindings = sources or {"a": {"source_id": "src_a"}, "b": {"source_id": "src_b"}}
    return MatchSet.model_validate(
        {
            "match_set_id": "test_set",
            "name": "test",
            "entity_ids": ["US01"],
            "sources": [{"role": role, **binding} for role, binding in bindings.items()],
            "pass_rules": rules,
            "owners": {"preparer": "p1", "reviewer": "r1"},
            **extra,
        }
    )


def by_source(*items: Item) -> dict[str, list[Item]]:
    out: dict[str, list[Item]] = {}
    for i in items:
        out.setdefault(i.source_id, []).append(i)
    return out
