"""App tests: the real FastAPI app against the test Postgres, with a scripted Jobs backend.

The background scheduler is off; tests drive the outbox relay and the poller explicitly, so every
step of a run's life is deterministic.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from apptestkit import GRANTS, assign
from fastapi.testclient import TestClient
from journalkit import calendar_days, rate_rows
from pgtestkit import admin_engine, clean_tables, engine, migrated  # noqa: F401 (pytest fixtures)
from sqlalchemy import Engine

from close_app.context import AppContext, build_context
from close_app.main import create_app
from close_app.settings import Settings
from close_contracts.query_cursor import CursorCodec
from close_platform.compute.fake import FakeComputeBackend
from close_platform.coordinator import DURABLE_TOPICS
from close_platform.outbox import Relay
from close_platform.query import QueryEngine
from close_platform.query.calendars import ReferenceCalendars
from close_platform.query.rates import ReferenceRates
from close_platform.query.reference import ReferenceBackend


@pytest.fixture
def settings() -> Settings:
    return Settings(
        job_id_smoke=1001,
        job_id_post_journals=1004,
        job_id_journal_ref=1005,
        scheduler_enabled=False,
        instance_id="test-app",
        lake_root="s3a://test/lake",
    )


@pytest.fixture
def fake() -> FakeComputeBackend:
    return FakeComputeBackend()


@pytest.fixture
def lake() -> ReferenceBackend:
    """The read path's data, in memory: the app runs the real QueryEngine over the reference evaluator."""
    return ReferenceBackend()


@pytest.fixture
def rates() -> ReferenceRates:
    """Exchange rates for the narrow lookup (Stage 7c): daily spot rates for every day of September and October."""
    return ReferenceRates(rate_rows())


@pytest.fixture
def calendars() -> ReferenceCalendars:
    """Business calendars for the narrow lookup (Stage 8b): US01 on US_FED, with its 2026 holidays; EU01 and UK01
    on calendars the lake doesn't have."""
    return ReferenceCalendars({"US01": "US_FED", "EU01": "TARGET2"}, {"US_FED": calendar_days()})


@pytest.fixture
def ctx(
    engine: Engine,  # noqa: F811 (the pgtestkit fixture)
    settings: Settings,
    fake: FakeComputeBackend,
    lake: ReferenceBackend,
    rates: ReferenceRates,
    calendars: ReferenceCalendars,
) -> AppContext:
    for user_id, entity in GRANTS:
        assign(engine, user_id, "match_preparer", entity)
    query = QueryEngine(lake, CursorCodec(b"t" * 32))
    return build_context(settings, engine=engine, compute=fake, query=query, rates=rates, calendars=calendars)


@pytest.fixture
def client(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_app(context=ctx)) as c:
        yield c


@pytest.fixture
def relay(ctx: AppContext) -> Relay:
    return Relay(ctx.engine, ctx.coordinator.handlers(), instance_id="test-relay", never_dead_letter=DURABLE_TOPICS)
