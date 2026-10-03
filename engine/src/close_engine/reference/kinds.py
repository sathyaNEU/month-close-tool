"""How one pass rule forms its groups from the pool, kind by kind (roadmap "How one pass runs").

Every function takes the pool as it stood when the rule started and returns proposals; the waterfall in
``matcher`` applies them. Nothing here depends on input order: pools are sorted by item id, and every tie is
broken by item ids.

- **Blocking.** A keyed rule only pairs items whose keys are equal and not null or empty. A key block with
  more than ``max_block_pairs`` pairs is skipped and its items are flagged ``too_many_candidates``. A
  keyless rule is one block, and the block limit does not apply to it here (the Spark engine blocks keyless
  rules on date and amount buckets, which are an implementation detail).
- **``1:1``.** The candidate graph holds the pairs that pass every condition. An item with more than
  ``max_candidates_per_item`` candidates is flagged and never matched, but its edges still count against its
  neighbours, so it cannot make them look unique. Then the ambiguity policy decides:
  ``unique_only`` takes a pair when each item is the other's only candidate; ``best_unique`` when each is
  the other's strictly best (lowest amount difference); ``greedy_total_order`` scans the pairs in
  (difference, left id, right id) order and takes each pair whose items are both free, flagging it
  ambiguous when another free pair with the same difference touched either item (an ambiguous automatic
  pair becomes a suggestion unless the rule sets ``allow_ambiguous_auto``). An ambiguous ``1:1`` pool under
  ``unique_only`` gets no suggestion.
- **``1:M`` and ``M:1``.** For each one-side item, the many-side candidates (pairwise conditions passed)
  are grouped by ``group_by``, and each group is searched (``subset.solve``). More than
  ``max_candidates_per_item`` candidate groups flags the item. A many-side item that appears in the
  solutions of two one-side items is contested, and neither gets a group from this rule. Otherwise one
  solution is a group with the rule's outcome, and two or more are a suggestion of the lexicographically
  smallest, flagged ambiguous. Only ``unique_only`` is implemented for these kinds so far.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from close_contracts.config.match_set import Ambiguity, Outcome, PassRule, RuleKind
from close_engine.reference.conditions import (
    Calendars,
    Compiled,
    Totals,
    group_key,
    left_key,
    pair_ok,
    right_key,
    score,
    totals_ok,
    values,
)
from close_engine.reference.subset import TOO_MANY, solve

AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class Member:
    item_id: str
    role: str
    source_id: str
    source_key: str
    entity_id: str
    row: dict[str, Any]  # the item's fields prefixed with its role


@dataclass(frozen=True)
class Proposal:
    item_ids: tuple[str, ...]  # sorted
    automatic: bool
    ambiguous: bool = False


Flag = Callable[[str, str], None]


@dataclass(frozen=True)
class Pass:
    """One pass rule being run: its compiled conditions, the calendars and where flags are raised."""

    c: Compiled
    rule: PassRule
    calendars: Calendars
    flag: Flag


def _blocks(
    c: Compiled, left: Sequence[Member], right: Sequence[Member], calendars: Calendars
) -> Iterator[tuple[list[Member], list[Member]]]:
    if not c.keys:
        yield list(left), list(right)
        return
    blocks: dict[tuple[Any, ...], tuple[list[Member], list[Member]]] = defaultdict(lambda: ([], []))
    for m in left:
        key = left_key(c, m.row, calendars)
        if key is not None:
            blocks[key][0].append(m)
    for m in right:
        key = right_key(c, m.row, calendars)
        if key is not None:
            blocks[key][1].append(m)
    for xs, ys in blocks.values():
        if xs and ys:
            yield xs, ys


def _over_block_limit(c: Compiled, rule: PassRule, xs: list[Member], ys: list[Member], flag: Flag) -> bool:
    if c.keys and len(xs) * len(ys) > rule.limits.max_block_pairs:
        for m in (*xs, *ys):
            flag(m.item_id, TOO_MANY)
        return True
    return False


@dataclass(frozen=True, order=True)
class _Edge:
    score: int
    x: str
    y: str


def one_to_one(p: Pass, left: Sequence[Member], right: Sequence[Member]) -> list[Proposal]:
    c, rule, calendars, flag = p.c, p.rule, p.calendars, p.flag
    edges: list[_Edge] = []
    for xs, ys in _blocks(c, left, right, calendars):
        if _over_block_limit(c, rule, xs, ys, flag):
            continue
        right_values = {y.item_id: values(c, "right", y.row, calendars) for y in ys}
        for x in xs:
            vx = values(c, "left", x.row, calendars)
            if vx is None:
                continue
            for y in ys:
                vy = right_values[y.item_id]
                if vy is not None and pair_ok(c, x.row | y.row, calendars) and totals_ok(c, vx, vy):
                    edges.append(_Edge(score(c, vx, vy), x.item_id, y.item_id))
    by_x: dict[str, list[_Edge]] = defaultdict(list)
    by_y: dict[str, list[_Edge]] = defaultdict(list)
    for e in edges:
        by_x[e.x].append(e)
        by_y[e.y].append(e)
    limit = rule.limits.max_candidates_per_item
    blocked = {i for i, es in (*by_x.items(), *by_y.items()) if len(es) > limit}
    for i in sorted(blocked):
        flag(i, TOO_MANY)
    return _assign(p.rule, sorted(edges), by_x, by_y, blocked)


def _strict_best(edges: list[_Edge], edge: _Edge) -> bool:
    best = min(e.score for e in edges)
    return edge.score == best and sum(1 for e in edges if e.score == best) == 1


def _assign(
    rule: PassRule,
    edges: list[_Edge],
    by_x: dict[str, list[_Edge]],
    by_y: dict[str, list[_Edge]],
    blocked: set[str],
) -> list[Proposal]:
    automatic = rule.outcome == Outcome.AUTO
    if rule.ambiguity == Ambiguity.UNIQUE_ONLY:
        return [
            Proposal(tuple(sorted((e.x, e.y))), automatic)
            for e in edges
            if len(by_x[e.x]) == 1 and len(by_y[e.y]) == 1 and not {e.x, e.y} & blocked
        ]
    if rule.ambiguity == Ambiguity.BEST_UNIQUE:
        return [
            Proposal(tuple(sorted((e.x, e.y))), automatic)
            for e in edges
            if _strict_best(by_x[e.x], e) and _strict_best(by_y[e.y], e) and not {e.x, e.y} & blocked
        ]
    out = []
    taken: set[str] = set()
    for e in edges:  # greedy_total_order
        if {e.x, e.y} & (taken | blocked):
            continue
        rivals = [o for o in (*by_x[e.x], *by_y[e.y]) if o != e and not {o.x, o.y} & (taken | blocked)]
        tie = any(o.score == e.score for o in rivals)
        taken |= {e.x, e.y}
        out.append(Proposal(tuple(sorted((e.x, e.y))), automatic and (not tie or rule.allow_ambiguous_auto), tie))
    return out


def _search(
    p: Pass, x: Member, many: Sequence[Member], many_values: dict[str, Totals | None], one_left: bool
) -> tuple[list[tuple[str, ...]], set[str], str | None]:
    """One one-side item's solutions, its candidate many-side items, and a flag when it cannot be decided."""
    c = p.c
    vx = values(c, "left" if one_left else "right", x.row, p.calendars)
    if vx is None:
        return [], set(), None

    def balanced(one: Totals, many_total: Totals) -> bool:
        return totals_ok(c, one, many_total) if one_left else totals_ok(c, many_total, one)

    groups: dict[tuple[Any, ...], list[tuple[str, Totals]]] = defaultdict(list)
    for y in many:
        vy = many_values[y.item_id]
        if vy is None or not pair_ok(c, x.row | y.row, p.calendars):
            continue
        g = group_key(c, y.row, p.calendars)
        if g is not None:
            groups[g].append((y.item_id, vy))
    candidates = {i for members in groups.values() for i, _ in members}
    if len(groups) > p.rule.limits.max_candidates_per_item:
        return [], candidates, TOO_MANY
    found: list[tuple[str, ...]] = []
    for members in groups.values():
        search = solve(c, vx, sorted(members), p.rule.limits, balanced)
        if search.flag is not None:
            return [], candidates, search.flag
        found.extend(search.solutions)
    return found, candidates, None


def aggregate(p: Pass, left: Sequence[Member], right: Sequence[Member]) -> list[Proposal]:
    if p.rule.ambiguity != Ambiguity.UNIQUE_ONLY:
        raise NotImplementedError("1:M and M:1 rules support unique_only in the reference matcher so far")
    one_left = p.c.kind == RuleKind.ONE_TO_MANY
    many_side = "right" if one_left else "left"
    solutions: dict[str, list[tuple[str, ...]]] = {}
    owners: dict[str, set[str]] = defaultdict(set)
    pairs = _blocks(p.c, left, right, p.calendars)
    for ones, many in pairs if one_left else ((ys, xs) for xs, ys in pairs):
        if _over_block_limit(p.c, p.rule, ones, many, p.flag):
            continue
        many_values = {y.item_id: values(p.c, many_side, y.row, p.calendars) for y in many}
        for x in ones:
            found, candidates, flag = _search(p, x, many, many_values, one_left)
            if flag is not None:
                p.flag(x.item_id, flag)
                for i in candidates:  # an undecided item still stands in the way of its candidates
                    owners[i].add(x.item_id)
                continue
            for i in {i for s in found for i in s}:
                owners[i].add(x.item_id)
            solutions[x.item_id] = found
    automatic = p.rule.outcome == Outcome.AUTO
    out = []
    for x_id in sorted(solutions):
        found = solutions[x_id]
        if not found:
            continue
        if any(len(owners[i]) > 1 for s in found for i in s):
            for i in sorted({x_id} | {i for s in found for i in s}):
                p.flag(i, AMBIGUOUS)
            continue
        if len(found) == 1:
            out.append(Proposal(tuple(sorted((x_id, *found[0]))), automatic))
        else:
            out.append(Proposal(tuple(sorted((x_id, *min(found)))), automatic=False, ambiguous=True))
    return out
