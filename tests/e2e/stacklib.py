"""Helpers for the end-to-end suite. The suite drives the running compose stack only from outside,
the way a user or an operator would: HTTP through devproxy (the only published port), and
``docker compose`` for everything else (kill, pause, psql as the superuser, emulator fault switches,
the SeaweedFS shell).
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8088"
FAULTS = REPO / ".local" / "faults"
# CLOSE_FAULTS for jobs run by the emulator (compose.e2e.yaml); after_lines_commit is the posting job's (Stage 7b)
POINTS = ("after_claim", "before_commit", "after_lines_commit")
COMPOSE = ["docker", "compose", "-f", "compose.yaml", "-f", "compose.e2e.yaml"]
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "submit_failed"})
ACTIVE_SQL = "('requested', 'submitted', 'running', 'cancel_requested')"
LAKE_ROOT = "s3a://close-lake/local"
SMOKE_LOG = "/buckets/close-lake/local/ops/smoke_results/_delta_log"


def compose(*args: str, check: bool = True, timeout: float = 900) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run([*COMPOSE, *args], cwd=REPO, capture_output=True, text=True, check=False, timeout=timeout)
    if check and proc.returncode != 0:
        raise AssertionError(f"docker compose {' '.join(args)} failed ({proc.returncode}):\n{proc.stderr[-3000:]}")
    return proc


def wait_until(predicate: Callable[[], Any], timeout: float, *, interval: float = 0.25, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(interval)


# ---------------------------------------------------------------- Postgres (superuser, inside the container)


def psql(sql: str) -> list[list[str]]:
    out = compose(
        "exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "close", "-X", "-q", "-At", "-F", "\t",
        "-v", "ON_ERROR_STOP=1", "-c", f"SET search_path = close; {sql}",
    ).stdout  # fmt: skip
    return [line.split("\t") for line in out.splitlines() if line]


def scalar(sql: str) -> str:
    rows = psql(sql)
    assert len(rows) == 1, (sql, rows)
    assert len(rows[0]) == 1, (sql, rows)
    return rows[0][0]


def lease_token() -> int:
    return int(scalar("SELECT COALESCE(max(fencing_token), 0) FROM leases WHERE resource = 'smoke:default'"))


def idle() -> bool:
    return (
        scalar(
            f"SELECT (SELECT count(*) FROM runs WHERE state IN {ACTIVE_SQL}) "
            "+ (SELECT count(*) FROM leases WHERE holder_run_id IS NOT NULL)"
        )
        == "0"
    )


def wait_idle(timeout: float = 240) -> None:
    wait_until(idle, timeout, interval=0.5, what="no active runs and no held lease")


# ---------------------------------------------------------------- Jobs API emulator fault switches


def emulator(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """Call a /__emulator/* switch from inside the jobs container (the emulator is never published)."""
    code = (
        "import sys, urllib.request as u; "
        "data = sys.argv[3].encode() if sys.argv[3] else None; "
        "req = u.Request('http://127.0.0.1:8080' + sys.argv[1], data=data, method=sys.argv[2], "
        "headers={'Content-Type': 'application/json'}); "
        "print(u.urlopen(req, timeout=15).read().decode())"
    )
    payload = json.dumps(body) if body is not None else ""
    return json.loads(compose("exec", "-T", "jobs", "python", "-c", code, path, method, payload).stdout)


def stack_job(name: str, job_id: int, **params: str) -> dict[str, Any]:
    """Run one bundle job through the Jobs API from inside the app container (tools/stack_job.py), as the gate's
    datagen measurement does; returns the tool's JSON outcome."""
    cmd = [*COMPOSE, "exec", "-T", "app", "python", "-", name, str(job_id), *(f"{k}={v}" for k, v in params.items())]
    with (REPO / "tools" / "stack_job.py").open("rb") as script:
        proc = subprocess.run(cmd, cwd=REPO, stdin=script, capture_output=True, check=False, timeout=900)
    lines = [ln for ln in proc.stdout.decode().splitlines() if ln.startswith("{")]
    if not lines:
        raise AssertionError(f"{name} produced no outcome:\n{proc.stderr.decode()[-3000:]}")
    outcome: dict[str, Any] = json.loads(lines[-1])
    return outcome


def emulator_runs(run_id: str | None = None) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = emulator("GET", "/__emulator/runs")
    if run_id is None:
        return runs
    hex_id = uuid.UUID(run_id).hex
    return [r for r in runs if r["job_parameters"].get("run_id") == hex_id]


def emulator_normal() -> None:
    emulator("POST", "/__emulator/faults", {"mode": "normal"})
    emulator("POST", "/__emulator/token-ttl", {"seconds": 3600})
    emulator("POST", "/__emulator/job-timeout", {"job_id": 1001, "seconds": 900})


def job_processes() -> list[str]:
    """Command lines of Spark driver processes still alive in the jobs container (not this probe, whose
    own command line contains the strings it looks for)."""
    code = (
        "import os; "
        "cmds = [open(f'/proc/{p}/cmdline', 'rb').read().replace(b'\\0', b' ').decode(errors='replace') "
        "for p in os.listdir('/proc') if p.isdigit() and int(p) != os.getpid() "
        "and os.path.exists(f'/proc/{p}/cmdline')]; "
        "print('\\n'.join(c for c in cmds if 'SparkSubmit' in c or 'run_job.py' in c))"
    )
    return [line for line in compose("exec", "-T", "jobs", "python", "-c", code).stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------- pause points (files in .local/faults)


def hold(point: str) -> None:
    """The next job to reach ``point`` waits there until ``release``."""
    FAULTS.joinpath(point).unlink(missing_ok=True)
    FAULTS.joinpath(f"{point}.reached").unlink(missing_ok=True)


def release(point: str) -> None:
    FAULTS.mkdir(parents=True, exist_ok=True)
    FAULTS.joinpath(point).touch()


def wait_reached(point: str, timeout: float = 120) -> None:
    wait_until(FAULTS.joinpath(f"{point}.reached").exists, timeout, what=f"a job to reach pause point {point}")


# ---------------------------------------------------------------- Delta log of the smoke table (SeaweedFS shell)


def weed(command: str) -> str:
    return compose(
        "exec", "-T", "seaweedfs", "sh", "-c", f"echo '{command}' | weed shell -master=localhost:9333"
    ).stdout


def smoke_commits() -> list[dict[str, Any]]:
    """commitInfo of every commit to the smoke table, oldest first."""
    names = sorted(
        line.rsplit("/", 1)[-1] for line in weed(f"fs.ls -l {SMOKE_LOG}").splitlines() if line.endswith(".json")
    )
    commits = []
    for name in names:
        for line in weed(f"fs.cat {SMOKE_LOG}/{name}").splitlines():
            if line.startswith('{"commitInfo"'):
                info = json.loads(line)["commitInfo"]
                info["userMetadata"] = json.loads(info.get("userMetadata") or "{}")
                commits.append(info)
    return commits


# ---------------------------------------------------------------- a signed-in dev user


class User:
    def __init__(self, name: str) -> None:
        self.name = name
        self.http = httpx.Client(base_url=BASE, timeout=60)
        self.http.get(f"/__dev/login/{name}").raise_for_status()

    def whoami(self, headers: dict[str, str] | None = None) -> httpx.Response:
        return self.http.get("/api/v1/whoami", headers=headers)

    def start(self, rows: int = 1000, key: str | None = None) -> httpx.Response:
        return self.http.post(
            "/api/v1/smoke/runs", json={"rows": rows}, headers={"Idempotency-Key": key or f"e2e-{uuid.uuid4()}"}
        )

    def get(self, run_id: str) -> dict[str, Any]:
        r = self.http.get(f"/api/v1/smoke/runs/{run_id}")
        r.raise_for_status()
        body: dict[str, Any] = r.json()
        return body

    def cancel(self, run_id: str) -> httpx.Response:
        return self.http.post(f"/api/v1/smoke/runs/{run_id}:cancel", headers={"Idempotency-Key": f"e2e-{uuid.uuid4()}"})

    def wait(self, run_id: str, *, timeout: float = 120, states: frozenset[str] = TERMINAL) -> dict[str, Any]:
        def done() -> dict[str, Any] | None:
            try:
                run = self.get(run_id)
            except httpx.HTTPError:
                return None  # the app may be restarting
            return run if run["state"] in states else None

        result: dict[str, Any] = wait_until(done, timeout, interval=0.5, what=f"run {run_id} to reach {sorted(states)}")
        return result
