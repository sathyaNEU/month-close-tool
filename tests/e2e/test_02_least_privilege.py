"""Least privilege in the running configuration: each secret reaches only the services that need it."""

from __future__ import annotations

import json
from typing import Any

import pytest
from stacklib import REPO, compose

pytestmark = pytest.mark.stack

SECRETS = REPO / ".local" / "secrets"


def _env_file(name: str) -> dict[str, str]:
    out = {}
    for line in SECRETS.joinpath(name).read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key] = value
    return out


def _config(*profiles: str) -> dict[str, Any]:
    args = [arg for p in profiles for arg in ("--profile", p)]
    config: dict[str, Any] = json.loads(compose(*args, "config", "--format", "json").stdout)
    return config


def _holders(config: dict[str, Any], secret: str) -> set[str]:
    return {name for name, spec in config["services"].items() if secret in json.dumps(spec.get("environment") or {})}


def test_the_s3_writer_key_is_only_in_jobs() -> None:
    writer = _env_file("s3-writer.env")
    for secret in (writer["CLOSE_S3_ACCESS_KEY"], writer["CLOSE_S3_SECRET_KEY"]):
        assert _holders(_config(), secret) == {"jobs"}
        # The opt-in zombie writer is the one named exception: a zombie is a writer by definition.
        assert _holders(_config("ha", "faults"), secret) == {"jobs", "faults"}


def test_the_app_has_no_object_store_credentials() -> None:
    for name in ("app", "app2", "migrate"):
        env = _config("ha")["services"][name]["environment"]
        assert not [key for key in env if key.startswith(("CLOSE_S3_", "AWS_"))], name


def test_each_database_password_reaches_only_its_role_and_postgres() -> None:
    pg = _env_file("postgres.env")
    config = _config("ha", "faults")
    # pg-roles is the one-shot deploy step that creates the posting job's role as the superuser (Stage 7b).
    assert _holders(config, pg["POSTGRES_PASSWORD"]) == {"postgres", "pg-roles"}
    assert _holders(config, _env_file("pg-migrator.env")["PGPASSWORD"]) == {"postgres", "migrate"}
    assert _holders(config, _env_file("pg-app.env")["PGPASSWORD"]) == {"postgres", "app", "app2"}
    # The posting job's read-only role: only the jobs service holds it at run time (Stage 7b).
    assert _holders(config, _env_file("pg-ledger-reader.env")["PGPASSWORD"]) == {"postgres", "pg-roles", "jobs"}


_PROBE = """
import json, psycopg
out = {}
checks = {
    "queue": "SELECT count(*) FROM posting_queue",
    "snapshots": "SELECT count(*) FROM job_config_snapshots",
    "config": "SELECT 1 FROM config_snapshots LIMIT 1",
    "journals": "SELECT 1 FROM journals LIMIT 1",
    "audit": "SELECT 1 FROM audit_events LIMIT 1",
    "write": "UPDATE journals SET status = status",
}
with psycopg.connect(autocommit=True) as conn:
    for name, sql in checks.items():
        try:
            conn.execute(sql)
            out[name] = "allowed"
        except psycopg.errors.InsufficientPrivilege:
            out[name] = "denied"
print(json.dumps(out))
"""


def test_the_ledger_reader_can_read_only_its_views() -> None:
    """From inside the jobs container, with its own credentials: its two views, and nothing else."""
    seen = json.loads(compose("exec", "-T", "jobs", "python", "-c", _PROBE).stdout)
    assert seen == {
        "queue": "allowed",
        "snapshots": "allowed",  # Stage 8a: the snapshots of journaling runs in progress
        "config": "denied",
        "journals": "denied",
        "audit": "denied",
        "write": "denied",
    }


def test_jobs_api_credentials_reach_only_the_app_and_the_emulator() -> None:
    config = _config("ha", "faults")
    assert _holders(config, _env_file("jobs-client.env")["DATABRICKS_CLIENT_SECRET"]) == {"app", "app2", "jobs"}
    assert _holders(config, _env_file("jobs-server.env")["EMULATOR_PAT"]) == {"jobs"}


def test_the_s3_reader_key_is_only_in_spark_connect() -> None:
    reader = _env_file("s3-reader.env")
    for secret in (reader["CLOSE_S3_ACCESS_KEY"], reader["CLOSE_S3_SECRET_KEY"]):
        assert _holders(_config("ha", "faults"), secret) == {"spark-connect"}


def test_the_cursor_signing_key_is_only_in_the_app() -> None:
    assert _holders(_config("ha", "faults"), _env_file("app.env")["CLOSE_CURSOR_SECRET"]) == {"app", "app2"}
