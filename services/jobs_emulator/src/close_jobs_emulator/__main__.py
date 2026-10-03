"""Start the Jobs API emulator: ``python -m close_jobs_emulator``.

Configuration (environment):
  EMULATOR_BUNDLE          path to engine/databricks.yml
  EMULATOR_TARGET          bundle target (default: local)
  EMULATOR_JOB_IDS         e.g. "smoke=1001" (stable ids the app is configured with)
  EMULATOR_STATE_DIR       SQLite state and run logs (a volume)
  EMULATOR_PAT, EMULATOR_CLIENT_ID, EMULATOR_CLIENT_SECRET   accepted credentials
  EMULATOR_TOKEN_TTL_S     idempotency-token memory (default 3600)
  EMULATOR_ALLOW_FAULTS    "1" enables /__emulator/* test switches (local only)
  EMULATOR_LAUNCHER        "spark-submit" (default) or "python" (unit tests)
  EMULATOR_RUN_SCRIPT      spark-submit launcher script (engine/scripts/run_job.py)
  EMULATOR_JARS_DIR        prefetched jars passed with --jars
  EMULATOR_DRIVER_MEMORY   default 1g
  CLOSE_S3_*               S3A endpoint and writer credentials (passed to jobs as AWS_* env, never argv)
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import uvicorn

from close_jobs_emulator.api import EmulatorState, create_app
from close_jobs_emulator.bundle import JobDefinition, load_bundle
from close_jobs_emulator.runner import Runner
from close_jobs_emulator.store import RunStore

Builder = Callable[[JobDefinition | None, list[str]], Sequence[str]]

# ActiveProcessorCount=2 matches the local[2] master every job and the Connect server run with: sized for the
# 18 CPUs the Docker VM exposes, the JVM grew GC, compiler and pool threads (and memory) for cores it never used.
JVM_FLAGS = (
    "-XX:MaxMetaspaceSize=256m -XX:MaxDirectMemorySize=256m -XX:ReservedCodeCacheSize=128m -XX:+ExitOnOutOfMemoryError"
    " -XX:ActiveProcessorCount=2"
)


def _job_ids(raw: str) -> dict[str, int]:
    out = {}
    for item in filter(None, (p.strip() for p in raw.split(","))):
        key, _, value = item.partition("=")
        out[key] = int(value)
    return out


def spark_base(environ: Mapping[str, str]) -> tuple[str, dict[str, str], dict[str, str]]:
    """What every local Spark process shares: the pinned jars, Delta, and S3A with the credentials in
    ``CLOSE_S3_*`` handed over as ``AWS_*`` environment (never on the command line). Returns the jar
    list, the ``--conf`` settings and the extra child environment."""
    jars_dir = Path(environ.get("EMULATOR_JARS_DIR", "/opt/close/jars"))
    jars = ",".join(sorted(str(p) for p in jars_dir.glob("*.jar")))
    conf = {
        "spark.ui.enabled": "false",
        "spark.sql.extensions": "io.delta.sql.DeltaSparkSessionExtension",
        "spark.sql.catalog.spark_catalog": "org.apache.spark.sql.delta.catalog.DeltaCatalog",
        "spark.driver.extraJavaOptions": JVM_FLAGS,
    }
    child_env: dict[str, str] = {"PYSPARK_PYTHON": "python", "PYSPARK_DRIVER_PYTHON": "python"}
    endpoint = environ.get("CLOSE_S3_ENDPOINT")
    if endpoint:
        conf.update(
            {
                "spark.hadoop.fs.s3a.impl": "org.apache.hadoop.fs.s3a.S3AFileSystem",
                "spark.hadoop.fs.s3a.endpoint": endpoint,
                "spark.hadoop.fs.s3a.endpoint.region": environ.get("CLOSE_S3_REGION", "us-east-1"),
                "spark.hadoop.fs.s3a.path.style.access": "true",
                "spark.hadoop.fs.s3a.connection.ssl.enabled": str(endpoint.startswith("https")).lower(),
                "spark.hadoop.fs.s3a.aws.credentials.provider": (
                    "software.amazon.awssdk.auth.credentials.EnvironmentVariableCredentialsProvider"
                ),
            }
        )
        child_env["AWS_ACCESS_KEY_ID"] = environ["CLOSE_S3_ACCESS_KEY"]
        child_env["AWS_SECRET_ACCESS_KEY"] = environ["CLOSE_S3_SECRET_KEY"]
        child_env["AWS_REGION"] = environ.get("CLOSE_S3_REGION", "us-east-1")
    return jars, conf, child_env


def spark_submit_builder(environ: Mapping[str, str]) -> tuple[Builder, dict[str, str]]:
    script = environ.get("EMULATOR_RUN_SCRIPT", "/opt/close/engine/scripts/run_job.py")
    jars, conf, child_env = spark_base(environ)

    def build(_job: JobDefinition | None, args: list[str]) -> Sequence[str]:
        cmd = ["spark-submit", "--master", "local[2]", "--driver-memory", environ.get("EMULATOR_DRIVER_MEMORY", "1g")]
        if jars:
            cmd += ["--jars", jars]
        for key, value in conf.items():
            cmd += ["--conf", f"{key}={value}"]
        return [*cmd, script, *args]

    return build, child_env


def python_builder(environ: Mapping[str, str]) -> tuple[Builder, dict[str, str]]:
    script = environ["EMULATOR_RUN_SCRIPT"]

    def build(_job: JobDefinition | None, args: list[str]) -> Sequence[str]:
        return ["python", script, *args]

    return build, {}


def build_state(environ: Mapping[str, str] = os.environ) -> tuple[EmulatorState, Runner]:
    state_dir = Path(environ.get("EMULATOR_STATE_DIR", "/var/lib/close-jobs"))
    jobs = load_bundle(
        Path(environ.get("EMULATOR_BUNDLE", "/opt/close/engine/databricks.yml")),
        target=environ.get("EMULATOR_TARGET", "local"),
        job_ids=_job_ids(environ.get("EMULATOR_JOB_IDS", "")),
    )
    store = RunStore(state_dir / "state.sqlite")
    store.recover_orphans()
    launcher = environ.get("EMULATOR_LAUNCHER", "spark-submit")
    builder, child_env = spark_submit_builder(environ) if launcher == "spark-submit" else python_builder(environ)
    runner = Runner(
        store=store,
        jobs=jobs,
        build_command=builder,
        logs_dir=state_dir / "logs",
        env=child_env,
        cancel_grace_s=float(environ.get("EMULATOR_CANCEL_GRACE_S", "20")),
    )
    state = EmulatorState(
        store=store,
        jobs=jobs,
        pat=environ["EMULATOR_PAT"],
        client_id=environ["EMULATOR_CLIENT_ID"],
        client_secret=environ["EMULATOR_CLIENT_SECRET"],
        token_ttl_s=float(environ.get("EMULATOR_TOKEN_TTL_S", "3600")),
        allow_faults=environ.get("EMULATOR_ALLOW_FAULTS") == "1",
    )
    return state, runner


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    state, runner = build_state()
    runner.start()
    host = os.environ.get("EMULATOR_HOST", "0.0.0.0")  # noqa: S104  # nosec B104 (container-internal, never published)
    try:
        uvicorn.run(create_app(state), host=host, port=int(os.environ.get("EMULATOR_PORT", "8080")), log_level="info")
    finally:
        runner.stop()


if __name__ == "__main__":
    main()
