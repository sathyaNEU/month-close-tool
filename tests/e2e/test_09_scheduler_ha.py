"""Two app instances: every claimed tick runs on exactly one of them, and when one is killed or frozen
the other keeps the cadence within one interval plus one second (Gate 1a). Evidence comes from each
instance's own log line per claimed tick (close_platform.scheduler at DEBUG in compose.e2e.yaml)."""

from __future__ import annotations

import itertools
import json
import time
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from stacklib import compose, scalar

pytestmark = pytest.mark.stack

INTERVAL_S = 1.0  # outbox-relay and run-poller in the e2e stack
FAST_TASKS = ("outbox-relay", "run-poller")  # the tick counts are measured on these; slower tasks tick less often
INSTANCES = {"app1": "app", "app2": "app2"}


@pytest.fixture
def two_instances() -> Iterator[None]:
    compose("--profile", "ha", "up", "-d", "--wait", "--no-deps", "app2")
    yield
    compose("unpause", "app", "app2", check=False)
    compose("up", "-d", "--wait", "--no-deps", "app")
    compose("--profile", "ha", "stop", "app2")


def _ticks(since: str) -> list[dict[str, object]]:
    out = compose("--profile", "ha", "logs", "--no-log-prefix", "--since", since, "app", "app2").stdout
    ticks = []
    for line in out.splitlines():
        if '"msg": "tick"' in line:
            record = json.loads(line)
            record["at"] = datetime.fromisoformat(str(record["ts"])).timestamp()
            ticks.append(record)
    return ticks


def _check(ticks: list[dict[str, object]], min_ticks: int) -> dict[str, list[float]]:
    by_task: dict[str, list[dict[str, object]]] = defaultdict(list)
    for t in ticks:
        by_task[str(t["task"])].append(t)
    gaps: dict[str, list[float]] = {}
    for task, items in by_task.items():
        seqs = [int(str(t["claim_seq"])) for t in items]
        assert len(seqs) == len(set(seqs)), f"{task}: a tick ran on two instances"
        ordered = sorted(items, key=lambda t: int(str(t["claim_seq"])))
        span = sorted(seqs)
        assert span == list(range(span[0], span[0] + len(span))), f"{task}: claim_seq has holes"
        if task in FAST_TASKS:
            assert len(span) >= min_ticks, f"{task}: only {len(span)} ticks"
        gaps[task] = [float(str(b["at"])) - float(str(a["at"])) for a, b in itertools.pairwise(ordered)]
    assert {str(t["instance"]) for t in ticks} == set(INSTANCES), "both instances should have claimed ticks"
    assert set(FAST_TASKS) <= set(by_task), "the fast tasks should have ticked"
    return gaps


def _latest_claimer(task: str = "outbox-relay") -> str:
    return INSTANCES[scalar(f"SELECT last_claimed_by FROM scheduler_tasks WHERE task = '{task}'")]


def test_the_survivor_keeps_the_cadence_after_a_kill_and_a_pause(two_instances: None) -> None:
    since = datetime.now(tz=UTC).isoformat()
    time.sleep(4)
    victim = _latest_claimer()
    compose("kill", "-s", "SIGKILL", victim)
    time.sleep(5)
    compose("--profile", "ha", "up", "-d", "--wait", "--no-deps", victim)
    time.sleep(3)
    frozen = _latest_claimer()
    compose("pause", frozen)
    time.sleep(5)
    compose("unpause", frozen)
    time.sleep(3)
    gaps = _check(_ticks(since), min_ticks=15)
    worst = max(gaps["outbox-relay"])
    print(f"worst outbox-relay gap across the kill of {victim} and the pause of {frozen}: {worst:.2f}s")
    assert worst <= INTERVAL_S + 1.0


@pytest.mark.slow
def test_a_hundred_intervals_with_two_instances_run_each_tick_exactly_once(two_instances: None) -> None:
    since = datetime.now(tz=UTC).isoformat()
    time.sleep(112)  # a 1 s interval runs at about 1.02 to 1.05 s (next due = claim time + interval)
    gaps = _check(_ticks(since), min_ticks=100)
    print({task: f"{len(g) + 1} ticks, worst gap {max(g):.2f}s" for task, g in gaps.items()})
    assert max(gaps["outbox-relay"]) <= INTERVAL_S + 1.0
