"""SparkSession creation with the platform's pinned, determinism-critical configuration.

Every engine job calls :func:`get_session`. On Databricks it reuses the runtime's session; locally it
builds one with Delta Lake (and the S3A connector when an S3-compatible store is configured). Either
way it then applies and *asserts* :data:`PINNED_CONF`, and the effective values are recorded in the run
manifest, so a run can prove which semantics it executed under.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from pyspark.sql import SparkSession

from close_contracts.spark_runtime import PINNED_SPARK_CONF

PINNED_CONF: Final[Mapping[str, str]] = PINNED_SPARK_CONF  # shared with the app (close_contracts.spark_runtime)


class PinnedConfError(RuntimeError):
    """Raised when the session cannot be put into the pinned configuration."""


@dataclass(frozen=True)
class S3Settings:
    """Connection settings for an S3-compatible object store (SeaweedFS locally, S3 in AWS)."""

    endpoint: str
    access_key: str
    secret_key: str = field(repr=False)
    region: str = "us-east-1"
    path_style: bool = True

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ, *, read_only: bool = False) -> S3Settings | None:
        endpoint = environ.get("CLOSE_S3_ENDPOINT")
        if not endpoint:
            return None
        prefix = "CLOSE_S3_READONLY_" if read_only else "CLOSE_S3_"
        try:
            return cls(
                endpoint=endpoint,
                access_key=environ[f"{prefix}ACCESS_KEY"],
                secret_key=environ[f"{prefix}SECRET_KEY"],
                region=environ.get("CLOSE_S3_REGION", "us-east-1"),
            )
        except KeyError as exc:
            raise PinnedConfError(f"CLOSE_S3_ENDPOINT is set but {exc.args[0]} is missing") from None

    def hadoop_conf(self) -> dict[str, str]:
        ssl = self.endpoint.startswith("https://")
        return {
            "spark.hadoop.fs.s3a.impl": "org.apache.hadoop.fs.s3a.S3AFileSystem",
            "spark.hadoop.fs.s3a.endpoint": self.endpoint,
            "spark.hadoop.fs.s3a.endpoint.region": self.region,
            "spark.hadoop.fs.s3a.path.style.access": str(self.path_style).lower(),
            "spark.hadoop.fs.s3a.connection.ssl.enabled": str(ssl).lower(),
            "spark.hadoop.fs.s3a.aws.credentials.provider": "org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider",
            "spark.hadoop.fs.s3a.access.key": self.access_key,
            "spark.hadoop.fs.s3a.secret.key": self.secret_key,
        }


@dataclass(frozen=True)
class LocalSparkOptions:
    """How to build a local (non-Databricks) session."""

    app_name: str = "close-engine"
    master: str = "local[4]"
    driver_memory: str = "2g"
    shuffle_partitions: int = 8
    s3: S3Settings | None = None
    extra_conf: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, app_name: str, environ: Mapping[str, str] = os.environ) -> LocalSparkOptions:
        return cls(
            app_name=app_name,
            master=environ.get("CLOSE_SPARK_MASTER", "local[4]"),
            driver_memory=environ.get("CLOSE_SPARK_DRIVER_MEMORY", "2g"),
            shuffle_partitions=int(environ.get("CLOSE_SPARK_SHUFFLE_PARTITIONS", "8")),
            s3=S3Settings.from_env(environ),
        )


def bundled_hadoop_version() -> str:
    """Hadoop version bundled in the pyspark wheel; hadoop-aws must match it exactly."""
    import pyspark

    jars = Path(pyspark.__file__).parent.joinpath("jars")  # joinpath: `/` is banned in engine code (SB008)
    for jar in jars.glob("hadoop-client-api-*.jar"):
        m = re.search(r"hadoop-client-api-(\d+\.\d+\.\d+)\.jar$", jar.name)
        if m:
            return m.group(1)
    raise PinnedConfError(f"cannot find hadoop-client-api jar in {jars}")


def on_databricks(environ: Mapping[str, str] = os.environ) -> bool:
    return "DATABRICKS_RUNTIME_VERSION" in environ


def apply_pinned_conf(spark: SparkSession) -> dict[str, str]:
    """Set every pinned value, read it back, and fail if any did not take effect."""
    for key, value in PINNED_CONF.items():
        spark.conf.set(key, value)
    effective = {key: str(spark.conf.get(key)) for key in PINNED_CONF}
    wrong = {k: v for k, v in effective.items() if v.lower() != PINNED_CONF[k].lower()}
    if wrong:
        raise PinnedConfError(f"pinned Spark configuration did not take effect: {wrong}")
    return effective


def build_local_session(opts: LocalSparkOptions) -> SparkSession:
    """Build a local SparkSession with Delta Lake (and S3A when ``opts.s3`` is set)."""
    from delta import configure_spark_with_delta_pip

    builder = (
        SparkSession.builder.master(opts.master)
        .appName(opts.app_name)
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        .config("spark.sql.catalog.spark_catalog", "org.apache.spark.sql.delta.catalog.DeltaCatalog")
        .config("spark.driver.memory", opts.driver_memory)
        .config("spark.sql.shuffle.partitions", str(opts.shuffle_partitions))
        .config("spark.ui.enabled", "false")
        .config("spark.ui.showConsoleProgress", "false")
    )
    for key, value in PINNED_CONF.items():
        builder = builder.config(key, value)
    extra_packages: list[str] = []
    if opts.s3 is not None:
        extra_packages.append(f"org.apache.hadoop:hadoop-aws:{bundled_hadoop_version()}")
        for key, value in opts.s3.hadoop_conf().items():
            builder = builder.config(key, value)
    for key, value in opts.extra_conf.items():
        builder = builder.config(key, value)
    spark = configure_spark_with_delta_pip(builder, extra_packages=extra_packages).getOrCreate()
    apply_pinned_conf(spark)
    return spark


def launched_by_spark_submit(environ: Mapping[str, str] = os.environ) -> bool:
    """True when a JVM launcher (spark-submit, a k8s Job) started this Python driver.

    PySpark sets ``PYSPARK_GATEWAY_PORT`` for a driver whose JVM already exists. In that case the
    launcher owns master, jars and static configuration, and the engine must not override them
    (a builder ``master`` or ``spark.jars.packages`` set here would silently win or be ignored).
    """
    return "PYSPARK_GATEWAY_PORT" in environ


def get_session(app_name: str = "close-engine") -> SparkSession:
    """Return the session for this process, with the pinned configuration asserted.

    * Databricks, or launched by spark-submit / a k8s Job: reuse the launcher's session as configured.
    * A plain local Python process (tests, ``python -m``): build a local session with Delta enabled.
    """
    spark = SparkSession.getActiveSession()
    if spark is None:
        if on_databricks() or launched_by_spark_submit():
            spark = SparkSession.builder.getOrCreate()
        else:
            spark = build_local_session(LocalSparkOptions.from_env(app_name))
    apply_pinned_conf(spark)
    return spark
