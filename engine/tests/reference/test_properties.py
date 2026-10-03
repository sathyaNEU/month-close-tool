"""Invariants of the reference matcher on random match sets and items (Gate 3: 10,000 examples).

``CLOSE_REFERENCE_EXAMPLES`` sets the number of examples (200 by default, 10,000 in the gate step).
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import timedelta
from typing import Any

from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from close_contracts.config.match_set import MatchSet, RuleKind
from close_datagen.world import CALENDARS
from close_engine.reference.conditions import compile_rule, group_key, left_key, pair_ok, right_key, totals_ok, values
from close_engine.reference.items import Item
from close_engine.reference.matcher import Result, run
from close_engine.reference.runner import digest

from .kit import DAY, amount, by_source, col, item, key, match_set, rule

EXAMPLES = int(os.environ.get("CLOSE_REFERENCE_EXAMPLES", "200"))


@st.composite
def _rules(draw: st.DrawFn, index: int) -> Any:
    kind = draw(st.sampled_from(["1:1", "1:M", "M:1"]))
    keyed = draw(st.booleans())
    tolerance = draw(st.sampled_from([amount(), amount("absolute", absolute_minor=draw(st.integers(1, 5)))]))
    conditions: list[Any] = [tolerance]
    if keyed:
        conditions.append(key())
    else:
        window = {
            "condition": "date_window",
            "left": col("a.transaction_date"),
            "right": col("b.transaction_date"),
            "unit": "calendar_days",
            "before": draw(st.integers(0, 2)),
            "after": draw(st.integers(0, 2)),
        }
        conditions.append(window)
    outcome = draw(st.sampled_from(["auto", "suggested"]))
    extra: dict[str, Any] = {"outcome": outcome}
    if kind == "1:1":
        extra["ambiguity"] = draw(st.sampled_from(["unique_only", "best_unique", "greedy_total_order"]))
        if outcome == "auto" and extra["ambiguity"] == "greedy_total_order":
            extra["allow_ambiguous_auto"] = draw(st.booleans())
    else:
        many = "b" if kind == "1:M" else "a"
        extra["group_by"] = [col(f"{many}.{draw(st.sampled_from(['reference', 'reference_2']))}")]
    if draw(st.booleans()):
        extra["limits"] = {"max_candidates_per_item": draw(st.integers(1, 4))}
    return rule(f"rule_{index}", kind, conditions, **extra)


@st.composite
def _items(draw: st.DrawFn, side: str) -> list[Item]:
    out = []
    for n in range(draw(st.integers(1, 7))):
        fields = {
            "entity_id": draw(st.sampled_from(["US01", "US01", "US01", "EU01"])),
            "reference": draw(st.sampled_from([None, "R1", "R1", "R2"])),
            "reference_2": draw(st.sampled_from([None, "G1", "G1", "G2"])),
            "transaction_date": DAY + timedelta(days=draw(st.integers(0, 2))),
        }
        # A few amounts that add up to each other, so ties, subsets and near misses are common.
        out.append(item(f"{side}{n}", draw(st.sampled_from([0, 5, 10, 15, 20, 25, -5, 7])), **fields))
    return out


@st.composite
def _cases(draw: st.DrawFn) -> tuple[MatchSet, list[Item]]:
    rules = [draw(_rules(i)) for i in range(draw(st.integers(1, 3)))]
    kind = draw(st.sampled_from(["matching", "matching", "intercompany"]))
    ms = match_set(rules, kind=kind, entity_ids=["US01", "EU01"])
    return ms, draw(_items("a")) + draw(_items("b"))


def _check_holdings(r: Result) -> None:
    active: Counter[str] = Counter()
    pending: Counter[str] = Counter()
    for g in r.groups:
        if g.status == "active":
            active.update(g.item_ids)
        elif g.status == "pending":
            pending.update(g.item_ids)
    for i in r.members:
        assert active[i] <= 1, i
        assert pending[i] <= 1, i
        assert not (active[i] and pending[i]), i
        expected = "matched" if active[i] else "suggested" if pending[i] else "open"
        assert r.status[i] == expected, i


def _check_groups(ms: MatchSet, r: Result) -> None:
    rules = {rule.rule_id: rule for rule in ms.pass_rules}
    for g in r.groups:
        rule = rules[g.rule_id]
        c = compile_rule(rule)
        members = [r.members[i] for i in g.item_ids]
        if ms.kind == "matching":
            assert len({m.entity_id for m in members}) == 1, g
        left = [m for m in members if m.role == c.roles[0]]
        right = [m for m in members if m.role == c.roles[1]]
        one_left = {RuleKind.ONE_TO_ONE: (1, 1), RuleKind.ONE_TO_MANY: (1, None), RuleKind.MANY_TO_ONE: (None, 1)}
        want_left, want_right = one_left[rule.kind]
        assert left, g
        assert right, g
        assert want_left is None or len(left) == want_left, g
        assert want_right is None or len(right) == want_right, g
        for x in left:
            for y in right:
                assert pair_ok(c, x.row | y.row, CALENDARS), g
                if c.keys:
                    assert left_key(c, x.row, CALENDARS) == right_key(c, y.row, CALENDARS) is not None, g
        many = right if rule.kind == RuleKind.ONE_TO_MANY else left if rule.kind == RuleKind.MANY_TO_ONE else []
        assert len({group_key(c, m.row, CALENDARS) for m in many}) <= 1, g
        assert None not in {group_key(c, m.row, CALENDARS) for m in many}, g
        totals = []
        for side, side_members in (("left", left), ("right", right)):
            vs = [values(c, side, m.row, CALENDARS) for m in side_members]
            assert all(v is not None for v in vs), g
            totals.append(tuple(sum(col_) for col_ in zip(*vs, strict=True)))  # type: ignore[arg-type]
        assert totals_ok(c, totals[0], totals[1]), g


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(case=_cases(), order=st.randoms(use_true_random=False))
def test_reference_matcher_invariants(case: tuple[MatchSet, list[Item]], order: Any) -> None:
    ms, items = case
    r = run(ms, by_source(*items), run_id="p1", calendars=CALENDARS)
    event(f"groups: {min(len(r.groups), 3)}")
    for label, seen in (
        ("a suggestion", any(g.status == "pending" for g in r.groups)),
        ("a superseded suggestion", any(g.status == "superseded" for g in r.groups)),
        ("an ambiguous group", any(g.ambiguous for g in r.groups)),
        ("an aggregate group", any(len(g.item_ids) > 2 for g in r.groups)),
        ("a flagged item", any(r.flags.values())),
    ):
        if seen:
            event(label)
    _check_holdings(r)
    _check_groups(ms, r)
    shuffled = list(items)
    order.shuffle(shuffled)
    again = run(ms, by_source(*shuffled), run_id="p2", calendars=CALENDARS)
    assert digest(again) == digest(r)
