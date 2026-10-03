"""Shared enumerations. Values are part of the public contract: change them only with a new version."""

from __future__ import annotations

from enum import StrEnum


class Env(StrEnum):
    """Deployment environment. ``local`` is the kind/Docker stack on a developer machine."""

    LOCAL = "local"
    DEV = "dev"
    STG = "stg"
    PROD = "prod"


class Schema(StrEnum):
    """Logical schemas inside a platform catalog (``close_<env>``)."""

    LANDING = "landing"
    REF = "ref"
    MATCH = "match"
    LEDGER = "ledger"
    OPS = "ops"


class JobKind(StrEnum):
    """Engine jobs launched by the orchestrator (each is a ``main(argv)`` entrypoint)."""

    SMOKE = "smoke"
    DDL_APPLY = "ddl_apply"
    NORMALIZE = "normalize"
    MATCH_RUN = "match_run"
    APPLY_COMMANDS = "apply_commands"


class JobStatus(StrEnum):
    """Status of a compute job as reported by a ComputeBackend."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
