"""The Stage 3 template match sets: valid, wired to real default sources, and scored by exactly one family."""

from __future__ import annotations

from close_contracts.config.match_set import MatchSet
from close_contracts.default_sources import BY_ID as SOURCES
from close_contracts.templates import BY_ID, FAMILIES, MATCH_SETS, Family


def test_every_template_is_a_valid_match_set_over_default_sources() -> None:
    assert len(BY_ID) == len(MATCH_SETS) == 12
    for m in MATCH_SETS:
        assert MatchSet.model_validate(m.model_dump(mode="json")) == m
        assert {b.source_id for b in m.sources} <= set(SOURCES), m.match_set_id


def test_every_match_set_is_scored_by_exactly_one_family() -> None:
    scored = [m for f in FAMILIES for m in f.match_sets]
    assert sorted(scored) == sorted(BY_ID)
    assert {f.template for f in FAMILIES} == {1, 2, 3, 6, 9, 10}


def test_template_10_splits_its_answer_key_between_two_families() -> None:
    orders, remittances = (f for f in FAMILIES if f.template == 10)
    remittance, order = "T10-REM-US01-CA-USD-202607", "T10-O123"
    assert orders.selects(order)
    assert not orders.selects(remittance)
    assert remittances.selects(remittance)
    assert not remittances.selects(order)
    assert Family("x", 1, ()).selects("anything")
