"""Scheduler claims: with several instances every due tick runs exactly once, and a survivor takes over
within one interval (Gate 1a). The 100-interval version is marked slow and runs at the gate."""

from __future__ import annotations

import threading
import time
from collections import Counter
from datetime import timedelta

import pytest
from sqlalchemy import Engine, text

from close_platform.scheduler import PeriodicTask, Scheduler

pytestmark = pytest.mark.pg


def _run_instances(
    engine: Engine, seconds: float, *, stop_first_after: float | None = None
) -> list[tuple[str, str, int]]:
    ran: list[tuple[str, str, int]] = []
    lock = threading.Lock()

    def make(name: str) -> Scheduler:
        return Scheduler(engine, [PeriodicTask("heartbeat", timedelta(seconds=1), lambda: None)], instance_id=name)

    stops = {"a": threading.Event(), "b": threading.Event()}

    def loop(name: str) -> None:
        sched = make(name)
        sched.register()
        while not stops[name].is_set():
            for task, seq in sched.tick():
                with lock:
                    ran.append((name, task, seq))
            stops[name].wait(0.05)

    threads = {n: threading.Thread(target=loop, args=(n,)) for n in stops}
    for t in threads.values():
        t.start()
    if stop_first_after is not None:
        time.sleep(stop_first_after)
        stops["a"].set()  # instance "a" dies (SIGKILL-equivalent: it simply stops claiming)
        time.sleep(seconds - stop_first_after)
    else:
        time.sleep(seconds)
    for event in stops.values():
        event.set()
    for t in threads.values():
        t.join()
    return ran


def _assert_each_tick_once(ran: list[tuple[str, str, int]]) -> None:
    per_tick = Counter((task, seq) for _, task, seq in ran)
    assert per_tick
    assert max(per_tick.values()) == 1, per_tick.most_common(3)
    seqs = sorted(seq for _, _, seq in ran)
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))  # no tick skipped while instances are alive


def test_two_instances_run_each_tick_exactly_once(engine: Engine) -> None:
    ran = _run_instances(engine, 6.5)
    _assert_each_tick_once(ran)
    assert len(ran) >= 5


def test_survivor_takes_over_within_one_interval(engine: Engine) -> None:
    ran = _run_instances(engine, 7.0, stop_first_after=3.0)
    _assert_each_tick_once(ran)
    with engine.connect() as conn:
        last = conn.execute(text("SELECT last_claimed_by, last_claimed_at FROM scheduler_tasks")).one()
    assert last.last_claimed_by == "b"
    b_ticks = [seq for name, _, seq in ran if name == "b"]
    assert b_ticks, "survivor never claimed"


def test_failing_task_is_recorded_and_other_tasks_still_run(engine: Engine) -> None:
    calls: list[str] = []

    def boom() -> None:
        raise RuntimeError("task broke")

    sched = Scheduler(
        engine,
        [
            PeriodicTask("bad", timedelta(seconds=1), boom),
            PeriodicTask("good", timedelta(seconds=1), lambda: calls.append("x")),
        ],
        instance_id="a",
    )
    sched.register()
    sched.tick()
    assert calls == ["x"]
    with engine.connect() as conn:
        err = conn.execute(text("SELECT last_error FROM scheduler_tasks WHERE task = 'bad'")).scalar_one()
    assert "task broke" in err


def test_run_forever_survives_a_database_that_is_down_at_boot(engine: Engine) -> None:
    calls: list[int] = []
    sched = Scheduler(engine, [PeriodicTask("boot", timedelta(seconds=1), lambda: calls.append(1))], instance_id="a")
    real_register, failures = sched.register, [0]

    def flaky_register() -> None:
        if failures[0] < 2:
            failures[0] += 1
            raise ConnectionError("database not up yet")
        real_register()

    sched.register = flaky_register  # type: ignore[method-assign]
    stop = threading.Event()
    thread = threading.Thread(target=sched.run_forever, args=(stop, timedelta(milliseconds=50)))
    thread.start()
    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.05)
    stop.set()
    thread.join()
    assert failures[0] == 2
    assert calls, "the scheduler thread gave up after a failed registration"


def test_a_claim_row_that_disappears_is_registered_again(engine: Engine, admin_engine: Engine) -> None:
    calls: list[int] = []
    sched = Scheduler(engine, [PeriodicTask("heal", timedelta(seconds=1), lambda: calls.append(1))], instance_id="a")
    stop = threading.Event()
    args = (stop, timedelta(milliseconds=50), timedelta(milliseconds=300))
    thread = threading.Thread(target=sched.run_forever, args=args)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline:
            time.sleep(0.05)
        with admin_engine.begin() as tx:
            tx.execute(text("DELETE FROM scheduler_tasks"))
        seen = len(calls)
        deadline = time.monotonic() + 5
        while len(calls) == seen and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        stop.set()
        thread.join()
    assert len(calls) > seen, "the task never ran again after its claim row was deleted"


def test_a_claimer_frozen_mid_transaction_delays_the_survivor_by_well_under_an_interval(engine: Engine) -> None:
    from close_platform.scheduler import _CLAIM_GUARDS

    task = PeriodicTask("frozen", timedelta(seconds=1), lambda: None)
    survivor = Scheduler(engine, [task], instance_id="b")
    survivor.register()
    frozen = engine.connect()
    frozen.begin()
    frozen.execute(text(_CLAIM_GUARDS))
    frozen.execute(text("UPDATE scheduler_tasks SET claim_seq = claim_seq + 1 WHERE task = 'frozen'"))  # no commit
    started = time.monotonic()
    ran: list[tuple[str, int]] = []
    while not ran and time.monotonic() - started < 10:
        try:
            ran = survivor.tick()
        except Exception:  # lock timeout while the frozen session still holds the row
            time.sleep(0.05)
    elapsed = time.monotonic() - started
    with pytest.raises(Exception):  # noqa: B017, PT011 (Postgres ended the frozen session)
        frozen.commit()
    frozen.close()
    assert ran, "the survivor never claimed"
    assert elapsed < 1.5, f"survivor waited {elapsed:.2f}s behind a frozen claimer"


def test_duplicate_task_names_are_rejected(engine: Engine) -> None:
    with pytest.raises(ValueError, match="duplicate"):
        Scheduler(engine, [PeriodicTask("x", timedelta(seconds=1), lambda: None)] * 2, instance_id="a")


@pytest.mark.slow
def test_hundred_intervals_with_two_instances(engine: Engine) -> None:
    # The next due time is set from the claim time, so a 1 s interval runs at about 1.02 to 1.05 s;
    # 112 s gives about 106 ticks, comfortably more than the 100 intervals the gate asks for.
    ran = _run_instances(engine, 112.0)
    _assert_each_tick_once(ran)
    assert len(ran) >= 100
