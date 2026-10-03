"""The waterfall: ambiguity policies, suggestions and supersession, aggregate kinds, membership, limits
and timing, on small hand-built match sets."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from close_datagen.world import CALENDARS
from close_engine.reference.matcher import Result, run

from .kit import DAY, amount, by_source, col, fn, item, key, match_set, rule, s


def _run(rules: list[Any], *items: Any, **extra: Any) -> Result:
    return run(match_set(rules, **extra), by_source(*items), run_id="run1", calendars=CALENDARS)


def _groups(r: Result) -> list[tuple[str, str, tuple[str, ...], bool]]:
    return sorted((g.match_type, g.status, g.item_ids, g.ambiguous) for g in r.groups)


def test_unique_only_takes_only_pairs_that_are_each_others_only_candidate() -> None:
    r = _run(
        [rule("exact", "1:1", [key(), amount()])],
        item("a1", 100, reference="R1"),
        item("b1", 100, reference="R1"),
        item("a2", 100, reference="R2"),
        item("b2", 100, reference="R2"),
        item("b3", 100, reference="R2"),
    )
    assert _groups(r) == [("automatic", "active", ("a1", "b1"), False)]
    assert r.status == {"a1": "matched", "b1": "matched", "a2": "open", "b2": "open", "b3": "open"}


def test_best_unique_takes_the_strictly_best_pair() -> None:
    items = [
        item("a1", 100, reference="R1"),
        item("b1", 100, reference="R1"),
        item("b2", 103, reference="R1"),
        item("a2", 100, reference="R2"),
        item("b3", 102, reference="R2"),
        item("b4", 98, reference="R2"),
    ]
    r = _run([rule("near", "1:1", [key(), amount("absolute", absolute_minor=5)], ambiguity="best_unique")], *items)
    assert _groups(r) == [("automatic", "active", ("a1", "b1"), False)]  # a2 has two equally good candidates


def test_greedy_downgrades_a_tie_to_a_suggestion_unless_the_rule_allows_it() -> None:
    items = [item("a1", 100, reference="R1"), item("b1", 101, reference="R1"), item("b2", 99, reference="R1")]
    greedy = [amount("absolute", absolute_minor=5)]
    r = _run([rule("near", "1:1", [key(), *greedy], ambiguity="greedy_total_order")], *items)
    assert _groups(r) == [("suggested", "pending", ("a1", "b1"), True)]
    allowed = rule("near", "1:1", [key(), *greedy], ambiguity="greedy_total_order", allow_ambiguous_auto=True)
    assert _groups(_run([allowed], *items)) == [("automatic", "active", ("a1", "b1"), True)]


def test_greedy_takes_pairs_in_total_order() -> None:
    items = [
        item("a1", 100, reference="R"),
        item("a2", 200, reference="R"),
        item("b1", 201, reference="R"),
        item("b2", 104, reference="R"),
    ]
    rules = [rule("near", "1:1", [key(), amount("absolute", absolute_minor=150)], ambiguity="greedy_total_order")]
    assert _groups(_run(rules, *items)) == [
        ("automatic", "active", ("a1", "b2"), False),
        ("automatic", "active", ("a2", "b1"), False),
    ]


def test_an_automatic_match_supersedes_a_suggestion_and_frees_its_other_items() -> None:
    rules = [
        rule("by_reference", "1:1", [key(), amount("absolute", absolute_minor=10)], outcome="suggested"),
        rule("by_reference_2", "1:1", [key("a.reference_2", "b.reference_2"), amount()]),
    ]
    items = [
        item("a1", 100, reference="R1", reference_2="X"),
        item("b1", 95, reference="R1"),
        item("b2", 100, reference="Q", reference_2="X"),
    ]
    r = _run(rules, *items)
    assert _groups(r) == [
        ("automatic", "active", ("a1", "b2"), False),
        ("suggested", "superseded", ("a1", "b1"), False),
    ]
    assert r.status["b1"] == "open"
    assert r.metrics["by_reference_2"]["superseded"] == 1


def test_a_suggested_item_is_not_in_the_pool_of_later_suggestion_rules() -> None:
    rules = [
        rule("first", "1:1", [key(), amount("absolute", absolute_minor=10)], outcome="suggested"),
        rule(
            "second",
            "1:1",
            [key("a.reference_2", "b.reference_2"), amount("absolute", absolute_minor=10)],
            outcome="suggested",
        ),
    ]
    items = [
        item("a1", 100, reference="R1", reference_2="X"),
        item("b1", 95, reference="R1"),
        item("b2", 96, reference_2="X"),
    ]
    assert _groups(_run(rules, *items)) == [("suggested", "pending", ("a1", "b1"), False)]


def _one_to_many(*conditions: Any, **extra: Any) -> Any:
    return rule("batch", "1:M", [key(), *conditions], group_by=[col("b.reference")], **extra)


def test_one_to_many_whole_group_subset_and_tie() -> None:
    items = [
        item("a1", 100, reference="G1"),
        item("b1", 60, reference="G1"),
        item("b2", 40, reference="G1"),
        item("a2", 100, reference="G2"),
        item("b3", 100, reference="G2"),
        item("b4", 7, reference="G2"),
        item("a3", 50, reference="G3"),
        item("b5", 50, reference="G3"),
        item("b6", 50, reference="G3"),
    ]
    r = _run([_one_to_many(amount())], *items)
    assert _groups(r) == [
        ("automatic", "active", ("a1", "b1", "b2"), False),
        ("automatic", "active", ("a2", "b3"), False),
        ("suggested", "pending", ("a3", "b5"), True),  # the lexicographically smallest of two
    ]
    assert r.status["b4"] == "open"


def test_contested_many_side_items_block_both_one_side_items() -> None:
    items = [item("a1", 100, reference="G"), item("a2", 100, reference="G"), item("b1", 100, reference="G")]
    r = _run([_one_to_many(amount())], *items)
    assert r.groups == []
    assert r.flags["b1"] == {"ambiguous"}


def test_many_to_one_sums_the_left_side() -> None:
    rules = [rule("collect", "M:1", [key(), amount()], group_by=[col("a.reference")])]
    items = [item("a1", 30, reference="G"), item("a2", 70, reference="G"), item("b1", 100, reference="G")]
    assert _groups(_run(rules, *items)) == [("automatic", "active", ("a1", "a2", "b1"), False)]


def test_only_unique_only_is_implemented_for_aggregates() -> None:
    with pytest.raises(NotImplementedError):
        _run(
            [_one_to_many(amount(), ambiguity="best_unique")],
            item("a1", 1, reference="G"),
            item("b1", 1, reference="G"),
        )


def test_membership_by_entity_and_scope() -> None:
    sources = {
        "a": {"source_id": "src_a", "scope": fn("eq", col("a.text_1"), s("keep"))},
        "b": {"source_id": "src_b"},
    }
    items = [
        item("a1", 100, reference="R", text_1="keep"),
        item("a2", 100, reference="R", text_1="drop"),
        item("a3", 100, reference="R", text_1="keep", entity_id="EU01"),
        item("b1", 100, reference="R"),
    ]
    r = _run([rule("exact", "1:1", [key(), amount()])], *items, sources=sources)
    assert sorted(r.members) == ["a1", "b1"]
    assert _groups(r) == [("automatic", "active", ("a1", "b1"), False)]


def test_one_source_in_two_roles_must_not_overlap() -> None:
    sources = {"a": {"source_id": "src_a"}, "b": {"source_id": "src_a"}}
    with pytest.raises(ValueError, match="two roles"):
        _run([rule("exact", "1:1", [key(), amount()])], item("a1", 1, reference="R"), sources=sources)


def test_limits_flag_items_instead_of_matching_them() -> None:
    rules = [
        rule("exact", "1:1", [key(), amount("absolute", absolute_minor=10)], limits={"max_candidates_per_item": 1})
    ]
    items = [item("a1", 100, reference="R"), item("b1", 100, reference="R"), item("b2", 101, reference="R")]
    r = _run(rules, *items)
    assert r.groups == []
    assert r.flags["a1"] == {"too_many_candidates"}
    block = [rule("exact", "1:1", [key(), amount()], limits={"max_block_pairs": 1})]
    r = _run(block, item("a1", 100, reference="R"), item("b1", 100, reference="R"), item("b2", 5, reference="R"))
    assert r.groups == []
    assert r.flags["b2"] == {"too_many_candidates"}


def test_timing_rules_classify_open_items_until_their_expected_clear_date() -> None:
    timing = [
        {
            "rule_id": "in_transit",
            "role": "b",
            "when": fn("eq", col("b.text_1"), s("wire")),
            "expected_clear_business_days": 2,
            "calendar": "US_FED",
        }
    ]
    items = [item("a1", 100, reference="R"), item("b1", 55, reference="Q", text_1="wire"), item("b2", 7, text_1="cash")]
    ms = match_set([rule("exact", "1:1", [key(), amount()])], timing_rules=timing)
    early = run(ms, by_source(*items), run_id="r", calendars=CALENDARS, as_of=DAY + timedelta(days=1))
    assert early.status == {"a1": "open", "b1": "in_transit", "b2": "open"}
    assert early.expected_clear["b1"] == DAY + timedelta(days=5)  # Thursday, then Monday: 3 July is a holiday
    late = run(ms, by_source(*items), run_id="r", calendars=CALENDARS, as_of=DAY + timedelta(days=7))
    assert late.status["b1"] == "open"
    assert run(ms, by_source(*items), run_id="r", calendars=CALENDARS).status["b1"] == "open"


def test_group_ids_depend_on_the_run_and_the_items() -> None:
    items = [item("a1", 100, reference="R"), item("b1", 100, reference="R")]
    ms = match_set([rule("exact", "1:1", [key(), amount()])])
    first = run(ms, by_source(*items), run_id="run1", calendars=CALENDARS).groups[0].group_id
    again = run(ms, by_source(*reversed(items)), run_id="run1", calendars=CALENDARS).groups[0].group_id
    other = run(ms, by_source(*items), run_id="run2", calendars=CALENDARS).groups[0].group_id
    assert first == again != other


def test_other_kinds_are_not_in_the_reference_yet() -> None:
    netting = rule(
        "net", "self_net", [amount(right=fn("sub", col("a.amount_minor"), col("a.amount_minor")))], sides="a"
    )
    with pytest.raises(NotImplementedError):
        _run([netting], item("a1", 1))


def test_keyless_rules_block_on_a_date_window_with_an_amount() -> None:
    window = {
        "condition": "date_window",
        "left": col("a.transaction_date"),
        "right": col("b.transaction_date"),
        "unit": "calendar_days",
        "before": 0,
        "after": 2,
    }
    items = [
        item("a1", 100),
        item("b1", 100, transaction_date=DAY + timedelta(days=1)),
        item("b2", 100, transaction_date=DAY + timedelta(days=5)),
    ]
    assert _groups(_run([rule("near", "1:1", [window, amount()])], *items)) == [
        ("automatic", "active", ("a1", "b1"), False)
    ]
    grouped = rule("near", "1:M", [window, amount()], group_by=[col("b.reference")])
    items = [item("a1", 100), item("b1", 60, reference="G"), item("b2", 40, reference="G"), item("b3", 100)]
    assert _groups(_run([grouped], *items)) == [("automatic", "active", ("a1", "b1", "b2"), False)]


def test_undecided_aggregates_stand_in_the_way_of_their_candidates() -> None:
    too_many_groups = _one_to_many(amount(), limits={"max_candidates_per_item": 1})
    items = [item("a1", 5, reference="G"), item("b1", 5, reference="G")]
    grouped = rule(
        "batch", "1:M", [key(), amount()], group_by=[col("b.reference_2")], limits={"max_candidates_per_item": 1}
    )
    r = _run(
        [grouped], *items, item("b2", 5, reference="G", reference_2="Y"), item("b3", 1, reference="G", reference_2="Z")
    )
    assert r.groups == []
    assert r.flags["a1"] == {"too_many_candidates"}
    budget = _one_to_many(amount(), limits={"work_budget": 10})
    many = [item(f"b{i}", 3, reference="G") for i in range(8)]
    r = _run([budget], item("a1", 5, reference="G"), item("a2", 3, reference="H"), *many)
    assert r.flags["a1"] == {"search_limit_reached"}
    assert r.groups == []
    assert _run([too_many_groups], *items).groups != []


def test_items_without_a_group_key_or_amount_stay_out_of_aggregates() -> None:
    rules = [rule("batch", "1:M", [key(), amount(right=col("b.number_1"))], group_by=[col("b.reference_2")])]
    items = [
        item("a1", 100, reference="G"),
        item("b1", 0, reference="G", number_1=100),  # no group key
        item("b2", 0, reference="G", reference_2="K"),  # no amount
    ]
    assert _run(rules, *items).groups == []
