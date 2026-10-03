"""The local stand-in for a Databricks Spark Connect endpoint: the Spark Connect server on the
close-spark image, serving the app's read path.

It reads the lake with the reader-only S3 identity (so a write through it fails with 403), runs the
pinned session settings as every session's defaults, and uses the FAIR scheduler so concurrent
queries share the driver. Spark Connect has no authentication, so the service is only ever on the
backend network and never published.

    python -m close_jobs_emulator.connect_server      (the spark-connect service's command)
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from close_contracts.spark_runtime import PINNED_SPARK_CONF
from close_jobs_emulator.__main__ import JVM_FLAGS, spark_base
from close_jobs_emulator.runner import SCRUBBED_PREFIXES

SERVER_CLASS = "org.apache.spark.sql.connect.service.SparkConnectServer"


def command(environ: Mapping[str, str]) -> tuple[list[str], dict[str, str]]:
    jars, conf, child_env = spark_base(environ)
    conf.update(
        {
            "spark.connect.grpc.binding.address": "0.0.0.0",  # noqa: S104  # nosec B104 (backend network only)
            "spark.connect.grpc.binding.port": environ.get("CONNECT_PORT", "15002"),
            "spark.scheduler.mode": "FAIR",
            "spark.driver.extraJavaOptions": JVM_FLAGS,  # includes -XX:ActiveProcessorCount=2 (local[2])
            **PINNED_SPARK_CONF,
        }
    )
    cmd = [
        "spark-submit",
        "--class",
        SERVER_CLASS,
        "--name",
        "close-spark-connect",
        "--master",
        "local[2]",
        "--driver-memory",
        environ.get("CONNECT_DRIVER_MEMORY", "1g"),
    ]
    if jars:
        cmd += ["--jars", jars]
    for key, value in conf.items():
        cmd += ["--conf", f"{key}={value}"]
    return [*cmd, "spark-internal"], child_env


def main() -> None:
    cmd, child_env = command(os.environ)
    env = {k: v for k, v in os.environ.items() if not k.startswith(SCRUBBED_PREFIXES)} | child_env
    os.execvpe(cmd[0], cmd, env)  # noqa: S606  # nosec B606 (fixed argv; replaces this process so signals reach Spark)


if __name__ == "__main__":
    main()
