"""The bounded subset search: whole group first, meet-in-the-middle held to brute force, and its limits."""

from __future__ import annotations

from itertools import combinations
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from close_contracts.config.match_set import Limits
from close_engine.reference.conditions import Compiled, Totals, compile_rule, totals_ok
from close_engine.reference.subset import SEARCH_LIMIT, TOO_MANY, solve

from .kit import amount, col, compiled_rule, key


def _compiled(*conditions: Any) -> Compiled:
    return compile_rule(compiled_rule("1:M", [key(), *conditions], group_by=[col("b.reference")]))


def _members(amounts: list[int]) -> list[tuple[str, Totals]]:
    return [(f"b{i:02d}", (a, a)) for i, a in enumerate(amounts)]


def _balanced(c: Compiled) -> Any:
    return lambda one, many: totals_ok(c, one, many)


def test_the_whole_group_wins_when_it_balances() -> None:
    c = _compiled(amount())
    found = solve(c, (100, 100), _members([30, 70, 0]), Limits(), _balanced(c))
    assert found.full_group
    assert found.solutions == [("b00", "b01", "b02")]


def test_a_unique_subset_and_two_tied_subsets() -> None:
    c = _compiled(amount())
    unique = solve(c, (100, 100), _members([100, 250]), Limits(), _balanced(c))
    assert unique.solutions == [("b00",)]
    tied = solve(c, (100, 100), _members([100, 100, 5]), Limits(), _balanced(c))
    assert tied.solutions == [("b00",), ("b01",)]


def test_limits() -> None:
    c = _compiled(amount())
    over = solve(c, (1, 1), _members([2] * 25), Limits(), _balanced(c))
    assert over.flag == TOO_MANY
    budget = solve(c, (1, 1), _members([2] * 20), Limits(work_budget=1000), _balanced(c))
    assert budget.flag == SEARCH_LIMIT
    assert budget.solutions == []


def test_sign_only_rules_search_every_subset() -> None:
    c = _compiled({"condition": "sign", "mode": "same"})
    found = solve(c, (5,), [("b0", (-3,)), ("b1", (1,))], Limits(), _balanced(c))
    assert found.solutions == [("b1",)]


def _brute(c: Compiled, one: Totals, members: list[tuple[str, Totals]]) -> list[tuple[str, ...]]:
    out = []
    for size in range(1, len(members) + 1):
        for chosen in combinations(members, size):
            total = tuple(sum(v[i] for _, v in chosen) for i in range(len(one)))
            if totals_ok(c, one, total):
                out.append(tuple(sorted(i for i, _ in chosen)))
    return sorted(set(out))


_CONDITIONS = st.sampled_from(
    [
        [amount()],
        [amount("absolute", absolute_minor=3)],
        [amount("bps", bps=500)],
        [amount(), {"condition": "sign", "mode": "absolute"}],
        [amount("absolute", absolute_minor=2), {"condition": "sign", "mode": "opposite"}],
    ]
)


@settings(max_examples=300, deadline=None)
@given(
    conditions=_CONDITIONS,
    one=st.integers(-60, 60),
    amounts=st.lists(st.integers(-40, 40), min_size=1, max_size=10),
)
def test_meet_in_the_middle_finds_exactly_the_brute_force_solutions(
    conditions: list[Any], one: int, amounts: list[int]
) -> None:
    c = _compiled(*conditions)
    members = _members(amounts)
    found = solve(c, (one, one), members, Limits(), _balanced(c))
    expected = _brute(c, (one, one), members)
    if found.full_group:
        assert tuple(m[0] for m in members) in expected
    else:
        assert found.solutions == expected
