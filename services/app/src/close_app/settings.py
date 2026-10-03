"""App settings from the environment (``CLOSE_*``). Database settings are the libpq ``PG*`` variables
(injected by a Lakebase resource in production), and the Jobs API client reads ``DATABRICKS_*`` itself."""

from __future__ import annotations

import socket

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from close_contracts.enums import Env
from close_platform.db.credentials import running_in_databricks_app


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLOSE_", extra="ignore", frozen=True)

    env: Env = Env.LOCAL
    instance_id: str = socket.gethostname()
    job_id_smoke: int
    job_id_post_journals: int | None = None  # Stage 7b; the app's job resource in production
    job_id_journal_ref: int | None = None  # Stage 8a: the journaling stand-in
    lake_root: str = "s3a://close-lake/local"

    scheduler_enabled: bool = True
    relay_interval_s: int = 1
    poll_interval_s: int = 2
    journal_tasks_interval_s: int = 60  # recurring journals and approval reminders (Stage 7)
    quarantine_s: int = 600
    token_window_s: int = 3600

    # Short retry budget: the SDK default (300 s) would stall the poller for minutes during an outage.
    jobs_retry_timeout_s: int = 15
    jobs_http_timeout_s: int = 10

    # Read path (Stage 1b): a Spark Connect endpoint, sc://spark-connect:15002 locally.
    spark_remote: str | None = None
    spark_version: str = "4.1.3"  # the Spark version the read path's server must report
    cursor_secret: SecretStr | None = None  # signs page cursors; required with spark_remote
    query_timeout_s: int = 30
    query_concurrency: int = 4

    @model_validator(mode="after")
    def _local_only_on_a_workstation(self) -> Settings:
        if self.env == Env.LOCAL and running_in_databricks_app():
            raise ValueError("CLOSE_ENV must name a deployed environment inside a Databricks App")
        if self.spark_remote and (self.cursor_secret is None or len(self.cursor_secret.get_secret_value()) < 32):
            raise ValueError("CLOSE_CURSOR_SECRET (32+ characters) is required with CLOSE_SPARK_REMOTE")
        return self
