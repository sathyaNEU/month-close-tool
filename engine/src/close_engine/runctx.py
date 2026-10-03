"""The job parameter contract (Stage 1a): what the control plane passes to every engine job.

All values arrive as strings (Databricks ``job_parameters``, mapped into the wheel task's argv as
``{{job.parameters.x}}``). They are validated here, once, so no job ever acts on a malformed token or
resource name.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from close_engine import __version__ as ENGINE_VERSION

RUN_ID_RE: Final = re.compile(r"^[0-9a-f]{32}$")
LEGACY_RUN_ID_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RESOURCE_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}:[A-Za-z0-9_.-]{1,96}$")
REQUEST_ID_RE: Final = re.compile(r"^[A-Za-z0-9-]{1,64}$")
DEFAULT_REQUESTED_AT: Final = "2000-01-01T00:00:00+00:00"


@dataclass(frozen=True)
class RunContext:
    run_id: str
    attempt: int
    lease_resource: str
    fencing_token: int
    requested_at: datetime
    root_uri: str
    request_id: str | None

    def commit_metadata(self) -> str:
        """Delta ``userMetadata`` recorded on every commit, linking data to the run and its fence."""
        return json.dumps(
            {
                "run_id": self.run_id,
                "attempt": self.attempt,
                "fencing_token": self.fencing_token,
                "engine_version": ENGINE_VERSION,
            },
            sort_keys=True,
        )


def add_contract_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-id", required=True, help="32 lowercase hex characters (legacy ids allowed locally)")
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--lease-resource", default="smoke:default")
    parser.add_argument("--fencing-token", type=int, default=1)
    parser.add_argument("--requested-at", default=DEFAULT_REQUESTED_AT, help="RFC 3339 UTC, from Postgres")
    parser.add_argument("--root-uri", required=True)
    parser.add_argument("--request-id", default=None)


def context_from_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> RunContext:
    if not (RUN_ID_RE.fullmatch(args.run_id) or LEGACY_RUN_ID_RE.fullmatch(args.run_id)):
        parser.error("--run-id must be 32 lowercase hex characters")
    if not 1 <= args.attempt <= 99:
        parser.error("--attempt must be between 1 and 99")
    if not RESOURCE_RE.fullmatch(args.lease_resource):
        parser.error("--lease-resource is malformed")
    if args.fencing_token < 1:
        parser.error("--fencing-token must be >= 1")
    if args.request_id is not None and not REQUEST_ID_RE.fullmatch(args.request_id):
        parser.error("--request-id is malformed")
    try:
        requested_at = datetime.fromisoformat(args.requested_at)
    except ValueError:
        parser.error("--requested-at must be RFC 3339")
    if requested_at.tzinfo is None:
        parser.error("--requested-at must include a UTC offset")
    return RunContext(
        run_id=args.run_id,
        attempt=args.attempt,
        lease_resource=args.lease_resource,
        fencing_token=args.fencing_token,
        requested_at=requested_at,
        root_uri=args.root_uri,
        request_id=args.request_id,
    )
