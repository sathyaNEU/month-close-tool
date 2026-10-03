"""Readiness enforces the schema head; the job parameter contract holds from app to bundle to engine."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from pgtestkit import ALEMBIC_INI, REPO

import close_app.context as context_module
from close_app import devseed
from close_app.context import AppContext, smoke_params
from close_app.logs import JsonFormatter, request_id_var
from close_app.settings import Settings
from close_engine.jobs import smoke
from close_jobs_emulator.bundle import load_bundle
from close_platform.runs import Attempt, AttemptState, Run, RunState


def test_schema_head_constant_matches_the_migrations() -> None:
    script = ScriptDirectory.from_config(AlembicConfig(str(ALEMBIC_INI)))
    assert script.get_current_head() == context_module.SCHEMA_HEAD


@pytest.mark.pg
def test_ready_when_the_schema_is_at_head(client: TestClient) -> None:
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["checks"]["database"]["ok"] is True


@pytest.mark.pg
def test_not_ready_when_the_schema_is_not_at_head(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    head = context_module.SCHEMA_HEAD
    monkeypatch.setattr(context_module, "SCHEMA_HEAD", "9999")
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["database"] == {"ok": False, "detail": f"schema at {head}, expected 9999"}


@pytest.mark.pg
def test_a_failing_dependency_makes_the_instance_unready(client: TestClient, ctx: AppContext) -> None:
    def broken() -> tuple[bool, str]:
        raise ConnectionError("jobs api down")

    ctx.readiness.append(("jobs_api", broken))
    body = client.get("/readyz").json()
    assert body["ready"] is False
    assert body["checks"]["jobs_api"] == {"ok": False, "detail": "ConnectionError: jobs api down"}


def _run(request_id: str | None = "req-9") -> tuple[Run, Attempt]:
    now = datetime(2026, 9, 27, 10, 30, tzinfo=UTC)
    run_id = uuid.uuid4()
    run = Run(
        run_id,
        "smoke",
        "smoke:default",
        "1001",
        request_id,
        {"rows": 123},
        RunState.REQUESTED,
        1,
        None,
        None,
        1,
        now,
        now,
        None,
    )
    attempt = Attempt(run_id, 1, 42, "tok", None, AttemptState.PENDING, None, None, None, None)
    return run, attempt


def test_app_params_render_through_the_bundle_into_the_engine_cli() -> None:
    settings = Settings(job_id_smoke=1001, lake_root="s3a://close-lake/local")
    run, attempt = _run()
    params = smoke_params(settings)(run, attempt)
    job = load_bundle(REPO / "engine" / "databricks.yml", job_ids={"smoke": 1001})[1001]  # the smoke job
    assert set(params) == {p.name for p in job.parameters}  # nothing missing, nothing unknown
    args = job.render_args(params)
    assert args[0] == "smoke"
    rctx, rows, _ = smoke._parse(args[1:])
    assert rctx.run_id == run.run_id.hex
    assert (rctx.attempt, rctx.fencing_token, rows) == (1, 42, 123)
    assert rctx.lease_resource == "smoke:default"
    assert rctx.request_id == "req-9"
    assert rctx.requested_at == run.created_at  # Postgres time, round-tripped as RFC 3339 UTC


@pytest.mark.parametrize("request_id", [None, "", "has space", "x" * 65, "semi;colon"])
def test_request_ids_the_engine_would_reject_become_none(request_id: str | None) -> None:
    run, attempt = _run(request_id)
    assert smoke_params(Settings(job_id_smoke=1))(run, attempt)["request_id"] == "none"


def test_settings_require_the_job_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLOSE_JOB_ID_SMOKE", raising=False)
    with pytest.raises(ValueError, match="job_id_smoke"):
        Settings()  # type: ignore[call-arg]
    monkeypatch.setenv("CLOSE_JOB_ID_SMOKE", "1001")
    assert Settings().job_id_smoke == 1001  # type: ignore[call-arg]


def test_devseed_refuses_outside_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOSE_ENV", "prod")
    assert devseed.main() == 2


def test_json_log_lines_carry_the_request_id() -> None:
    token = request_id_var.set("req-77")
    try:
        record = logging.LogRecord("close_app.access", logging.INFO, __file__, 1, "request", None, None)
        record.status = 202
        line = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert line["request_id"] == "req-77"
    assert line["status"] == 202
    assert line["msg"] == "request"


def test_app_yaml_runs_the_module_entry_point() -> None:
    import yaml

    spec = yaml.safe_load(Path(REPO / "services" / "app" / "app.yaml").read_text(encoding="utf-8"))
    assert spec["command"] == ["python", "-m", "close_app"]
    env = {e["name"]: e for e in spec["env"]}
    assert env["CLOSE_JOB_ID_SMOKE"]["valueFrom"] == "smoke-job"


def test_local_env_is_refused_inside_a_databricks_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABRICKS_APP_NAME", "close-app")
    with pytest.raises(ValueError, match="deployed environment"):
        Settings(job_id_smoke=1)
    assert Settings(job_id_smoke=1, env="dev").env == "dev"


def test_log_levels_can_be_raised_per_logger() -> None:
    from close_app import logs

    logger = logging.getLogger("close_platform.scheduler")
    before = logger.level
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        logs.configure({"CLOSE_LOG_LEVELS": "close_platform.scheduler=debug"})
        assert logger.level == logging.DEBUG
        assert root.level == logging.INFO
    finally:
        logger.setLevel(before)
        root.handlers[:] = handlers
        root.setLevel(level)
