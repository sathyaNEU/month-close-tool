"""The reference matcher: one match set's waterfall of pass rules over normalized items, in pure Python.

It is the oracle the Spark matcher is held to (Gate 3: engine groups exactly equal reference groups), so it
favours plain, deterministic code over speed. The semantics are in ADR-0013; in short:

- **Membership.** An item belongs to the match set when its entity is one of the set's entities and its
  source is bound to a role whose scope (a DSL expression over the role's prefixed fields) holds. An item
  in the scope of two roles is a configuration error.
- **Pool.** A rule's pool is the items on its sides without an active group. Items with a pending
  suggestion are in the pool of automatic rules only. In a ``matching`` set each rule runs once per entity,
  so a group never mixes entities; an ``intercompany`` set runs each rule over all its entities at once.
- **Apply.** A rule decides all its groups from the pool as it stood when it started (``kinds``), then the
  groups are applied in item-id order. An automatic group supersedes a pending suggestion of any of its
  items, and that suggestion's other items return to the pool for the rules that follow. A suggestion is
  only made from items that are neither matched nor already suggested.
- **Timing.** After the pass rules, each timing rule classifies the open items of its role that satisfy its
  condition as in transit while ``as_of`` is on or before the transaction date plus the expected clearing
  business days. Without ``as_of`` no timing rule runs.
- **Identity.** A group id hashes the match set, the run, the rule and the sorted item ids.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from close_contracts.config.match_set import MatchSet, Outcome, PassRule, RuleKind
from close_contracts.dsl import DslType, evaluate, typecheck
from close_contracts.ids import hash_id
from close_engine.reference.conditions import Calendars, compile_rule, schema_for, view
from close_engine.reference.items import Item
from close_engine.reference.kinds import Member, Pass, Proposal, aggregate, one_to_one


@dataclass
class Group:
    group_id: str
    rule_id: str
    match_type: str  # automatic or suggested
    status: str  # active, pending or superseded
    item_ids: tuple[str, ...]
    ambiguous: bool = False


@dataclass
class Result:
    match_set_id: str
    run_id: str
    members: dict[str, Member]
    groups: list[Group] = field(default_factory=list)
    flags: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    expected_clear: dict[str, date] = field(default_factory=dict)  # in-transit items
    metrics: dict[str, dict[str, int]] = field(default_factory=dict)
    status: dict[str, str] = field(default_factory=dict)  # open, matched, suggested or in_transit


def members_of(match_set: MatchSet, items: Mapping[str, Sequence[Item]], calendars: Calendars) -> dict[str, Member]:
    out: dict[str, Member] = {}
    entities = set(match_set.entity_ids)
    for binding in match_set.sources:
        role = binding.role.value
        scope = None
        if binding.scope is not None:
            scope = typecheck(binding.scope, schema_for(role), expect=DslType.BOOLEAN)
        for item in items.get(binding.source_id, ()):
            if item.fields.get("entity_id") not in entities:
                continue
            row = view(role, item.fields)
            if scope is not None and evaluate(scope, row, calendars=calendars) is not True:
                continue
            if item.item_id in out:
                raise ValueError(f"item {item.item_id} is in the scope of two roles of {match_set.match_set_id}")
            entity = str(item.fields["entity_id"])
            out[item.item_id] = Member(item.item_id, role, item.source_id, item.source_key, entity, row)
    return out


class _Waterfall:
    def __init__(self, match_set: MatchSet, run_id: str, members: dict[str, Member]) -> None:
        self.result = Result(match_set.match_set_id, run_id, members)
        self.by_role: dict[str, list[Member]] = defaultdict(list)
        for m in sorted(members.values(), key=lambda m: m.item_id):
            self.by_role[m.role].append(m)
        self.active: dict[str, str] = {}
        self.pending: dict[str, str] = {}
        self.groups: dict[str, Group] = {}

    def flag(self, item_id: str, name: str) -> None:
        self.result.flags[item_id].add(name)

    def pool(self, role: str, automatic: bool, entity: str | None) -> list[Member]:
        return [
            m
            for m in self.by_role[role]
            if m.item_id not in self.active
            and (automatic or m.item_id not in self.pending)
            and (entity is None or m.entity_id == entity)
        ]

    def apply(self, rule: PassRule, proposals: list[Proposal]) -> None:
        counts: Counter[str] = Counter()
        for p in sorted(proposals, key=lambda p: p.item_ids):
            gid = hash_id("group", self.result.match_set_id, self.result.run_id, rule.rule_id, *p.item_ids)
            if p.automatic:
                for item in p.item_ids:
                    if item in self.pending:
                        self._supersede(self.pending[item])
                        counts["superseded"] += 1
                self.groups[gid] = Group(gid, rule.rule_id, "automatic", "active", p.item_ids, p.ambiguous)
                self.active.update(dict.fromkeys(p.item_ids, gid))
                counts["automatic"] += 1
            elif any(i in self.pending or i in self.active for i in p.item_ids):
                counts["not_suggested_already_held"] += 1
            else:
                self.groups[gid] = Group(gid, rule.rule_id, "suggested", "pending", p.item_ids, p.ambiguous)
                self.pending.update(dict.fromkeys(p.item_ids, gid))
                counts["suggested"] += 1
            counts["ambiguous"] += p.ambiguous
        self.result.metrics[rule.rule_id] = dict(counts)

    def _supersede(self, gid: str) -> None:
        group = self.groups[gid]
        group.status = "superseded"
        for item in group.item_ids:
            self.pending.pop(item, None)

    def timing(self, match_set: MatchSet, calendars: Calendars, as_of: date) -> None:
        for rule in match_set.timing_rules:
            role = rule.role.value
            when = typecheck(rule.when, schema_for(role), expect=DslType.BOOLEAN)
            calendar = calendars[rule.calendar]
            count = 0
            for m in self.by_role[role]:
                i = m.item_id
                if i in self.active or i in self.pending or i in self.result.expected_clear:
                    continue
                if evaluate(when, m.row, calendars=calendars) is not True:
                    continue
                expected = calendar.add_business_days(
                    m.row[f"{role}.transaction_date"], rule.expected_clear_business_days
                )
                if as_of <= expected:
                    self.result.expected_clear[i] = expected
                    count += 1
            self.result.metrics[rule.rule_id] = {"in_transit": count}


def run(
    match_set: MatchSet,
    items: Mapping[str, Sequence[Item]],
    *,
    run_id: str,
    calendars: Calendars,
    as_of: date | None = None,
) -> Result:
    """Run the match set's waterfall once over ``items`` (keyed by data source id), from an empty state."""
    w = _Waterfall(match_set, run_id, members_of(match_set, items, calendars))
    # A matching set never groups items of different entities; an intercompany set exists to do exactly that.
    partitions: tuple[str | None, ...] = (
        (None,) if match_set.kind == "intercompany" else tuple(sorted(match_set.entity_ids))
    )
    for rule in match_set.pass_rules:
        c = compile_rule(rule)
        automatic = rule.outcome == Outcome.AUTO
        if rule.kind == RuleKind.ONE_TO_ONE:
            kind = one_to_one
        elif rule.kind in {RuleKind.ONE_TO_MANY, RuleKind.MANY_TO_ONE}:
            kind = aggregate
        else:
            raise NotImplementedError(f"{rule.kind} rules are not in the reference matcher yet")
        p = Pass(c, rule, calendars, w.flag)
        proposals = []
        for entity in partitions:
            left, right = w.pool(c.roles[0], automatic, entity), w.pool(c.roles[1], automatic, entity)
            proposals += kind(p, left, right)
        w.apply(rule, proposals)
    if as_of is not None:
        w.timing(match_set, calendars, as_of)
    w.result.groups = sorted(w.groups.values(), key=lambda g: g.group_id)
    for i in w.result.members:
        in_transit = "in_transit" if i in w.result.expected_clear else "open"
        w.result.status[i] = "matched" if i in w.active else ("suggested" if i in w.pending else in_transit)
    return w.result
