"""Journaling the generated marketplace with the SAMPLE configuration (Gate 6).

- **Engine equals oracle.** The engine books what the independent oracle books, in transaction and functional
  amounts, under the sample policy, and under every combination of the policy switches.
- **The guards.** Flipping any switch changes the books on the test data, the engine follows each flip, and the
  data has every event type in every program.
- **The balances.** Every ledger balance ties to its source of truth at several cutoffs.
- **The golden digest.** The sample books on ``tiny`` seed 11 are pinned by digest; after an intended change,
  rerun with ``CLOSE_UPDATE_GOLDEN=1`` and review.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import replace
from datetime import date
from functools import cache
from pathlib import Path

from close_contracts.samples.journaling import PROGRAMS, SAMPLE_POLICY
from close_datagen.generate import generate
from close_datagen.landing import Builder
from close_datagen.profiles import PROFILES
from close_datagen.world import CALENDARS
from close_engine.reference.journal.events import Derived, derive
from close_engine.reference.journal.invariants import as_of, check_all
from close_engine.reference.journal.ledger import digest, run
from close_engine.reference.journal.matrix import flips, presentation_matrix, program_matrix
from close_engine.reference.journal.oracle import engine_nets, oracle_nets
from close_engine.reference.journal.posting import JournalConfig, ReferenceData
from close_engine.reference.journal.runner import reference_from, sample_config

GOLDEN = Path(__file__).parent.parent / "golden" / "journal-tiny-11.json"


@cache
def _world(profile: str) -> tuple[Builder, JournalConfig, ReferenceData, Derived]:
    b = generate(11, PROFILES[profile], f"journal-{profile}")
    config = sample_config()
    return b, config, reference_from(b.rows), derive(list(config.event_types.values()), b.rows, CALENDARS)


def test_every_source_row_is_accounted_for() -> None:
    _, _, _, derived = _world("small")
    for label, c in derived.control.items():
        assert c["read"] == c["events"] + c["excluded"] + c["rejected"], label
    assert derived.rejected == []


def test_the_engine_books_what_the_oracle_books() -> None:
    for profile in ("tiny", "small"):
        _, config, ref, derived = _world(profile)
        result = run(config, derived.events, ref=ref)
        assert result.failed_types == {}
        assert engine_nets(result.lines) == oracle_nets(derived.events, SAMPLE_POLICY, ref), profile


def test_the_engine_equals_the_oracle_for_every_policy_combination() -> None:
    _, config, ref, derived = _world("tiny")
    policies = program_matrix(SAMPLE_POLICY) + presentation_matrix(SAMPLE_POLICY)
    assert len(policies) == 212
    mismatched = []
    for label, policy in policies:
        result = run(replace(config, policy=policy), derived.events, ref=ref)
        if result.failed_types or engine_nets(result.lines) != oracle_nets(derived.events, policy, ref):
            mismatched.append(label)
    assert mismatched == []


def test_flipping_any_switch_changes_the_books_and_the_engine_follows() -> None:
    """On ``small`` each flip changes the oracle's books, and the engine books the same under the flipped policy.
    (On ``tiny`` some flips change nothing, so the matrix there cannot prove those switch values alone.)"""
    _, config, ref, derived = _world("small")
    base = oracle_nets(derived.events, SAMPLE_POLICY, ref)
    unchanged, mismatched = [], []
    for label, policy in flips(SAMPLE_POLICY):
        expected = oracle_nets(derived.events, policy, ref)
        if expected == base:
            unchanged.append(label)
        result = run(replace(config, policy=policy), derived.events, ref=ref)
        if result.failed_types or engine_nets(result.lines) != expected:
            mismatched.append(label)
    assert (unchanged, mismatched) == ([], [])


def test_the_data_has_every_event_type_in_every_program() -> None:
    _, config, _, derived = _world("small")
    seen = Counter((e.event_type, e.fields.get("program")) for e in derived.events)
    for event_type in ("ORDER_CAPTURED", "ORDER_STAGE", "ORDER_REFUND"):
        for program in PROGRAMS:
            assert seen[(event_type, program)] > 0, (event_type, program)
    assert seen[("SELLER_CHARGE", "verified_marketplace")] > 0
    assert {t for t, _ in seen} == set(config.event_types)


def test_every_balance_ties_to_its_source_of_truth_at_each_cutoff() -> None:
    b, config, _, _ = _world("small")
    for cutoff in (date(2026, 7, 8), date(2026, 7, 20), date(2026, 8, 1), date(2026, 12, 31)):
        snapshot = as_of(b.rows, cutoff)
        events = derive(list(config.event_types.values()), snapshot, CALENDARS).events
        result = run(config, events, ref=reference_from(snapshot))
        checks = check_all(snapshot, result.lines, SAMPLE_POLICY)
        assert {name: detail for name, (ok, detail) in checks.items() if not ok} == {}, cutoff
        if cutoff < date(2026, 12, 31):  # mid-period every account has open balances, so no check compares nothing
            assert [name for name, (_, detail) in checks.items() if ": 0 balances tie" in detail] == [], cutoff


def test_the_sample_books_match_the_golden_digest() -> None:
    _, config, ref, derived = _world("tiny")
    books = {"lines": digest(run(config, derived.events, ref=ref).lines)}
    if os.environ.get("CLOSE_UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(books, indent=2) + "\n")
    assert json.loads(GOLDEN.read_text()) == books
