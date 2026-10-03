"""Running the Stage 3 templates end to end in the reference matcher: normalize every default data source,
run every template match set, score each family against the answer key, and digest the outcome."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from close_contracts.binding import bind
from close_contracts.canonical_json import canonical_json
from close_contracts.default_sources import DEFAULT_SOURCES
from close_contracts.templates import FAMILIES, MATCH_SETS
from close_engine.reference.conditions import Calendars
from close_engine.reference.items import Item, normalize
from close_engine.reference.matcher import Result, run
from close_engine.reference.score import TABLE_OF, FamilyScore, score_family


def normalize_all(rows: Mapping[str, Sequence[Mapping[str, Any]]], calendars: Calendars) -> dict[str, list[Item]]:
    """Items by data source id; any rejected row is an error here (the generated data must all map)."""
    out = {}
    for source in DEFAULT_SOURCES:
        normalized = normalize(bind(source), rows.get(source.landing_table, []), calendars)
        if normalized.rejected:
            raise ValueError(f"{source.source_id} rejected {normalized.rejected[:3]}")
        out[source.source_id] = normalized.items
    return out


def run_all(items: Mapping[str, Sequence[Item]], calendars: Calendars, run_id: str) -> dict[str, Result]:
    return {m.match_set_id: run(m, items, run_id=run_id, calendars=calendars) for m in MATCH_SETS}


def score_all(
    results: Mapping[str, Result], groups: Iterable[Mapping[str, Any]], members: Iterable[Mapping[str, Any]]
) -> list[FamilyScore]:
    group_list, member_list = list(groups), list(members)
    return [score_family(f, [results[m] for m in f.match_sets], group_list, member_list) for f in FAMILIES]


def digest(result: Result) -> str:
    """A stable digest of what the match set decided, by source identity (independent of the run id)."""

    def ident(item_id: str) -> list[str]:
        m = result.members[item_id]
        return [TABLE_OF.get(m.source_id, m.source_id), m.source_key]

    groups = sorted(
        [g.rule_id, g.match_type, g.status, g.ambiguous, sorted(ident(i) for i in g.item_ids)] for g in result.groups
    )
    flags = sorted([ident(i), sorted(f)] for i, f in result.flags.items() if f)
    statuses = sorted([ident(i), s] for i, s in result.status.items())
    payload: list[Any] = [groups, flags, statuses]
    return hashlib.sha256(canonical_json(payload)).hexdigest()
