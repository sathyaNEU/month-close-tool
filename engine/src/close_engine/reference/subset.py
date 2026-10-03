"""The bounded subset search for ``1:M`` and ``M:1`` rules (roadmap "How one pass runs", kinds).

For one item on the one side and its candidate group on the many side, the whole group is tried first and
wins outright when it balances. Otherwise the search looks for every non-empty subset that balances:

- a group larger than ``subset_max_candidates`` is not searched (``too_many_candidates``);
- with an amount condition, a meet-in-the-middle search on the first amount operand: the group, sorted by
  item id, is split in two halves, every subset sum of each half is listed, and each left-half sum is paired
  with the right-half sums that land in the tolerance window; every pairing is checked against all the
  rule's amount and sign conditions;
- without one (only sign conditions), every subset is checked in bit-mask order.

Work counts the half-subsets listed plus the pairings (or the subsets) checked. Passing ``work_budget``
stops the search with ``search_limit_reached``, and no partial answer is used.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from close_contracts.config.match_set import Limits
from close_engine.reference.conditions import Compiled, Totals, add, allowed

TOO_MANY = "too_many_candidates"
SEARCH_LIMIT = "search_limit_reached"


@dataclass
class Search:
    solutions: list[tuple[str, ...]] = field(default_factory=list)  # sorted item ids of each balancing subset
    full_group: bool = False
    flag: str | None = None


def _zero(width: int) -> Totals:
    return (0,) * width


def _windows(c: Compiled, one: Totals) -> list[tuple[int, int]]:
    """Where the many side's first amount total may land for the first amount condition to hold."""
    condition = c.amounts[0][0]
    target = one[0]
    tol = allowed(condition, target)
    if "absolute" not in c.signs:
        return [(target - tol, target + tol)]
    magnitude = abs(target)
    return sorted({(magnitude - tol, magnitude + tol), (-magnitude - tol, -magnitude + tol)})


def _halves(members: Sequence[tuple[str, Totals]], width: int) -> list[tuple[Totals, int]]:
    out = []
    for mask in range(1 << len(members)):
        total = _zero(width)
        for i, (_, values) in enumerate(members):
            if mask >> i & 1:
                total = add(total, values)
        out.append((total, mask))
    return out


def solve(
    c: Compiled,
    one: Totals,
    members: Sequence[tuple[str, Totals]],
    limits: Limits,
    balanced: Callable[[Totals, Totals], bool],
) -> Search:
    """``members`` are (item id, totals) sorted by item id; ``balanced(one, many_total)`` applies the rule."""
    width = len(one)
    whole = _zero(width)
    for _, values in members:
        whole = add(whole, values)
    if balanced(one, whole):
        return Search([tuple(m[0] for m in members)], full_group=True)
    n = len(members)
    if n > limits.subset_max_candidates:
        return Search(flag=TOO_MANY)
    found: list[tuple[str, ...]] = []
    if not c.amounts:
        if (1 << n) - 1 > limits.work_budget:
            return Search(flag=SEARCH_LIMIT)
        for mask in range(1, 1 << n):
            chosen = [members[i] for i in range(n) if mask >> i & 1]
            total = _zero(width)
            for _, values in chosen:
                total = add(total, values)
            if balanced(one, total):
                found.append(tuple(m[0] for m in chosen))
        return Search(sorted(found))
    left, right = members[: n // 2], members[n // 2 :]
    work = (1 << len(left)) + (1 << len(right))
    if work > limits.work_budget:
        return Search(flag=SEARCH_LIMIT)
    left_sums = _halves(left, width)
    right_sums = sorted(_halves(right, width), key=lambda entry: (entry[0][0], entry[1]))
    keys = [entry[0][0] for entry in right_sums]
    for total_left, mask_left in left_sums:
        for lo, hi in _windows(c, one):
            start, stop = bisect_left(keys, lo - total_left[0]), bisect_right(keys, hi - total_left[0])
            work += stop - start
            if work > limits.work_budget:
                return Search(flag=SEARCH_LIMIT)
            for total_right, mask_right in right_sums[start:stop]:
                if not mask_left and not mask_right:
                    continue
                if balanced(one, add(total_left, total_right)):
                    ids = [left[i][0] for i in range(len(left)) if mask_left >> i & 1]
                    ids += [right[i][0] for i in range(len(right)) if mask_right >> i & 1]
                    found.append(tuple(sorted(ids)))
    return Search(sorted(set(found)))
