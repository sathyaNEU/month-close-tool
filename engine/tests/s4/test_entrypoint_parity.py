"""Spike S4 (local half): the same job gives the same digest via spark-submit and `python -m`.

Each invocation starts its own JVM in a subprocess, exactly like a k8s Job would, and writes to its
own lake root, so the only shared thing is the code.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from close_engine.jobs import RESULT_PREFIX

pytestmark = [pytest.mark.spark, pytest.mark.slow]

REPO = Path(__file__).resolve().parents[3]


def _run(cmd: list[str], **extra_env: str) -> dict[str, object]:
    # Never leak this process's py4j gateway into the child: each run must start its own JVM.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYSPARK_GATEWAY")}
    env["PYSPARK_PYTHON"] = sys.executable
    env["PYSPARK_DRIVER_PYTHON"] = sys.executable
    env.update(extra_env)
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=REPO, timeout=600, check=False)
    assert proc.returncode == 0, f"{cmd} failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}"
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith(RESULT_PREFIX)]
    assert len(lines) == 1, proc.stdout
    result: dict[str, object] = json.loads(lines[0][len(RESULT_PREFIX) :])
    return result


def test_spark_submit_and_python_m_produce_identical_digests(tmp_path: Path) -> None:
    job_args = ["smoke", "--run-id", "s4-parity", "--rows", "2000"]
    via_module = _run(
        [sys.executable, "-m", "close_engine.jobs", *job_args, "--root-uri", (tmp_path / "a").as_uri()],
        CLOSE_SPARK_MASTER="local[4]",
    )
    spark_submit = shutil.which("spark-submit", path=str(Path(sys.executable).parent))
    assert spark_submit, "spark-submit not found next to the venv python"
    via_submit = _run(
        [
            spark_submit,
            "--master",
            "local[2]",
            "--conf",
            "spark.ui.enabled=false",
            "--conf",
            "spark.jars.packages=io.delta:delta-spark_4.1_2.13:4.4.0",
            "--conf",
            "spark.sql.extensions=io.delta.sql.DeltaSparkSessionExtension",
            "--conf",
            "spark.sql.catalog.spark_catalog=org.apache.spark.sql.delta.catalog.DeltaCatalog",
            str(REPO / "engine/scripts/run_job.py"),
            *job_args,
            "--root-uri",
            (tmp_path / "b").as_uri(),
        ]
    )
    assert via_module["rows"] == via_submit["rows"] == 2000
    assert via_module["digest"] == via_submit["digest"]
    # Each launcher's own configuration was used: spark-submit's --master was not overridden.
    assert via_module["spark_master"] == "local[4]"
    assert via_submit["spark_master"] == "local[2]"
