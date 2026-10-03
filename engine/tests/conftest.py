"""Shared fixtures for engine tests.

One local SparkSession (Delta enabled) is shared per pytest process. Parity tests against the
S3-compatible store run in their own pytest invocation with ``CLOSE_TEST_S3=1`` so the S3A connector
jars are only fetched when needed.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from pyspark.sql import SparkSession

from close_engine.session import LocalSparkOptions, S3Settings, build_local_session


@pytest.fixture(scope="session")
def spark() -> Iterator[SparkSession]:
    s3 = S3Settings.from_env() if os.environ.get("CLOSE_TEST_S3") == "1" else None
    opts = LocalSparkOptions(
        app_name="close-engine-tests",
        master=os.environ.get("CLOSE_SPARK_MASTER", "local[4]"),
        driver_memory=os.environ.get("CLOSE_SPARK_DRIVER_MEMORY", "2g"),
        shuffle_partitions=4,
        s3=s3,
    )
    session = build_local_session(opts)
    yield session
    session.stop()
