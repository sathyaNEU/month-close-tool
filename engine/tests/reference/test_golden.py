"""Golden tests: the Stage 3 templates on generated data, scored against the answer key.

Every family must have 0 false automatic matches, full recall and every expected outcome reached, on tiny
for the gate's three seeds and on small for seed 11 (the gate itself adds small 23 and 47 and medium 11).
The decisions on tiny seed 11 are pinned by digest in ``golden/tiny-11.json``; after an intended change,
rerun with ``CLOSE_UPDATE_GOLDEN=1`` and review the diff.
"""

from __future__ import annotations

import json
import os
import random
from functools import cache
from pathlib import Path

import pytest

from close_contracts.templates import BY_ID, FAMILIES
from close_datagen.generate import generate
from close_datagen.landing import Builder
from close_datagen.plan import build_plan
from close_datagen.profiles import PROFILES
from close_datagen.settlement import carrier_flows
from close_datagen.world import CALENDARS
from close_engine.reference.items import Item
from close_engine.reference.matcher import Result, run
from close_engine.reference.runner import digest, normalize_all, run_all, score_all
from close_engine.reference.score import score_family

GOLDEN = Path(__file__).parent / "golden" / "tiny-11.json"


@cache
def _data(profile: str, seed: int) -> tuple[Builder, dict[str, list[Item]]]:
    b = generate(seed, PROFILES[profile], f"golden-{seed}")
    return b, normalize_all(b.rows, CALENDARS)


@cache
def _results(profile: str, seed: int) -> dict[str, Result]:
    return run_all(_data(profile, seed)[1], CALENDARS, "golden")


@pytest.mark.parametrize(("profile", "seed"), [("tiny", 11), ("tiny", 23), ("tiny", 47), ("small", 11)])
def test_every_template_family_scores_perfectly(profile: str, seed: int) -> None:
    b, _ = _data(profile, seed)
    for score in score_all(_results(profile, seed), b.group_rows(), b.member_rows()):
        assert score.false_auto == 0, (score.family, score.examples)
        assert score.recall == 1.0, (score.family, score.examples)
        assert score.all_reached, (score.family, score.examples)
        assert all(reached == total for reached, total in score.breaks.values()), (score.family, score.breaks)


def test_small_exercises_every_break_the_templates_cover() -> None:
    b, _ = _data("small", 11)
    covered = set()
    for score in score_all(_results("small", 11), b.group_rows(), b.member_rows()):
        covered |= {name for name, (_, total) in score.breaks.items() if total >= 10}
    assert covered == {
        "carrier_correction",
        "duplicate",
        "fx_difference",
        "partial_capture",
        "rounding",
        "split_tender",
        "timing",
        "unclaimed_payout",
    }


def test_input_order_does_not_change_any_decision() -> None:
    _, items = _data("tiny", 11)
    shuffled = {}
    for source_id, source_items in items.items():
        copy = list(source_items)
        random.Random(source_id).shuffle(copy)  # noqa: S311 - a seeded test shuffle
        shuffled[source_id] = copy
    again = run_all(shuffled, CALENDARS, "golden")
    for match_set_id, result in _results("tiny", 11).items():
        assert digest(again[match_set_id]) == digest(result), match_set_id


def test_tiny_decisions_match_the_golden_digests() -> None:
    digests = {m: digest(r) for m, r in sorted(_results("tiny", 11).items())}
    if os.environ.get("CLOSE_UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(digests, indent=2) + "\n")
    assert json.loads(GOLDEN.read_text()) == digests


def test_a_carrier_correction_equal_to_the_quote_is_expected_and_made_as_a_suggestion() -> None:
    """No seed happens to bill a correction equal to the label's quote, so the tie is built here."""
    source, _ = _data("tiny", 11)
    label = source.rows["landing.shipping_labels"][0]
    orders, _ = build_plan(11, PROFILES["tiny"])
    order = next(o for o in orders if o.order_id == label["order_id"])
    order.breaks["carrier_correction"] = ("weight_adjustment", int(label["quoted_minor"]))
    b = Builder(11, "tie")
    b.rows["landing.shipping_labels"] = [label]
    carrier_flows(b, [order])
    groups, members = list(b.group_rows()), list(b.member_rows())
    assert [(g["expected_outcome"], g["break_type"]) for g in groups] == [("suggested", "carrier_correction")]
    assert len(members) == 3
    items = normalize_all(b.rows, CALENDARS)
    result = run(BY_ID["t9_carrier_invoices"], items, run_id="tie", calendars=CALENDARS)
    [family] = [f for f in FAMILIES if f.template == 9]
    score = score_family(family, [result], groups, members)
    assert score.false_auto == 0
    assert score.reached == {"suggested": 1}
    assert [g.ambiguous for g in result.groups] == [True]
