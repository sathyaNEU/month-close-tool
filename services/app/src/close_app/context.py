"""Wiring: database engine, compute backend, run coordinator and background scheduler for one app process."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config
from sqlalchemy import Engine, text

from close_app.close import checklists as close_checklists
from close_app.journals import posting as journal_posting
from close_app.journals import tasks as journal_tasks
from close_app.ledger import journaling as ledger_journaling
from close_app.settings import Settings
from close_contracts.query_cursor import CursorCodec
from close_contracts.resolver import PathTableResolver
from close_contracts.spark_runtime import PINNED_SPARK_CONF
from close_platform.compute.base import ComputeBackend
from close_platform.compute.databricks import DatabricksJobsBackend
from close_platform.coordinator import DURABLE_TOPICS, RunCoordinator
from close_platform.db.credentials import (
    CredentialError,
    PgCredentialProvider,
    StaticCredentialProvider,
    running_in_databricks_app,
)
from close_platform.db.engine import make_engine
from close_platform.outbox import Relay
from close_platform.query import QueryEngine
from close_platform.query.calendars import CalendarLookup, DataFrameCalendars
from close_platform.query.rates import DataFrameRates, RateLookup
from close_platform.runs import Attempt, Run
from close_platform.scheduler import PeriodicTask, Scheduler

SCHEMA_HEAD = "0008"  # must equal the Alembic head; asserted by tests/test_migrations.py
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")

ReadinessCheck = Callable[[], tuple[bool, str]]


def credential_provider() -> PgCredentialProvider:
    if running_in_databricks_app():
        raise CredentialError("Lakebase OAuth credentials are wired in Stage 10")
    return StaticCredentialProvider.from_env()


_ENTITY_RE: Final = re.compile(r"^[A-Z0-9]{2,16}$")
_HASH_RE: Final = re.compile(r"^[0-9a-f]{64}$")


def job_params(settings: Settings) -> Callable[[Run, Attempt], Mapping[str, str]]:
    """The job parameter contract, plus each job's own parameters (smoke: rows; post_journals: entity_id)."""

    def build(run: Run, attempt: Attempt) -> Mapping[str, str]:
        request_id = run.request_id if run.request_id and _REQUEST_ID_RE.fullmatch(run.request_id) else "none"
        common = {
            "run_id": run.run_id.hex,
            "attempt": str(attempt.attempt),
            "lease_resource": run.lease_resource,
            "fencing_token": str(attempt.fencing_token),
            "requested_at": run.created_at.isoformat(),
            "request_id": request_id,
            "root_uri": settings.lake_root,
        }
        if run.job_key in ("post_journals", "journal_ref"):
            entity_id = str(run.params["entity_id"])
            if not _ENTITY_RE.fullmatch(entity_id):
                raise ValueError(f"invalid entity {entity_id!r}")
            if run.job_key == "post_journals":
                return common | {"entity_id": entity_id}
            hashes = {k: str(run.params.get(k) or "") for k in ("snapshot_hash", "pinned_inputs")}
            if not _HASH_RE.fullmatch(hashes["snapshot_hash"]) or (
                hashes["pinned_inputs"] and not _HASH_RE.fullmatch(hashes["pinned_inputs"])
            ):
                raise ValueError("invalid journaling hashes")
            return common | {"entity_id": entity_id, **hashes}
        return common | {"rows": str(int(run.params["rows"]))}

    return build


smoke_params = job_params  # the Stage 1 name


def jobs_backend(*, retry_timeout_s: int, http_timeout_s: int) -> DatabricksJobsBackend:
    """Jobs API client from the ``DATABRICKS_*`` environment (the app's service principal in production,
    ``jobs-client.env`` locally). Built lazily, so the app starts even when the workspace is unreachable."""

    def factory() -> WorkspaceClient:
        return WorkspaceClient(
            config=Config(retry_timeout_seconds=retry_timeout_s, http_timeout_seconds=http_timeout_s)
        )

    return DatabricksJobsBackend(factory)


def jobs_readiness(settings: Settings) -> ReadinessCheck:
    probe = jobs_backend(retry_timeout_s=2, http_timeout_s=3)  # own short budget: readiness must answer quickly

    def check() -> tuple[bool, str]:
        return True, probe.describe_job(settings.job_id_smoke)

    return check


@dataclass
class AppContext:
    settings: Settings
    engine: Engine
    compute: ComputeBackend
    coordinator: RunCoordinator
    query: QueryEngine | None = None  # the read path; None when no Spark Connect endpoint is configured
    rates: RateLookup | None = None  # Stage 7c: the narrow FX rate lookup, over the same Spark Connect pool
    calendars: CalendarLookup | None = None  # Stage 8b: the narrow business-calendar lookup, over the same pool
    readiness: list[tuple[str, ReadinessCheck]] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start_background(self) -> None:
        if not self.settings.scheduler_enabled:
            return
        relay = Relay(
            self.engine,
            self.coordinator.handlers(),
            instance_id=self.settings.instance_id,
            never_dead_letter=DURABLE_TOPICS,
        )
        journal_every = timedelta(seconds=self.settings.journal_tasks_interval_s)
        scheduler = Scheduler(
            self.engine,
            [
                PeriodicTask("outbox-relay", timedelta(seconds=self.settings.relay_interval_s), relay.run_once),
                PeriodicTask("run-poller", timedelta(seconds=self.settings.poll_interval_s), self.coordinator.poll),
                PeriodicTask(
                    "journal-recurring",
                    journal_every,
                    lambda: journal_tasks.run_recurring(self.engine, rates=self.rates),
                ),
                PeriodicTask("journal-reminders", journal_every, lambda: journal_tasks.run_reminders(self.engine)),
                PeriodicTask("close-reminders", journal_every, lambda: close_checklists.run_reminders(self.engine)),
                PeriodicTask(
                    "posting-reconcile",
                    timedelta(seconds=self.settings.poll_interval_s),
                    lambda: journal_posting.reconcile(self.engine),
                ),
                PeriodicTask(
                    "journaling-reconcile",
                    timedelta(seconds=self.settings.poll_interval_s),
                    lambda: ledger_journaling.reconcile(self.engine),
                ),
            ],
            instance_id=self.settings.instance_id,
        )
        self._thread = threading.Thread(
            target=scheduler.run_forever, args=(self._stop, timedelta(milliseconds=250)), daemon=True
        )
        self._thread.start()

    def stop_background(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)


def read_path(settings: Settings) -> tuple[QueryEngine, RateLookup, CalendarLookup, ReadinessCheck]:
    """The query engine and the narrow rate and calendar lookups over one Spark Connect session pool, and a readiness
    check that holds the server to the Spark version and pinned settings the results depend on."""
    from close_platform.query.connect import ConnectSessions
    from close_platform.query.dataframe import DataFrameBackend

    if settings.spark_remote is None or settings.cursor_secret is None:
        raise ValueError("the read path needs CLOSE_SPARK_REMOTE and CLOSE_CURSOR_SECRET")
    sessions = ConnectSessions(
        settings.spark_remote, concurrency=settings.query_concurrency, timeout_s=float(settings.query_timeout_s)
    )
    resolver = PathTableResolver(lake_root=settings.lake_root)
    backend = DataFrameBackend(sessions.run, resolver)
    engine = QueryEngine(backend, CursorCodec(settings.cursor_secret.get_secret_value().encode()))

    def check() -> tuple[bool, str]:
        return sessions.readiness(settings.spark_version, PINNED_SPARK_CONF)

    return engine, DataFrameRates(sessions.run, resolver), DataFrameCalendars(sessions.run, resolver), check


def build_context(
    settings: Settings,
    *,
    engine: Engine | None = None,
    compute: ComputeBackend | None = None,
    query: QueryEngine | None = None,
    rates: RateLookup | None = None,
    calendars: CalendarLookup | None = None,
) -> AppContext:
    engine = engine or make_engine(credential_provider())
    readiness: list[tuple[str, ReadinessCheck]] = []
    if compute is None:
        compute = jobs_backend(
            retry_timeout_s=settings.jobs_retry_timeout_s, http_timeout_s=settings.jobs_http_timeout_s
        )
        readiness.append(("jobs_api", jobs_readiness(settings)))
    if query is None and settings.spark_remote:
        query, found_rates, found_calendars, spark_ready = read_path(settings)
        rates = rates or found_rates
        calendars = calendars or found_calendars
        readiness.append(("spark_connect", spark_ready))
    coordinator = RunCoordinator(
        engine,
        compute,
        env=settings.env,
        actor=settings.instance_id,
        job_ids={"smoke": settings.job_id_smoke}
        | ({"post_journals": settings.job_id_post_journals} if settings.job_id_post_journals else {})
        | ({"journal_ref": settings.job_id_journal_ref} if settings.job_id_journal_ref else {}),
        build_params=job_params(settings),
        quarantine=timedelta(seconds=settings.quarantine_s),
        token_window=timedelta(seconds=settings.token_window_s),
    )
    ctx = AppContext(settings, engine, compute, coordinator, query, rates, calendars)

    def database() -> tuple[bool, str]:
        with engine.connect() as conn:
            head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        return head == SCHEMA_HEAD, f"schema at {head}, expected {SCHEMA_HEAD}"

    ctx.readiness = [("database", database), *readiness]
    return ctx
