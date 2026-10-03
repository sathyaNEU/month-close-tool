"""Measure bringing the local stack up: cold, warm, and three down/up cycles that keep the data.

cold    ``make down-v``, delete the project images, then ``make up`` with ``docker compose build
        --no-cache``: every project layer is rebuilt and every jar, apt and Python package is
        downloaded again. (Docker's shared build cache is left alone on purpose: pruning it would
        wipe the cache of every other project on this machine.) Base images stay, as agreed.
warm    ``make down`` then ``make up`` with everything cached.
cycles  three more ``make down`` / ``make up`` rounds; every service must come back healthy and the
        runs recorded before the cycles must still be there, with a smoke run passing afterwards.

Bytes downloaded are read from the Docker VM's network counters around the cold build. Prints one
JSON document and exits 1 if a budget is missed (cold 600 s, warm 120 s).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROBE_IMAGE = "python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f"
COLD_BUDGET_S, WARM_BUDGET_S = 600, 120
SMOKE = ["uv", "run", "--frozen", "python", "tools/stack_smoke.py"]


def _run(argv: list[str], **env: str) -> float:
    import os

    started = time.monotonic()
    proc = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, check=False, env={**os.environ, **env})
    if proc.returncode != 0:
        raise SystemExit(f"{' '.join(argv)} failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}")
    return time.monotonic() - started


def _vm_rx_bytes() -> int:
    out = subprocess.run(
        ["docker", "run", "--rm", "--net=host", PROBE_IMAGE, "cat", "/proc/net/dev"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    rx = {}
    for line in out.splitlines()[2:]:
        name, _, data = line.partition(":")
        if data.split():
            rx[name.strip()] = int(data.split()[0])
    if "eth0" in rx:  # the VM's uplink; bridged container traffic shows up again on docker0/br-*/veth*
        return rx["eth0"]
    return sum(v for k, v in rx.items() if k != "lo" and not k.startswith(("docker", "br-", "veth", "services")))


def _psql(sql: str) -> str:
    return subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "close", "-At", "-c", sql],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip


def _healthy() -> dict[str, str]:
    out = subprocess.run(
        ["docker", "compose", "ps", "--format", "json"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    services = [json.loads(line) for line in out.splitlines() if line.strip()]
    return {s["Service"]: s.get("Health") or s["State"] for s in services}


def main() -> int:
    result: dict[str, object] = {}
    _run(["make", "down-v"])
    subprocess.run(
        ["docker", "image", "rm", "-f", "close-app:local", "close-spark:local"], capture_output=True, check=False
    )
    rx_before = _vm_rx_bytes()
    result["cold_s"] = round(_run(["make", "up"], BUILD_FLAGS="--no-cache"), 1)
    result["cold_downloaded_mb"] = round((_vm_rx_bytes() - rx_before) / 1e6, 1)
    result["after_cold"] = _healthy()
    smoke = subprocess.run(SMOKE, cwd=REPO, capture_output=True, text=True, check=False)
    result["smoke_after_cold"] = "ok" if smoke.returncode == 0 else smoke.stdout[-500:]
    runs_before = _psql("SELECT count(*) FROM close.runs")
    _run(["make", "down"])
    result["warm_s"] = round(_run(["make", "up"]), 1)
    cycles = []
    for _ in range(3):
        _run(["make", "down"])
        seconds = round(_run(["make", "up"]), 1)
        cycles.append({"up_s": seconds, "health": _healthy(), "runs": _psql("SELECT count(*) FROM close.runs")})
    result["cycles"] = cycles
    smoke = subprocess.run(SMOKE, cwd=REPO, capture_output=True, text=True, check=False)
    result["smoke_after_cycles"] = "ok" if smoke.returncode == 0 else smoke.stdout[-500:]
    kept = all(c["runs"] == runs_before for c in cycles) and int(runs_before) >= 1
    healthy = all(
        state in {"healthy", "exited"}
        for c in cycles
        for state in c["health"].values()  # type: ignore[attr-defined]
    )
    result["data_kept"] = kept
    result["cycles_healthy"] = healthy
    ok = (
        float(result["cold_s"]) <= COLD_BUDGET_S  # type: ignore[arg-type]
        and float(result["warm_s"]) <= WARM_BUDGET_S  # type: ignore[arg-type]
        and kept
        and healthy
        and result["smoke_after_cold"] == "ok"
        and result["smoke_after_cycles"] == "ok"
    )
    result["verdict"] = "PASS" if ok else "FAIL"
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
