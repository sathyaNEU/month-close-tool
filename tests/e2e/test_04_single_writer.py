"""One writer: 20 parallel starts through the proxy give one 202 and nineteen 409s (Gate 1a)."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from stacklib import User, lease_token, scalar

pytestmark = pytest.mark.stack


def test_twenty_parallel_starts_one_wins_and_the_token_rises_by_one() -> None:
    before = lease_token()
    grants_before = int(scalar("SELECT count(*) FROM lease_events WHERE event = 'granted'"))
    clients = [User(("alice", "bob", "dave")[i % 3]) for i in range(20)]
    barrier = threading.Barrier(20)

    def go(i: int) -> httpx.Response:
        barrier.wait()
        return clients[i].start(key=f"parallel-{i:02d}-{before}")

    with ThreadPoolExecutor(max_workers=20) as pool:
        responses = list(pool.map(go, range(20)))
    assert sorted(r.status_code for r in responses) == [202] + [409] * 19
    winner = next(r.json() for r in responses if r.status_code == 202)
    assert {r.json()["holder_run_id"] for r in responses if r.status_code == 409} == {winner["run_id"]}
    assert lease_token() == before + 1
    assert int(scalar("SELECT count(*) FROM lease_events WHERE event = 'granted'")) == grants_before + 1
    assert clients[0].wait(winner["run_id"])["state"] == "succeeded"
