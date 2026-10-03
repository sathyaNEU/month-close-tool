"""Watch the local stack's memory while tests run, and judge it against the Stage 1 budget.

Every couple of seconds it reads each running container's cgroup v2 ``memory.peak`` (the highest
usage since the container started, page cache included), ``memory.max`` (its limit) and
``memory.events`` (``oom_kill``). Peaks are kept per container name across restarts, and the verdict
uses their sum: an upper bound on what the stack ever used at once. Because the limits themselves add
up to about 6.4 GiB, the signal that really binds is ``oom_kill = 0`` (no container ever hit its
limit hard enough to be killed); the report prints each peak against its limit to show the headroom.

    python tools/measure_memory.py start                   # detach a watcher (pid file under .local/gate)
    python tools/measure_memory.py stop [--budget-gib 6.5]  # stop it, print the verdict, exit 1 if over budget
    python tools/measure_memory.py once [--budget-gib 6.5]  # one sample of the current stack, same verdict

The budget is per gate: 6.5 GiB for Stage 1a, 8.5 GiB for Stage 1b (the always-on Spark Connect server).
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATE = REPO / ".local" / "gate" / "memory"
DEFAULT_BUDGET_GIB = 6.5
INTERVAL_S = 2.0
STALE_S = 60.0  # the verdict fails if the watcher stopped sampling this long before `stop`


def _containers() -> list[str]:
    out = subprocess.run(
        ["docker", "ps", "--filter", "label=com.docker.compose.project=close", "--format", "{{.Names}}"],
        capture_output=True, text=True, check=False,
    ).stdout  # fmt: skip
    return sorted(name for name in out.split() if name)


def _read(container: str) -> tuple[int, int, int, int] | None:
    """(peak, limit, oom_kill, anon) of one container, or None if it cannot be read right now.
    ``anon`` is the process memory that cannot be reclaimed (no page cache) at this instant."""
    try:
        proc = subprocess.run(
            ["docker", "exec", container, "cat", "/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/memory.max",
             "/sys/fs/cgroup/memory.events", "/sys/fs/cgroup/memory.stat"],
            capture_output=True, text=True, check=False, timeout=20,
        )  # fmt: skip
        if proc.returncode != 0:  # paused, stopping, or gone
            return None
        lines = proc.stdout.split("\n")
        pairs = dict(line.split()[:2] for line in lines[2:] if len(line.split()) >= 2)
        limit = 0 if lines[1].strip() == "max" else int(lines[1])
        return int(lines[0]), limit, int(pairs.get("oom_kill", "0")), int(pairs.get("anon", "0"))
    except (subprocess.SubprocessError, OSError, ValueError, IndexError):
        return None


def sample(peaks: dict[str, int], ooms: dict[str, int], limits: dict[str, int], anon: dict[str, int]) -> None:
    for name in _containers():
        read = _read(name)
        if read is None:
            continue
        peak, limit, oom, anon_now = read
        peaks[name] = max(peaks.get(name, 0), peak)
        limits[name] = limit
        ooms[name] = max(ooms.get(name, 0), oom)
        anon[name] = max(anon.get(name, 0), anon_now)


def verdict(
    peaks: dict[str, int],
    ooms: dict[str, int],
    samples: int,
    *,
    limits: dict[str, int] | None = None,
    anon: dict[str, int] | None = None,
    budget_gib: float = DEFAULT_BUDGET_GIB,
) -> tuple[bool, str]:
    budget = int(budget_gib * 2**30)
    total = sum(peaks.values())
    killed = {name: n for name, n in ooms.items() if n}
    ok = bool(peaks) and total <= budget and not killed
    limits, anon = limits or {}, anon or {}

    def row(name: str, peak: int) -> str:
        limit = limits.get(name, 0)
        of = f" of {limit / 2**20:.0f}" if limit else ""
        return f"{name.removeprefix('close-')} {peak / 2**20:.0f}{of} MiB (process max {anon.get(name, 0) / 2**20:.0f})"

    rows = ", ".join(row(name, peak) for name, peak in sorted(peaks.items()))
    detail = (
        f"memory: sum of peaks {total / 2**30:.2f} GiB (budget {budget_gib:.1f} GiB), "
        f"oom_kill {sum(ooms.values())} over {samples} samples [{rows}]"
    )
    if killed:
        detail += f"; OOM-killed: {killed}"
    return ok, detail


def watch(out: Path) -> None:
    peaks: dict[str, int] = {}
    ooms: dict[str, int] = {}
    limits: dict[str, int] = {}
    anon: dict[str, int] = {}
    samples, errors = 0, 0
    running = True

    def _stop(*_: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while running:
        try:
            sample(peaks, ooms, limits, anon)
            samples += 1
        except Exception:  # never let one bad sample end the watch; the verdict checks freshness
            errors += 1
        state = {"peaks": peaks, "ooms": ooms, "limits": limits, "anon": anon, "samples": samples, "errors": errors}
        out.write_text(json.dumps({**state, "last_sample": time.time()}, indent=2, sort_keys=True))
        time.sleep(INTERVAL_S)


def main(argv: list[str]) -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    pid_file, data_file = STATE / "watcher.pid", STATE / "memory.json"
    command = argv[0] if argv else "once"
    budget_gib = float(argv[argv.index("--budget-gib") + 1]) if "--budget-gib" in argv else DEFAULT_BUDGET_GIB
    if command == "start":
        data_file.unlink(missing_ok=True)
        proc = subprocess.Popen(
            [sys.executable, __file__, "_watch", str(data_file)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )  # fmt: skip
        pid_file.write_text(str(proc.pid))
        print(f"memory watcher started (pid {proc.pid})")
        return 0
    if command == "_watch":
        watch(Path(argv[1]))
        return 0
    if command == "stop":
        pid = int(pid_file.read_text())
        with contextlib.suppress(ProcessLookupError):  # it died on its own: the freshness check catches that
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.2)
        data = json.loads(data_file.read_text())
        ok, detail = verdict(
            data["peaks"],
            data["ooms"],
            data["samples"],
            limits=data.get("limits"),
            anon=data.get("anon"),
            budget_gib=budget_gib,
        )
        stale = time.time() - float(data.get("last_sample", 0))
        if stale > STALE_S:
            ok, detail = False, f"{detail}; the watcher stopped sampling {stale:.0f}s before the end"
    elif command == "once":
        peaks: dict[str, int] = {}
        ooms: dict[str, int] = {}
        limits: dict[str, int] = {}
        anon: dict[str, int] = {}
        sample(peaks, ooms, limits, anon)
        ok, detail = verdict(peaks, ooms, 1, limits=limits, anon=anon, budget_gib=budget_gib)
    else:
        raise SystemExit("usage: measure_memory.py start|stop|once")
    print(detail)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
