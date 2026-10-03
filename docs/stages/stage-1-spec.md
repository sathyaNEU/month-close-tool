# Stage 1 (re-specified): local Databricks-shaped platform, in two gated parts

> The approved master plan is `docs/plan/ROADMAP.md`. It is amended by ADR-0006 (Databricks App + Lakebase + Lakeflow Jobs, no Kubernetes, no Temporal), which you chose on 2026-09-27. This file re-specifies **Stage 1 only**. A principal-engineer review has been applied: 7 must-fixes and 8 should-fixes.

## Context

With 7 users, all accountants, production is a Databricks App backed by Lakebase, with the engine running as Lakeflow jobs. The original Stage 1, a kind cluster with Temporal, Keycloak and k8s Jobs, is obsolete. The new Stage 1 builds the local platform in the same shape as production:
- an app behind an identity proxy;
- Postgres standing in for Lakebase;
- a runner that speaks the Databricks Jobs API;
- Spark Connect for reads;
- SeaweedFS for storage.

It also builds, and proves under fault injection, the guarantees Temporal used to provide: one writer at a time, idempotent submission, a transactional outbox, and a scheduler that is safe to run on several app instances.

The stage is split into **1a (write path and orchestration)** and **1b (read path, catalog and CI)**. Each part has its own gate, report and sign-off, because together they are too big for one gate to test thoroughly.

These Stage 0 pieces are reused unchanged:
- `close_contracts`: IDs, money, `LogicalTable`, and the resolvers.
- `close_engine`: `session.get_session`/`launched_by_spark_submit`, `io/digest`, `io/tables`, `io/sql.run_ddl`, `jobs/smoke.py`, `RESULT_PREFIX` and `engine/scripts/run_job.py`.
- The tools: `run_gate.py`, `lint_spark_bans.py`, `wheel_smoke.py`, `check_versions.py`.
- `compose.test.yaml`.

## Stage 0 sign-off (approving this plan also signs off Stage 0)

| Item | What you approve |
|---|---|
| ADR-0002 / ADR-0005 | The engine never binds SQL parameters (delta-io/delta#7640), and the read path uses typed query specs |
| ADR-0003 | SeaweedFS replaces MinIO, which was archived on 2026-04-25 |
| Databricks CLI | Deferred to Stage 10. It needs a `brew trust` of the databricks tap, which will need your go-ahead then |
| Waiver PYSEC-2026-113 (pyarrow) | Expires 2026-12-31. **Please name an owner** (default: platform engineering lead) |

Memory note: Stage 1 fits the current 7.75 GB Docker VM, with a gate at 6.5 GiB. The Stage 3 matching job alone needs about 4 GB, so plan to raise Docker to about 10 GB **before Stage 3**, not now.

## Design rules added by the review (these replace the draft's weaker versions)

1. **Leases have no timer.** The holder is a run *attempt*, and every grant or re-grant (retry) increments `fencing_token`. A lease is released only in the same Postgres transaction that records the attempt as terminal. That means `status.state == TERMINATED`, plus a 10-minute quarantine after `INTERNAL_ERROR` or `CLOUD_FAILURE` terminations. If the Jobs API is unreachable, the status becomes `UNKNOWN`: nothing moves, the lease stays held, and new starts get a 409. The only way out is an audited break-glass release, which also bumps the token. Every compared timestamp is Postgres `now()`.
2. **Fencing detects stale writers; the lease prevents them.**
   - A job's first action is to append a claim to `ops.writer_claims`. It checks for a higher token before every commit and once after the last, and reports `fenced` if it finds one.
   - The prevention itself is the Postgres grant, which happens only after the previous attempt is terminal.
   - Jobs run with `max_retries: 0`; retries belong to the app.
   - Only the app's service principal can run the engine jobs, and people get view access only. This removes the realistic zombie source, a "Repair run" with stale parameters.
   - The remaining check-to-commit window is written up in the S5' ADR and pinned by a strict-xfail test.
   - Every Delta commit records `userMetadata` (`run_id`, `attempt`, `fencing_token`) for audit, which adds an 18th feature to the parity allowlist.
3. **Only the outbox relay submits jobs.**
   - In one transaction the request stores its `Idempotency-Key`, acquires the lease, inserts the run and attempt, and publishes `run.submit`. Request handlers make no external calls.
   - The relay calls `run-now` with the attempt's token, `close-{env}-{job_key}-{run_id_hex}-a{NN}` (64 characters at most).
   - How long Databricks remembers an idempotency token is not documented, so we assume it is short-lived. After an hour, `find()` searches `runs/list` by `run_id` and `attempt` parameters. The emulator has a `TOKEN_TTL` knob to test this.
   - Run states: `requested → submitted → running → succeeded | failed | cancelled`, plus `cancel_requested` and `submit_failed`. Every transition is a compare-and-set.
4. **The scheduler claims per-task rows instead of taking advisory locks.** A pooled connection keeps a session-level advisory lock after it returns to the pool, and loses it silently when the connection is recycled. So each periodic task runs only after the instance wins a compare-and-set on `scheduler_tasks.next_due_at` (Postgres time). All tasks are idempotent, so a lost claim delays work by one interval but never duplicates it. The S5' ADR records this change to ADR-0006.
5. **Entity scoping is tested from day one.**
   - The smoke job writes `entity_id` (US01/EU01) and reports a digest per entity.
   - A deny-by-default `principal_entities` table maps users to entities.
   - The principal is keyed on the stable `X-Forwarded-User`. Email and display name are for display only.
6. **Least privilege, even locally.**
   - Each service gets its own secrets file: `s3-writer.env` for `jobs` only, `s3-reader.env` for `spark-connect`, `pg-migrator.env` and `pg-app.env` for Postgres. The app gets no S3 keys.
   - Spark Connect has no authentication by default, so it stays on the backend network and is never published.
7. **Packaging parity.**
   - `pyspark` and `pyspark-client` both install the `pyspark` package, so they are mutually exclusive uv groups (`[tool.uv] conflicts`).
   - The Apps runtime defaults to Python 3.11. `make app-dist` therefore builds exactly what we will deploy (source, `app.yaml`, a `pyproject` requiring 3.12, hashed requirements from `uv.lock`, and the workspace wheels), and the `close-app` image installs only that artifact.

## Stage 1a: write path and orchestration (gate: `make gate-1a`)

**Compose services** (only `devproxy` is published, on 127.0.0.1):

| Service | Stands in for | mem_limit | Notes |
|---|---|---|---|
| `devproxy` | Databricks Apps proxy | 64m | Official Caddy image, pinned by digest. It strips the six Apps headers, `Forwarded` and `X-Forwarded-Access-Token` from clients, then sets identity from an HttpOnly dev cookie (`/__dev/login/<user>`) and `X-Request-Id` from a UUID |
| `app` (+`app2`, profile `ha`) | Databricks App | 512m each | FastAPI in sync mode, uvicorn on `$DATABRICKS_APP_PORT`, reads the `PG*` environment variables. Rejects requests that are missing or duplicate identity headers |
| `migrate` (one-shot) | deploy step | 256m | Alembic as `close_migrator`. Migrations are expand/contract, and the app's readiness fails unless the schema is at head |
| `postgres` | Lakebase | 384m | `postgres:17.11`, separate migrator and app roles, statement, lock and idle timeouts set |
| `jobs` | Lakeflow Jobs | 2560m | Jobs API 2.2 emulator (see below). Runs one `spark-submit` at a time with a 1g driver |
| `seaweedfs` | S3 | 768m | As in Stage 0, with separate writer and reader identities |
| `faults` (profile) | zombie writer | 1792m | Used only by the fencing tests |

**Jobs API emulator** (`services/jobs_emulator/`). This is the smallest subset the real `databricks-sdk` needs, so production and local share one client code path.
- **Endpoints:**
  - `POST /api/2.2/jobs/run-now`;
  - `GET runs/get`, returning `state` and `status` with `termination_details` and `tasks[].run_id`;
  - `POST runs/cancel`;
  - `GET runs/get-output`, keyed by task run ID;
  - `GET runs/list`;
  - `GET jobs/get`;
  - the SDK's host-metadata probe;
  - optionally the OIDC token endpoints, so local auth uses the same M2M path the App uses.
- **Errors** are `{error_code, message}` with the correct HTTP status.
- **State** is kept in SQLite on a volume; on restart, orphaned runs are marked `INTERNAL_ERROR`.
- **Execution.** Each run gets its own process group. Cancel sends SIGTERM, then SIGKILL after 20 s. `timeout_seconds` is enforced, stdout is reserved for the result line, and `init: true` reaps zombie processes.
- **Job definitions** come from `engine/databricks.yml`. They are parsed with a strict pydantic subset (parameters, one `python_wheel_task`, timeout, `max_concurrent_runs`, queue, `max_retries`), and unknown keys are rejected.
- The app reads the job ID from `CLOSE_JOB_ID_SMOKE`, the same environment variable a Databricks App job resource provides.

**Libraries** (`libs/platform/src/close_platform/`, all sync, SQLAlchemy 2 + psycopg 3):
- `db/engine.py` and `db/credentials.py`: a `PgCredentialProvider` with two implementations. `StaticCredentialProvider` refuses to run inside Apps. `LakebaseOAuthProvider` is a stub with tests in 1a and is completed in Stage 10.
- `runs.py`, `leases.py`, `outbox.py` (claim TTL, backoff, dead-letter, FIFO per aggregate, consumer receipts table) and `scheduler.py`.
- `compute/{base,databricks,fake}.py`: `ComputeBackend.submit/find/observe/cancel/result/ensure_terminal`. `observe` returns `UNKNOWN` on transport errors and never raises. `ensure_terminal` never assumes a run is terminal.

**Engine changes:**
- `engine/src/close_engine/runctx.py`: run context from the job parameter contract.
- `io/fence.py`: writer claims and checks.
- `faults.py`: pause points, active only when `CLOSE_FAULTS` is set and never on Databricks.
- `smoke.py`: `entity_id`, digests by entity, the fence, commit `userMetadata`.
- `engine/databricks.yml`: the smoke job, with `max_retries: 0`.

**App** (`services/app/`):
- `app.yaml`;
- `/healthz` and `/readyz` (schema at head, dependencies reachable);
- `/api/v1/whoami`;
- `POST /api/v1/smoke/runs` (Idempotency-Key, 202, 409 while busy or UNKNOWN);
- `GET /api/v1/smoke/runs/{id}`. In 1a it returns the job's recorded result, filtered to the caller's entities.
- JSON logs carrying the request ID, and RFC 9457 errors.

**Job parameter contract.** All values are strings, mapped in the bundle as `{{job.parameters.x}}`:
- `run_id`: 32 hex characters;
- `attempt`: 1 to 99;
- `lease_resource`: `^[a-z][a-z0-9_]{0,31}:[A-Za-z0-9_.-]{1,96}$`;
- `fencing_token`: int64, at least 1, one per attempt;
- `requested_at`: Postgres time, RFC 3339 UTC;
- `root_uri`;
- `request_id`;
- job-specific parameters (smoke: `rows`).

The result line is `CLOSE_JOB_RESULT {run_id, attempt, fencing_token, fenced, rows, digests_by_entity, table, engine_version, spark_master}`.

**Postgres tables** (Alembic, with the full DDL in the migration): `principal_entities`, `runs`, `run_attempts` (fencing token, unique idempotency token of 64 characters at most, external run ID, quarantine), `leases` (never deleted), `lease_events` (the app may only insert and select), `outbox`, `outbox_receipts`, `idempotency_keys` (principal, method, route and key, request hash, stored response, 24 h expiry), and `scheduler_tasks`.

**Spike ADRs:**
- **S5':** orchestration without Temporal. Covers leases, fencing and its residual window, the outbox and the scheduler claims, and amends ADR-0006.
- **S6':** Jobs API emulator parity. Covers the SDK against the emulator, the subset served, bundle parsing and token time-to-live behaviour.

### Gate 1a (pass/fail)

- **Bring-up.** Cold `make up` takes 10 minutes or less and warm 2 minutes or less. "Cold" means no project images and a pruned build cache, building for the native architecture only; the bytes downloaded are recorded. Three `down`/`up` cycles end with every service healthy and data kept.
- **Smoke.** 10 of 10 end-to-end runs pass through `devproxy`, each in 120 s or less. alice (US01) gets exactly the US01 digest, bob (EU01) the EU01 digest, and a user with no grants gets 0 rows.
- **Idempotent submit.** 20 redeliveries, including an app restart and a relay kill right after `run-now`, give 1 run per token. With `TOKEN_TTL=5s`, the `find()` path creates 0 duplicates.
- **Single writer.** 20 parallel starts give one 202 and nineteen 409s, and the token rises by exactly 1.
- **Hand-over.** At 5 kill points, the new grant always comes after the old attempt is TERMINATED (in Postgres time). With the emulator returning 503, nothing transitions and the lease stays held.
- **Zombie.** A job holding a stale token aborts and output digests stay unchanged. The residual window is a strict xfail.
- **Cancel and timeout.** A cancelled run is TERMINATED within 30 s. A timed-out run ends within `timeout_seconds` + 10 s. No orphan processes remain.
- **Outbox.** 100 events across 3 crash points give 0 lost and 0 double-applied.
- **Scheduler.** With 2 app instances over 100 intervals, every (task, due time) runs exactly once. After `SIGKILL` or `docker pause` of either instance, the survivor picks up within one interval plus 1 s.
- **Identity.**
  - A request with no proxy headers gets 401.
  - Every spoof variant yields only the proxy's identity. The variants are case changes, duplicate headers, comma-joined values, underscore names, `Forwarded`, and HTTP/1.0 and h2c requests.
  - Only `devproxy` is published.
- **Least privilege.** `docker compose config` shows the S3 writer key only under `jobs`, and the app has no S3 keys.
- **Migrations and bundle.**
  - Migrations run up, down and up again cleanly.
  - Readiness enforces the schema head.
  - The bundle parser rejects unknown keys and target overrides.
- **Memory.** The sum over containers of cgroup `memory.peak` stays at 6.5 GiB or less, with `oom_kill` = 0 in every container.
- **Regression and security.**
  - The Gate 0 steps still pass.
  - The parity allowlist includes Delta commit `userMetadata`.
  - bandit, pip-audit and gitleaks are clean.

## Stage 1b: read path, catalog and CI (gate: `make gate-1b`)

- **`spark-connect`** (1792m) runs in the foreground under tini:
  - `spark-submit --class org.apache.spark.sql.connect.service.SparkConnectServer`, with `local[2]`, a 1g driver, the FAIR scheduler, Delta extension and catalog, `PINNED_CONF`, and the reader-only S3A settings.
  - Jars are resolved when the image is built and pinned in `versions.lock` by name and SHA-256; nothing is fetched when the container starts.
  - The app side:
    - one remote session per process;
    - a semaphore of 4;
    - a tag and interrupt per request for timeouts;
    - `toArrow()` for results;
    - the session is rebuilt when it closes;
    - readiness checks the server version (4.1.3) and the pinned settings.
- **QuerySpec** (`libs/contracts/.../query.py`, stdlib frozen dataclasses):
  - Filters: `Eq`, `In` (200 values or fewer), `Range`, `Prefix` (compiled to `startswith`).
  - Sort keys must be non-nullable and end in a unique key.
  - Simple `count`/`sum` aggregates.
  - Pages hold 500 rows or fewer, and grouped results 1,000 or fewer.
  - The cursor is HMAC-signed and binds the spec hash and the page-1 Delta version; later pages read with `versionAsOf`.
  - The `QueryEngine` always adds the principal's entity filter.
  - A pure-Python reference evaluator serves as the oracle.
  - The lint rules SB009 to SB011 extend to `close_platform/query`.
- **SQL renderer** for the future SQL warehouse: named markers, backticked identifiers validated by the resolver. It is tested **now** on plain Spark 4.1.3 without Delta, where parameter binding works (the positive-control canary), against a Parquet copy of the data.
- **The smoke GET switches to a QuerySpec over Spark Connect.**
- **S1 catalog spike**, time-boxed to 2 days: the `+uc` profile (UC OSS 0.6.0) exercises `CatalogTableResolver` (`forName`, `saveAsTable`, time travel through `.table()`). "Inconclusive, defer to Stage 10" is an acceptable outcome, because UC OSS is not Databricks Unity Catalog.
- **CI:**
  - `ci.yml` (mirrors `make ci`) and `e2e.yml` (the Compose smoke flow).
  - Actions pinned to 40-character SHAs, `permissions: {}` and `persist-credentials: false`.
  - Checked with actionlint and zizmor. `e2e.yml` is labelled "not yet executed" until a GitHub remote exists.
- **trivy** gates our own images and `trivy config` (`--ignore-unfixed`, with expiring waivers), and produces a CycloneDX SBOM. Third-party images are reported but not gated.

### Gate 1b (pass/fail)

- **Read path.** With the data at 100k rows, 3 specs are called 5 times to warm up and then 100 times each, and p95 must be 1.5 s or less. The 1M-row numbers and results with 4 concurrent clients are recorded as a baseline.
- **Entity filter.** 2,000 generated specs return 0 rows outside the principal's entities.
- **Renderer.**
  - Over 10,000 generated specs with random sentinel values, no user-supplied value ever appears in the SQL text.
  - Markers and bindings match one to one.
  - sqlglot parses every statement in the Databricks dialect.
  - The entity predicate is a top-level conjunct.
  - 300 specs return identical rows on plain Spark, the DataFrame backend and the reference evaluator.
- **Smoke.** 10 of 10 runs pass, with the per-entity digest read back through a QuerySpec.
- **Least privilege.** A write through Spark Connect fails with 403.
- **Catalog.** The S1 ADR is written, with its results or an explicit "inconclusive".
- **CI.** Every `uses:` is pinned to a SHA, and actionlint and zizmor are clean.
- **Security.** trivy: 0 unwaived fixable critical or high findings, and the SBOM exists.
- **Memory.** 6.5 GiB or less, as in 1a.
- **Regression.** The Gate 0 and Gate 1a steps still pass.
- **ADRs.** The S3 ADR (final QuerySpec format, amending ADR-0005) and the S1 ADR.

## Verification

- `make up`, then `make smoke`, then `make gate-1a` (or `gate-1b`). Each gate runs every criterion above as pytest suites and tools, and writes `docs/stages/stage-1a-report.md` (or `stage-1b-report.md`) with the real output.
- Fault injection (kills, pauses, 503s, relay crashes, zombie writers) is code, so it runs on every gate.
- I stop after Gate 1a for your sign-off, and again after Gate 1b.
