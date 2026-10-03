# Close platform — developer entry points. `make help` lists targets.
# Every gate target mirrors what CI runs, so a green `make gate-N` locally means a green pipeline.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

BREW_PREFIX ?= /opt/homebrew
export PATH := $(BREW_PREFIX)/bin:$(HOME)/.local/bin:$(PATH)
ifeq ($(shell uname -s),Darwin)
export JAVA_HOME := $(BREW_PREFIX)/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
else
export JAVA_HOME ?= /usr/lib/jvm/java-21  # CI: actions/setup-java sets JAVA_HOME, which wins
endif
export HOMEBREW_NO_AUTO_UPDATE := 1
export HOMEBREW_NO_INSTALL_CLEANUP := 1
export HOMEBREW_NO_ENV_HINTS := 1
export PYSPARK_PYTHON := $(CURDIR)/.venv/bin/python
export PYSPARK_DRIVER_PYTHON := $(CURDIR)/.venv/bin/python

UV := uv
RUN := $(UV) run --frozen
COMPOSE := docker compose -f compose.test.yaml
STACK := docker compose
E2E_STACK := docker compose -f compose.yaml -f compose.e2e.yaml
BUILD_FLAGS ?=
PYTEST_ARGS ?=
PY_SRC := libs/contracts/src libs/platform/src engine/src datagen/src services/app/src services/jobs_emulator/src
S3_ENV := .local/secrets/s3.env
# Gates run for about an hour: keep the Mac awake (macOS's built-in caffeinate), because a sleep freezes the
# containers mid-test and breaks the wall-clock checks (it happened in the fourth Stage 4 gate run).
KEEP_AWAKE := $(shell command -v caffeinate >/dev/null 2>&1 && echo caffeinate -i)
GITLEAKS_IMAGE := ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f
TRIVY_IMAGE := aquasec/trivy:0.74.0@sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969
ACTIONLINT_IMAGE := rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667

.PHONY: gate-8b gate-8a gate-7c gate-7b gate-7a gate-6-ref gate-4 gate-3-ref gate-2b gate-2a datagen-determinism datagen-stack test-reference help bootstrap secrets check-versions lint format typecheck vectors test-unit test-spark \
        wheel-smoke compose-up compose-down up-pg test-platform test-app test-emulator test-parity security ci \
        app-dist spark-dist images up down down-v smoke test-e2e lint-workflows scan read-benchmark uc-spike gate-0 \
        gate-1a \
        gate-1b clean

help: ## List targets
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

bootstrap: ## Install/verify toolchain, sync the Python env, generate local secrets, install git hooks (idempotent)
	brew bundle check --file=Brewfile --no-upgrade >/dev/null 2>&1 || brew bundle install --file=Brewfile --no-upgrade
	$(UV) python install 3.12 >/dev/null
	$(UV) sync --all-packages --locked
	$(RUN) python tools/gen_local_secrets.py
	$(RUN) pre-commit install --install-hooks >/dev/null
	$(RUN) python tools/check_versions.py

secrets: ## Generate local-only credentials under .local/secrets (never overwrites)
	$(RUN) python tools/gen_local_secrets.py

check-versions: ## Verify tools and Python packages against tools/versions.lock
	$(RUN) python tools/check_versions.py

lint: ## ruff, format check, banned-Spark-API lint, stale test vectors
	$(RUN) ruff check .
	$(RUN) ruff format --check .
	$(RUN) python tools/lint_spark_bans.py
	$(RUN) python tools/gen_test_vectors.py --check

format: ## Auto-format and apply safe lint fixes
	$(RUN) ruff format .
	$(RUN) ruff check --fix .

typecheck: ## mypy --strict on every package, the migrations and tool code
	$(RUN) mypy $(PY_SRC) services/app/migrations tools

vectors: ## Regenerate shared test vectors (review the diff!)
	$(RUN) python tools/gen_test_vectors.py

test-unit: ## Pure-Python tests incl. the generator (contracts coverage >= 90%)
	$(RUN) pytest libs/contracts/tests datagen/tests tools/tests -q --cov=close_contracts --cov-branch --cov-fail-under=90 --cov-report=term $(PYTEST_ARGS)

test-reference: ## Pure-Python reference matcher: conditions, subset search, waterfall, template golden tests
	$(RUN) pytest engine/tests/reference -q --cov=close_engine.reference --cov-branch --cov-fail-under=90 --cov-report=term $(PYTEST_ARGS)

test-spark: ## Host Spark tests: shared vectors, digests, canaries, notebooks-as-scripts, S4 entrypoint parity
	$(RUN) pytest engine/tests/spark engine/tests/notebooks engine/tests/s4 -q -m spark -rxX $(PYTEST_ARGS)

wheel-smoke: ## Build both wheels, install into a clean venv, run close-job smoke, compare digest with dev env
	$(RUN) python tools/wheel_smoke.py

compose-up: secrets ## Start test dependencies (SeaweedFS) and wait until healthy
	$(COMPOSE) up -d --wait

compose-down: ## Stop test dependencies and delete their volumes
	$(COMPOSE) --profile pg down -v

up-pg: secrets ## Start the test Postgres (127.0.0.1:55432) as well as the test object store
	$(COMPOSE) --profile pg up -d --wait

test-platform: up-pg ## Control-plane library against the test Postgres: leases, outbox, scheduler, coordinator
	$(RUN) pytest libs/platform/tests -q -m "not slow" --timeout 300 -p no:cacheprovider $(PYTEST_ARGS)

test-app: up-pg ## The FastAPI app against the test Postgres with a scripted Jobs backend
	$(RUN) pytest services/app/tests -q --timeout 300 -p no:cacheprovider $(PYTEST_ARGS)

test-emulator: ## Jobs API emulator driven by the official databricks-sdk (runs fake jobs, no Spark)
	$(RUN) pytest services/jobs_emulator/tests -q --timeout 300 -p no:cacheprovider $(PYTEST_ARGS)

test-parity: compose-up ## S2: Delta feature allowlist on the S3-compatible store, 3 consecutive runs
	set -a; source $(S3_ENV); set +a; export CLOSE_TEST_S3=1; \
	for i in 1 2 3; do \
	  echo "== parity run $$i/3 =="; \
	  $(RUN) pytest engine/tests/parity -q -m parity -p no:cacheprovider || exit 1; \
	done

app-dist: ## Build dist/app: the Databricks App deploy artifact (the close-app image installs only this)
	$(RUN) python tools/build_dist.py app

spark-dist: ## Build dist/spark: engine + jobs emulator runtime (the close-spark image installs only this)
	$(RUN) python tools/build_dist.py spark

images: secrets app-dist spark-dist ## Build the close-app and close-spark images (native architecture)
	$(STACK) build $(BUILD_FLAGS) app jobs

up: images ## Start the local stack and wait until healthy (devproxy: http://127.0.0.1:8088)
	mkdir -p .local/faults
	$(STACK) up -d --wait

down: ## Stop the local stack, keeping its data volumes
	$(STACK) --profile ha --profile faults down

down-v: ## Stop the local stack and delete its data volumes
	$(STACK) --profile ha --profile faults down -v

smoke: ## One smoke run through devproxy as alice (make up first)
	$(RUN) python tools/stack_smoke.py

test-e2e: ## Fault-injection suite against the stack, with the e2e timing overrides (compose.e2e.yaml)
	mkdir -p .local/faults
	$(E2E_STACK) up -d --wait
	CLOSE_E2E=1 $(RUN) pytest tests/e2e -q -m stack --timeout 1800 -p no:cacheprovider -rA $(PYTEST_ARGS)

lint-workflows: ## actionlint + zizmor over .github/workflows (SHA pins, permissions, injection)
	docker run --rm -v "$(CURDIR)":/repo:ro -w /repo $(ACTIONLINT_IMAGE) -color
	$(RUN) zizmor --offline --min-severity low .github/workflows

scan: ## trivy: gate our images and Dockerfiles (policy in tools/run_trivy.py), write CycloneDX SBOMs
	$(RUN) python tools/run_trivy.py $(TRIVY_IMAGE)

uc-spike: ## Spike S1: the engine's Unity Catalog paths against UC OSS 0.6.0 (compose.uc.yaml)
	$(RUN) python tools/run_uc_spike.py

read-benchmark: ## Read-path latency through devproxy (100k rows p95 <= 1.5 s; 1M and 4 clients recorded)
	$(RUN) python tools/measure_read_path.py

security: ## bandit, pip-audit (installed env), gitleaks (tracked + untracked-not-ignored files)
	# Low findings are subprocess calls with fixed argv (no shell) in dev tools; gate on medium+.
	$(RUN) bandit -q -c pyproject.toml --severity-level medium --confidence-level medium -r $(PY_SRC) services/app/migrations tools
	$(RUN) python tools/run_pip_audit.py
	git ls-files -co --exclude-standard -z | tar --null -cf - -T - | \
	  docker run --rm -i --entrypoint sh $(GITLEAKS_IMAGE) -c \
	  'mkdir -p /scan && tar -xf - -C /scan && gitleaks dir /scan --redact --no-banner --exit-code 1'

ci: check-versions lint typecheck lint-workflows test-unit test-reference test-spark wheel-smoke test-parity test-platform test-app \
    test-emulator security images scan ## Everything CI runs (ci.yml), in CI order

datagen-stack: ## Gate 2b through the stack: ddl_apply <= 60 s and rerun 0; small <= 3 min; medium <= 25 min
	$(RUN) python tools/measure_datagen.py

datagen-determinism: ## Gate 2b: seeds 11/23/47 x 3 runs, local[1] vs local[4], identical landing digests
	$(RUN) python tools/datagen_determinism.py

gate-8b: ## Stage 8b: every 8a step as regression + the soft close, the close checklist, flux, the hard close, stack
	$(KEEP_AWAKE) $(RUN) python tools/run_gate.py 8b

gate-8a: ## Stage 8a: every 7c step as regression + the journaling stand-in, balances, the ERP tie-out, stack
	$(KEEP_AWAKE) $(RUN) python tools/run_gate.py 8a

gate-7c: ## Stage 7c: every 7b step as regression + D9 at submit, narrow rates, functional terms, V008, stack
	$(KEEP_AWAKE) $(RUN) python tools/run_gate.py 7c

gate-7b: ## Stage 7b: every 7a step as regression + posting exactly once, ERP write-back, ledger reads, XLSX, stack
	$(KEEP_AWAKE) $(RUN) python tools/run_gate.py 7b

gate-7a: ## Stage 7a: every 6-ref step as regression + journal states, SoD, routing, periods, evidence, stack
	$(KEEP_AWAKE) $(RUN) python tools/run_gate.py 7a

gate-6-ref: ## Stage 6 first slice: every Stage 4 step as regression + journaling engine = oracle, invariants, config APIs
	$(KEEP_AWAKE) $(RUN) python tools/run_gate.py 6-ref

gate-4: ## Stage 4 gate: every 3-ref step as regression + authorization matrix, SoD, audit chain, maker-checker
	$(KEEP_AWAKE) $(RUN) python tools/run_gate.py 4

gate-3-ref: ## Stage 3 reference slice: every 2b step as regression + reference matcher, invariants, template scores
	$(KEEP_AWAKE) $(RUN) python tools/run_gate.py 3-ref

gate-2b: ## Stage 2b gate: every 2a step as regression + migrations, table contracts, generator, answer key
	$(KEEP_AWAKE) $(RUN) python tools/run_gate.py 2b

gate-2a: ## Stage 2a gate: every 1b step as regression + schema drift, DSL, config and Spark-semantics criteria
	$(KEEP_AWAKE) $(RUN) python tools/run_gate.py 2a

gate-1b: ## Stage 1b gate: Stage 0 + 1a steps + read path, catalog, CI, trivy; writes docs/stages/stage-1b-report.md
	$(KEEP_AWAKE) $(RUN) python tools/run_gate.py 1b

gate-1a: ## Stage 1a gate: Stage 0 steps + platform/app/emulator/e2e suites; writes docs/stages/stage-1a-report.md
	$(KEEP_AWAKE) $(RUN) python tools/run_gate.py 1a

gate-0: ## Stage 0 gate: bootstrap idempotency + ci + ADR checks; writes docs/stages/stage-0-report.md
	$(KEEP_AWAKE) $(RUN) python tools/run_gate.py 0

clean: ## Remove caches and build artefacts (keeps .venv and secrets)
	rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis htmlcov .coverage dist build spark-warehouse
