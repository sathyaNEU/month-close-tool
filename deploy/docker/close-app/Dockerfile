# syntax=docker/dockerfile:1
# close-app: the Databricks App (FastAPI) exactly as deployed, installed only from the `make app-dist`
# artifact (dist/app): hashed third-party requirements first, then our wheels without dependencies.
# The same image runs the one-shot `migrate` step (Alembic as close_migrator, then the dev seed).
# Build context: the repository root (see .dockerignore).
ARG PYTHON_IMAGE=python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.10.2@sha256:94a23af2d50e97b87b522d3cea24aaf8a1faedec1344c952767434f69585cbf9

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABRICKS_APP_PORT=8000
# Every Debian security fix published since the pinned base image was built (your choice at Stage 7c's sign-off, after
# fixes for OpenSSL and then PCRE2 had each held up a gate). Builds on different days may differ; the CycloneDX SBOM
# records exactly what each image installed.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get -y upgrade --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 10001 close \
 && useradd --system --uid 10001 --gid close --no-create-home close
COPY --from=uv /uv /usr/local/bin/uv
COPY dist/app/requirements.txt /opt/close-app/requirements.txt
RUN uv pip install --system --no-cache --require-hashes -r /opt/close-app/requirements.txt
COPY dist/app/ /opt/close-app/
RUN uv pip install --system --no-cache --no-deps /opt/close-app/wheels/*.whl
USER close
WORKDIR /opt/close-app
EXPOSE 8000
CMD ["python", "-m", "close_app"]
