"""Credential providers and the Jobs API 2.2 status mapping (no database needed)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from databricks.sdk.service.jobs import Run

from close_contracts.enums import Env, JobStatus
from close_platform.compute.base import JobSpec
from close_platform.compute.databricks import DatabricksJobsBackend, observation_from_run, parse_result
from close_platform.db.credentials import CredentialError, LakebaseOAuthProvider, StaticCredentialProvider


def test_static_credentials_refuse_to_run_inside_a_databricks_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABRICKS_APP_NAME", "close-app")
    with pytest.raises(CredentialError, match="local development only"):
        StaticCredentialProvider("secret")


def test_static_credentials_need_a_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABRICKS_APP_NAME", raising=False)
    monkeypatch.delenv("PGPASSWORD", raising=False)
    with pytest.raises(CredentialError):
        StaticCredentialProvider.from_env()
    assert repr(StaticCredentialProvider("s3cret")).count("s3cret") == 0


def test_lakebase_token_is_cached_and_refreshed_before_expiry() -> None:
    now = [datetime(2026, 9, 27, 12, 0, tzinfo=UTC)]
    minted: list[int] = []

    def mint() -> tuple[str, datetime]:
        minted.append(1)
        return f"token-{len(minted)}", now[0] + timedelta(hours=1)

    provider = LakebaseOAuthProvider(mint, refresh_margin=timedelta(minutes=10), clock=lambda: now[0])
    assert provider.password() == "token-1"
    provider.refresh_if_needed()
    assert provider.password() == "token-1"  # 60 minutes left: no refresh
    now[0] += timedelta(minutes=51)
    provider.refresh_if_needed()
    assert provider.password() == "token-2"  # 9 minutes left: refreshed in the background


def test_lakebase_rejects_naive_expiry() -> None:
    provider = LakebaseOAuthProvider(lambda: ("t", datetime(2026, 1, 1)))  # noqa: DTZ001 (deliberately naive)
    with pytest.raises(CredentialError, match="timezone-aware"):
        provider.password()


def _run(status: dict[str, object] | None = None, state: dict[str, object] | None = None) -> Run:
    body: dict[str, object] = {"run_id": 42, "tasks": [{"run_id": 4242, "task_key": "main"}]}
    if status is not None:
        body["status"] = status
    if state is not None:
        body["state"] = state
    return Run.from_dict(body)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"state": "QUEUED"}, JobStatus.PENDING),
        ({"state": "RUNNING"}, JobStatus.RUNNING),
        ({"state": "TERMINATING"}, JobStatus.RUNNING),
        ({"state": "TERMINATED", "termination_details": {"code": "SUCCESS", "type": "SUCCESS"}}, JobStatus.SUCCEEDED),
        (
            {"state": "TERMINATED", "termination_details": {"code": "CANCELED", "type": "CLIENT_ERROR"}},
            JobStatus.CANCELLED,
        ),
        (
            {"state": "TERMINATED", "termination_details": {"code": "DRIVER_ERROR", "type": "INTERNAL_ERROR"}},
            JobStatus.FAILED,
        ),
    ],
)
def test_status_v2_mapping(status: dict[str, object], expected: JobStatus) -> None:
    obs = observation_from_run(_run(status=status), 42)
    assert obs.status == expected
    assert obs.task_run_id == 4242


def test_legacy_state_is_only_a_fallback_and_terminating_is_not_terminal() -> None:
    assert observation_from_run(_run(state={"life_cycle_state": "TERMINATING"}), 1).status == JobStatus.PENDING
    obs = observation_from_run(_run(state={"life_cycle_state": "TERMINATED", "result_state": "SUCCESS"}), 1)
    assert obs.status == JobStatus.SUCCEEDED
    assert observation_from_run(_run(), 1).status == JobStatus.UNKNOWN


def test_result_line_parsing() -> None:
    assert parse_result('noise\nCLOSE_JOB_RESULT {"rows": 3}\nmore') == {"rows": 3}
    assert parse_result("no result here") is None
    assert parse_result('CLOSE_JOB_RESULT {"a": 1}\nCLOSE_JOB_RESULT {"a": 2}') is None  # ambiguous
    assert parse_result(None) is None


def test_idempotency_token_shape() -> None:
    import uuid

    spec = JobSpec("smoke", 1, uuid.UUID(int=7), 1)
    token = spec.idempotency_token(Env.LOCAL)
    assert token == f"close-local-smoke-{uuid.UUID(int=7).hex}-a01"
    assert len(token) <= 64
    with pytest.raises(ValueError, match="job key"):
        JobSpec("way_too_long_job_key", 1, uuid.UUID(int=7), 1)
    with pytest.raises(TypeError, match="string"):
        JobSpec("smoke", 1, uuid.UUID(int=7), 1, {"rows": 5})  # type: ignore[dict-item]


def test_lazy_client_is_built_on_first_use_and_a_failed_build_is_retried() -> None:
    client = MagicMock()
    client.jobs.get_run.side_effect = RuntimeError("boom")
    builds: list[int] = []

    def factory() -> MagicMock:
        builds.append(1)
        if len(builds) == 1:
            raise ConnectionError("workspace unreachable")
        return client

    backend = DatabricksJobsBackend(factory)
    assert builds == []  # nothing happens at construction
    first = backend.observe(7)
    assert first.status == JobStatus.UNKNOWN
    assert "workspace unreachable" in (first.message or "")
    second = backend.observe(7)
    assert second.status == JobStatus.UNKNOWN
    assert "boom" in (second.message or "")
    backend.observe(7)
    assert len(builds) == 2  # built once successfully, then reused
