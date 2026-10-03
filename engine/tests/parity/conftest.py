"""Fixtures for the S2 parity suite: a unique prefix in a bucket on the local S3-compatible store."""

from __future__ import annotations

import os
import time
import uuid

import pytest

from close_engine.session import S3Settings


def _ensure_bucket(settings: S3Settings, bucket: str) -> None:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError

    client = boto3.client(
        "s3",
        endpoint_url=settings.endpoint,
        aws_access_key_id=settings.access_key,
        aws_secret_access_key=settings.secret_key,
        region_name=settings.region,
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 2}),
    )
    deadline = time.monotonic() + 60
    while True:
        try:
            names = {b["Name"] for b in client.list_buckets().get("Buckets", [])}
            if bucket not in names:
                client.create_bucket(Bucket=bucket)
            return
        except (BotoCoreError, ClientError):  # gateway may still be starting (connection closed/refused)
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


@pytest.fixture(scope="session")
def s3_root() -> str:
    settings = S3Settings.from_env()
    if os.environ.get("CLOSE_TEST_S3") != "1" or settings is None:
        pytest.fail("parity tests need CLOSE_TEST_S3=1 and CLOSE_S3_* settings: run `make test-parity`")
    bucket = os.environ.get("CLOSE_S3_TEST_BUCKET", "close-lake-test")
    _ensure_bucket(settings, bucket)
    return f"s3a://{bucket}/parity/{uuid.uuid4().hex[:12]}"
