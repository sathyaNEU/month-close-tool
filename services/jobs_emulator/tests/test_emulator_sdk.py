"""The official databricks-sdk and our DatabricksJobsBackend drive the emulator unchanged (spike S6')."""

from __future__ import annotations

import socket
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config

from close_contracts.enums import Env, JobStatus
from close_jobs_emulator.__main__ import build_state
from close_jobs_emulator.api import create_app
from close_platform.compute.base import JobSpec
from close_platform.compute.databricks import DatabricksJobsBackend

REPO = Path(__file__).resolve().parents[3]
PAT = "dapi-test-token"
CLIENT_ID, CLIENT_SECRET = "close-app-sp-test", "s3cr3t-test"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def emulator(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    state_dir = tmp_path_factory.mktemp("jobs")
    bundle = state_dir / "bundle.yml"
    text = (REPO / "engine" / "databricks.yml").read_text().replace("timeout_seconds: 900", "timeout_seconds: 6")
    bundle.write_text(text)
    env = {
        "EMULATOR_BUNDLE": str(bundle),
        "EMULATOR_JOB_IDS": "smoke=1001",
        "EMULATOR_STATE_DIR": str(state_dir),
        "EMULATOR_PAT": PAT,
        "EMULATOR_CLIENT_ID": CLIENT_ID,
        "EMULATOR_CLIENT_SECRET": CLIENT_SECRET,
        "EMULATOR_ALLOW_FAULTS": "1",
        "EMULATOR_LAUNCHER": "python",
        "EMULATOR_RUN_SCRIPT": str(Path(__file__).parent / "fake_job.py"),
        "EMULATOR_CANCEL_GRACE_S": "2",
    }
    state, runner = build_state(env)
    runner.poll_s = 0.05
    runner.start()
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(state), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/healthz").status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.05)
    yield base
    server.should_exit = True
    thread.join(timeout=5)
    runner.stop()


@pytest.fixture
def backend(emulator: str) -> DatabricksJobsBackend:
    return DatabricksJobsBackend(WorkspaceClient(host=emulator, token=PAT))


def _spec(rows: int, attempt: int = 1) -> JobSpec:
    run_id = uuid.uuid4()
    return JobSpec("smoke", 1001, run_id, attempt, {"run_id": run_id.hex, "attempt": str(attempt), "rows": str(rows)})


def _wait_terminal(backend: DatabricksJobsBackend, ext: int, timeout: float = 30) -> JobStatus:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        obs = backend.observe(ext)
        if obs.status.is_terminal:
            return obs.status
        time.sleep(0.1)
    raise AssertionError(f"run {ext} did not terminate")


def test_submit_is_idempotent_and_result_comes_from_the_task_run(backend: DatabricksJobsBackend) -> None:
    spec = _spec(rows=5)
    ext = backend.submit(spec, Env.LOCAL)
    assert all(backend.submit(spec, Env.LOCAL) == ext for _ in range(19))  # 20 submissions, one run
    assert _wait_terminal(backend, ext) == JobStatus.SUCCEEDED
    obs = backend.observe(ext)
    assert obs.task_run_id is not None
    assert obs.task_run_id != ext
    result = backend.result(obs)
    assert result is not None
    assert result["params"]["run-id"] == spec.params["run_id"]


def test_failed_run_maps_to_failed_with_client_error(backend: DatabricksJobsBackend) -> None:
    ext = backend.submit(_spec(rows=13), Env.LOCAL)
    assert _wait_terminal(backend, ext) == JobStatus.FAILED
    obs = backend.observe(ext)
    assert (obs.termination_code, obs.termination_type) == ("RUN_EXECUTION_ERROR", "CLIENT_ERROR")


def test_cancel_terminates_within_the_grace_period(backend: DatabricksJobsBackend) -> None:
    ext = backend.submit(_spec(rows=7), Env.LOCAL)
    deadline = time.monotonic() + 20
    while backend.observe(ext).status != JobStatus.RUNNING:
        assert time.monotonic() < deadline
        time.sleep(0.1)
    started = time.monotonic()
    backend.cancel(ext)
    assert _wait_terminal(backend, ext) == JobStatus.CANCELLED
    assert time.monotonic() - started < 30


def test_timeout_is_enforced(backend: DatabricksJobsBackend) -> None:
    ext = backend.submit(_spec(rows=7), Env.LOCAL)
    started = time.monotonic()
    assert _wait_terminal(backend, ext, timeout=40) == JobStatus.FAILED
    assert time.monotonic() - started < 6 + 10 + 5  # timeout_seconds + grace + polling slack


def test_after_token_expiry_find_locates_the_original_run(emulator: str, backend: DatabricksJobsBackend) -> None:
    spec = _spec(rows=3)
    since = time.time() - 1
    ext = backend.submit(spec, Env.LOCAL)
    httpx.post(f"{emulator}/__emulator/token-ttl", json={"seconds": 0})
    try:
        from datetime import UTC, datetime

        assert backend.find(spec, since=datetime.fromtimestamp(since, tz=UTC)) == ext
    finally:
        httpx.post(f"{emulator}/__emulator/token-ttl", json={"seconds": 3600})


def test_outage_maps_to_unknown_never_terminal(emulator: str, backend: DatabricksJobsBackend) -> None:
    ext = backend.submit(_spec(rows=4), Env.LOCAL)
    httpx.post(f"{emulator}/__emulator/faults", json={"mode": "unavailable"})
    try:
        slow = DatabricksJobsBackend(WorkspaceClient(config=Config(host=emulator, token=PAT, retry_timeout_seconds=1)))
        assert slow.observe(ext).status == JobStatus.UNKNOWN
    finally:
        httpx.post(f"{emulator}/__emulator/faults", json={"mode": "normal"})


def test_m2m_oauth_like_a_databricks_app(emulator: str) -> None:
    client = WorkspaceClient(host=emulator, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    backend = DatabricksJobsBackend(client)
    ext = backend.submit(_spec(rows=2), Env.LOCAL)
    assert _wait_terminal(backend, ext) == JobStatus.SUCCEEDED


def test_errors_use_the_databricks_shape(emulator: str) -> None:
    r = httpx.get(
        f"{emulator}/api/2.2/jobs/runs/get", params={"run_id": 999999}, headers={"Authorization": f"Bearer {PAT}"}
    )
    assert r.status_code == 404
    assert r.json()["error_code"] == "RESOURCE_DOES_NOT_EXIST"
    r = httpx.get(f"{emulator}/api/2.2/jobs/get", params={"job_id": 1001})
    assert r.status_code == 401
    assert r.json()["error_code"] == "UNAUTHENTICATED"
    r = httpx.post(
        f"{emulator}/api/2.2/jobs/run-now",
        json={"job_id": 1001, "job_parameters": {"not_a_param": "x"}},
        headers={"Authorization": f"Bearer {PAT}"},
    )
    assert r.status_code == 400
    assert r.json()["error_code"] == "INVALID_PARAMETER_VALUE"


def test_jobs_never_see_the_emulator_credentials_or_raw_s3_keys(
    backend: DatabricksJobsBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EMULATOR_PAT", "must-not-leak")
    monkeypatch.setenv("CLOSE_S3_SECRET_KEY", "must-not-leak")
    monkeypatch.setenv("CLOSE_FAULTS", "after_claim=/tmp/never")
    ext = backend.submit(_spec(5), Env.LOCAL)
    assert _wait_terminal(backend, ext) == JobStatus.SUCCEEDED
    result = backend.result(backend.observe(ext))
    assert result is not None
    keys = result["env_keys"]
    assert isinstance(keys, list)
    assert "EMULATOR_PAT" not in keys
    assert "CLOSE_S3_SECRET_KEY" not in keys
    assert "CLOSE_FAULTS" in keys  # fault injection still reaches the job


def test_fault_knobs_hold_run_now_list_runs_and_override_the_timeout(
    emulator: str, backend: DatabricksJobsBackend
) -> None:
    httpx.post(f"{emulator}/__emulator/faults", json={"mode": "hold-run-now", "seconds": 1}).raise_for_status()
    try:
        started = time.monotonic()
        ext = backend.submit(_spec(5), Env.LOCAL)
        assert time.monotonic() - started >= 1.0  # the run existed before the caller got its id
    finally:
        httpx.post(f"{emulator}/__emulator/faults", json={"mode": "normal"}).raise_for_status()
    runs = httpx.get(f"{emulator}/__emulator/runs").json()
    assert any(r["run_id"] == ext for r in runs)
    assert httpx.post(f"{emulator}/__emulator/faults", json={"mode": "bogus"}).status_code == 400

    httpx.post(f"{emulator}/__emulator/job-timeout", json={"job_id": 1001, "seconds": 1}).raise_for_status()
    try:
        slow = backend.submit(_spec(7), Env.LOCAL)  # sleeps until killed
        assert _wait_terminal(backend, slow) == JobStatus.FAILED
        assert backend.observe(slow).termination_code == "RUN_EXECUTION_ERROR"
    finally:
        httpx.post(f"{emulator}/__emulator/job-timeout", json={"job_id": 1001, "seconds": 6}).raise_for_status()


def test_oauth_tokens_survive_an_emulator_restart(emulator: str, tmp_path: Path) -> None:
    from close_jobs_emulator.api import EmulatorState, access_token_valid, mint_access_token
    from close_jobs_emulator.store import RunStore

    def state() -> EmulatorState:
        return EmulatorState(RunStore(tmp_path / "s.sqlite"), {}, PAT, CLIENT_ID, CLIENT_SECRET)

    token = mint_access_token(state())
    assert access_token_valid(state(), token)  # a fresh process with the same secret accepts it
    assert not access_token_valid(state(), token[:-1] + ("0" if token[-1] != "0" else "1"))
    other = EmulatorState(RunStore(tmp_path / "o.sqlite"), {}, PAT, CLIENT_ID, "another-secret")
    assert not access_token_valid(other, token)


def test_a_rejected_token_is_replaced_on_the_next_call(emulator: str) -> None:
    def client() -> WorkspaceClient:
        return WorkspaceClient(config=Config(host=emulator, client_id=CLIENT_ID, client_secret=CLIENT_SECRET))

    backend = DatabricksJobsBackend(client)
    ext = backend.submit(_spec(5), Env.LOCAL)
    assert backend.observe(ext).status != JobStatus.UNKNOWN
    httpx.post(f"{emulator}/__emulator/revoke-tokens").raise_for_status()
    assert backend.observe(ext).status == JobStatus.UNKNOWN  # 401: the client is dropped
    assert backend.observe(ext).status != JobStatus.UNKNOWN  # a new client, a new token
