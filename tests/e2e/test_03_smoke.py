"""Ten smoke runs through devproxy, each within 120 s, with results scoped to each user's entities."""

from __future__ import annotations

import time

import pytest
from stacklib import User

pytestmark = pytest.mark.stack


def test_ten_smoke_runs_with_entity_scoped_results() -> None:
    users = {name: User(name) for name in ("alice", "bob", "carol", "dave")}
    timings = []
    for i in range(10):
        starter = users[("alice", "bob", "dave")[i % 3]]
        started = time.monotonic()
        r = starter.start(rows=1000)
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        run = starter.wait(run_id, timeout=120)
        elapsed = time.monotonic() - started
        timings.append(round(elapsed, 1))
        assert run["state"] == "succeeded", run
        assert elapsed <= 120
        seen = {name: user.get(run_id)["result"] for name, user in users.items()}
        assert seen["alice"]["entities"] == ["US01"]
        assert seen["bob"]["entities"] == ["EU01"]
        assert (seen["carol"]["entities"], seen["carol"]["rows"], seen["carol"]["digests_by_entity"]) == ([], 0, {})
        assert seen["dave"]["digests_by_entity"] == {
            **seen["alice"]["digests_by_entity"],
            **seen["bob"]["digests_by_entity"],
        }
        assert seen["alice"]["rows"] + seen["bob"]["rows"] == seen["dave"]["rows"] == 1000
        assert seen["alice"]["rows"] > 0
        assert seen["bob"]["rows"] > 0
    print(f"smoke timings (s): {timings}; max {max(timings)}")
