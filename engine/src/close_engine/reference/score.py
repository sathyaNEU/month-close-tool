"""Scoring matcher results against the generator's answer key (the Gate 3 correctness criteria).

Items are compared by source identity, (landing table, source key), so the score does not depend on how a
matcher names its items. For one template family (``close_contracts.templates.FAMILIES``):

- an expected **automatic** group is reached when the matcher made exactly that group automatically;
- an expected **suggested** group is reached when none of its items was matched automatically and its
  one-side items (the side with the fewest members) are in pending suggestions. The suggestion's other
  members are not compared: a tie between identical items (a duplicate) has no single right answer;
- an expected **open** group is reached when none of its items was matched automatically;
- a **false automatic match** is any automatic group that is not exactly an expected automatic group,
  including groups over items the answer key does not mention;
- **clean recall** is the share of expected automatic groups without a break that were reached.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from close_contracts.default_sources import DEFAULT_SOURCES
from close_contracts.templates import Family
from close_engine.reference.matcher import Result

Ident = tuple[str, str]  # (landing table, source key)
TABLE_OF = {d.source_id: d.landing_table for d in DEFAULT_SOURCES}


@dataclass
class FamilyScore:
    family: str
    expected: Counter[str] = field(default_factory=Counter)  # outcome -> groups
    reached: Counter[str] = field(default_factory=Counter)
    clean_auto: int = 0
    clean_auto_reached: int = 0
    false_auto: int = 0
    breaks: dict[str, list[int]] = field(default_factory=dict)  # break type -> [reached, total]
    examples: list[str] = field(default_factory=list)  # unreached groups and false matches, a few of each

    @property
    def recall(self) -> float:
        if not self.clean_auto:
            return 1.0
        return self.clean_auto_reached / self.clean_auto  # spark-ban: allow SB008 a reported ratio

    @property
    def all_reached(self) -> bool:
        return sum(self.reached.values()) == sum(self.expected.values())


def _engine_view(results: Sequence[Result]) -> tuple[set[frozenset[Ident]], set[Ident], set[Ident]]:
    auto_sets: set[frozenset[Ident]] = set()
    auto_items: set[Ident] = set()
    suggested: set[Ident] = set()
    for r in results:
        for g in r.groups:
            ids = frozenset((TABLE_OF[r.members[i].source_id], r.members[i].source_key) for i in g.item_ids)
            if g.status == "active":
                auto_sets.add(ids)
                auto_items |= ids
            elif g.status == "pending":
                suggested |= ids
    return auto_sets, auto_items, suggested


def _anchor(members: Sequence[Mapping[str, Any]]) -> set[Ident]:
    sides: dict[str, set[Ident]] = defaultdict(set)
    for m in members:
        sides[m["side"]].add((m["source_table"], m["source_key"]))
    side = min(sides, key=lambda k: (len(sides[k]), k))
    return sides[side]


def score_family(
    family: Family,
    results: Sequence[Result],
    groups: Iterable[Mapping[str, Any]],
    members: Iterable[Mapping[str, Any]],
) -> FamilyScore:
    auto_sets, auto_items, suggested = _engine_view(results)
    chosen = [g for g in groups if g["template"] == family.template and family.selects(str(g["group_key"]))]
    keys = {str(g["group_key"]) for g in chosen}
    by_group: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for m in members:
        if m["template"] == family.template and str(m["group_key"]) in keys:
            by_group[str(m["group_key"])].append(m)
    out = FamilyScore(family.name)
    expected_auto: set[frozenset[Ident]] = set()
    for g in chosen:
        key, outcome, break_type = str(g["group_key"]), str(g["expected_outcome"]), g["break_type"]
        group_members = by_group[key]
        idents = frozenset((m["source_table"], m["source_key"]) for m in group_members)
        if outcome == "auto":
            expected_auto.add(idents)
            reached = idents in auto_sets
        elif outcome == "suggested":
            reached = not idents & auto_items and _anchor(group_members) <= suggested
        else:
            reached = not idents & auto_items
        out.expected[outcome] += 1
        out.reached[outcome] += reached
        if outcome == "auto" and break_type is None:
            out.clean_auto += 1
            out.clean_auto_reached += reached
        if break_type is not None:
            tally = out.breaks.setdefault(str(break_type), [0, 0])
            tally[0] += reached
            tally[1] += 1
        if not reached and len(out.examples) < 10:
            out.examples.append(f"{key} expected {outcome} ({break_type or 'no break'})")
    false_matches = sorted(sorted(s) for s in auto_sets - expected_auto)
    out.false_auto = len(false_matches)
    out.examples += [f"false automatic match {s}" for s in false_matches[:5]]
    return out
