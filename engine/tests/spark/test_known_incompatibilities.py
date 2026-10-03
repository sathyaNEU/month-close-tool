"""Canaries for known upstream incompatibilities that shaped the design (run at every gate).

OSS Delta Lake 4.1-4.4 drops SQL parameter markers once its SparkSession extension is loaded:
``DeltaSqlParser`` does not override Spark 4.1's ``ParserInterface.parsePlanWithParameters``
(delta-io/delta#7640, fix proposed in delta-io/delta#7747). The engine therefore never binds SQL
parameters (lint rule SB010) and uses the DataFrame / DeltaTable APIs instead.

The xfail markers are strict: when a Delta release with the fix is installed, these tests XPASS, the
suite fails, and we revisit ADR-0002 deliberately instead of drifting.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from pyspark.errors import AnalysisException
from pyspark.sql import SparkSession

pytestmark = pytest.mark.spark

UPSTREAM = "delta-io/delta#7640: DeltaSqlParser drops SQL parameter markers on Spark 4.1+ (fix proposed in #7747)"


@pytest.mark.xfail(strict=True, raises=AnalysisException, reason=UPSTREAM)
def test_named_parameters_with_delta_extension(spark: SparkSession) -> None:
    assert spark.sql("SELECT :x AS v", args={"x": 1}).collect()[0]["v"] == 1


@pytest.mark.xfail(strict=True, raises=AnalysisException, reason=UPSTREAM)
def test_positional_parameters_with_delta_extension(spark: SparkSession) -> None:
    assert spark.sql("SELECT ? AS v", args=[1]).collect()[0]["v"] == 1


@pytest.mark.xfail(strict=True, raises=AnalysisException, reason=UPSTREAM)
def test_identifier_parameter_with_delta_extension(spark: SparkSession) -> None:
    spark.range(2).createOrReplaceTempView("canary_v")
    assert spark.sql("SELECT count(*) AS n FROM IDENTIFIER(:t)", args={"t": "canary_v"}).collect()[0]["n"] == 2


def test_positive_control_plain_spark_binds_parameters() -> None:
    """Without the Delta extension, Spark 4.1 binds parameters. Must run in its own JVM (settings leak)."""
    code = (
        "from pyspark.sql import SparkSession\n"
        "s = SparkSession.builder.master('local[1]').config('spark.ui.enabled', 'false').getOrCreate()\n"
        "s.range(3).createOrReplaceTempView('v')\n"
        "q = 'SELECT :x + count(*) AS v FROM IDENTIFIER(:t)'\n"
        "print('BOUND', s.sql(q, args={'x': 39, 't': 'v'}).collect()[0]['v'])\n"
    )
    env = {**os.environ, "PYSPARK_PYTHON": sys.executable}
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=300, check=False
    )
    assert "BOUND 42" in proc.stdout, proc.stdout[-2000:] + proc.stderr[-2000:]
