# syntax=docker/dockerfile:1
# close-spark: the engine runtime for the local Lakeflow Jobs stand-in (jobs emulator + spark-submit)
# and for the fault-injection zombie writer. Mirrors Databricks Runtime 18 LTS: Python 3.12, JDK 21,
# pinned pyspark/delta-spark/pandas/pyarrow/numpy, and the jars pinned in tools/jars.lock (fetched and
# hash-checked here, passed to spark-submit with --jars, so nothing is resolved at container start).
# Inputs: `make spark-dist` (dist/spark). Build context: the repository root (see .dockerignore).
ARG PYTHON_IMAGE=python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.10.2@sha256:94a23af2d50e97b87b522d3cea24aaf8a1faedec1344c952767434f69585cbf9

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS jars
COPY tools/fetch_jars.py tools/jars.lock /build/
RUN python /build/fetch_jars.py /opt/close/jars

FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JAVA_HOME=/usr/lib/jvm/close-java
# Every Debian security fix published since the pinned base image was built (your choice at Stage 7c's sign-off, after
# fixes for OpenSSL and then PCRE2 had each held up a gate). Builds on different days may differ; the CycloneDX SBOM
# records exactly what each image installed.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get -y upgrade --no-install-recommends \
 && apt-get install -y --no-install-recommends openjdk-21-jre-headless \
 && rm -rf /var/lib/apt/lists/* \
 && ln -s "$(dirname "$(dirname "$(readlink -f "$(command -v java)")")")" /usr/lib/jvm/close-java \
 && groupadd --system --gid 10001 close \
 && useradd --system --uid 10001 --gid close --create-home --home-dir /home/close close \
 && install -d -o close -g close /var/lib/close-jobs
COPY --from=uv /uv /usr/local/bin/uv
COPY dist/spark/requirements.txt /opt/close/requirements.txt
RUN uv pip install --system --no-cache --require-hashes -r /opt/close/requirements.txt
COPY --from=jars /opt/close/jars /opt/close/jars
COPY dist/spark/ /opt/close/
RUN uv pip install --system --no-cache --no-deps /opt/close/wheels/*.whl
USER close
WORKDIR /home/close
ENV EMULATOR_BUNDLE=/opt/close/engine/databricks.yml \
    EMULATOR_RUN_SCRIPT=/opt/close/engine/scripts/run_job.py \
    EMULATOR_JARS_DIR=/opt/close/jars \
    EMULATOR_STATE_DIR=/var/lib/close-jobs
EXPOSE 8080
CMD ["python", "-m", "close_jobs_emulator"]
