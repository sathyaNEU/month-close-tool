## What Stage 1a delivers

The local stack now has the same shape as production, and `make up` starts all of it. devproxy stands in for the Databricks Apps proxy and is the only published port. The FastAPI app runs the way a Databricks App will. Postgres 17 stands in for Lakebase, a Jobs API 2.2 emulator runs the engine with `spark-submit` the way Lakeflow Jobs will, and SeaweedFS stands in for S3. The app talks to the emulator through the official `databricks-sdk` with machine-to-machine OAuth, so the client code that decides when a run is finished is the production code path.

The guarantees Temporal used to give are rebuilt and proven under fault injection ([ADR-0007](../adr/0007-orchestration-without-temporal.md)). A writer lease with a fencing token allows one writer at a time. A transactional outbox with idempotent submission sends every job exactly once, even across crashes. A scheduler that claims each tick through a row in Postgres is safe with any number of app instances, and every result is scoped to the caller's entities from day one. The criteria table below carries the measured evidence for each gate item.

## Found and fixed while building and testing

Review and testing found real defects while the stage was being built, and each one is fixed and pinned by a test.

1. If the Jobs API was down longer than the relay's retry window, a job submission was dead-lettered and the writer lease stayed held forever. Submissions and cancellations are now never dead-lettered, because their handlers end their own retry loops.
2. Databricks does not document how long it remembers an `idempotency_token`, so the relay now looks the run up in `runs/list` before any resubmission. A duplicate would need both a missed lookup and a forgotten token.
3. After the jobs service restarted, the app kept a rejected OAuth token until it expired (the SDK refreshes only on expiry), which froze every run for up to an hour. The client is now rebuilt with a fresh token after a 401 ([ADR-0008](../adr/0008-jobs-api-emulator-parity.md)).
4. An app instance frozen in the middle of a scheduler claim held the row lock for 30 s. The claim transaction is now bounded to half a second.
5. A 500 response lost its request id, replayed idempotent responses were not byte-identical, and jobs inherited the emulator's own credentials. All three are fixed.

One operational rule came out of this, and it matters for Stage 10. Fencing tokens must never go backwards, so restoring the control-plane database from an older backup makes every job fence itself, which is safe but means nothing runs. The recovery is to raise the lease tokens above the highest claim in the lake. The suite demonstrates both halves, and Stage 10 turns the recovery into an audited command.

## Where the build differs from the approved spec

These are small, but you approved specific wording, so here they are.

1. The cold bring-up measurement rebuilds our images with `--no-cache` instead of pruning Docker's build cache, because pruning is machine-wide and would wipe the cache of every other project on this Mac.
2. "The S3 writer key only in `jobs`" has one named exception, the opt-in `faults` container, because a zombie writer is a writer. The least-privilege test asserts exactly that set.
3. The app starts with `python -m close_app`, which reads `DATABRICKS_APP_PORT` itself, instead of a uvicorn command line, so nothing depends on shell expansion in `app.yaml`.
4. Containers use Docker's built-in init (`init: true`) instead of installing tini in the image.
5. The relay searches for the run on any redelivery, not only after the 1-hour token window (finding 2 above).
6. The memory budget is judged on the sum of each container's peak, and those peaks include page cache. The container limits alone add up to about 6.4 GiB, so the check that really binds is `oom_kill = 0`, and the report prints each peak against its limit and the highest process memory seen.
7. Job permissions (only the app's service principal can run the engine jobs, people get view access) need a real workspace, so they land in Stage 10. Until then the protection is the lease, `max_retries: 0` and fencing.
8. The jar pins live in their own file, `tools/jars.lock` (Maven coordinate and SHA-256 of all 28 jars the engine resolves), rather than in `tools/versions.lock` as the spec said, because they are generated and checked by a tool (`tools/lock_jars.py --check` is a gate step).

## Gate runs

The first full gate run failed one criterion, the scheduler row, because the 100-interval test (run for the first time, since it is marked slow) expected 100 ticks in a fixed 101 s window. The next due time is set from the claim time, so a 1 s interval really runs at about 1.02 to 1.05 s and 101 s only fits about 98 ticks. The fix was to the test (a 112 s window, in the platform test and in its end-to-end twin), not to the scheduler, and this report is the run after that fix, with the missing Stage 0 steps (bootstrap twice and the seeded lint) added to the regression check.

## Needs your decision

1. Starlette 1.7 deprecates `httpx` for its test client and asks for `httpx2`, which the httpx author publishes under the pydantic organisation. I tried to add it as a development dependency, and the permission check stopped me, so the app tests still use `httpx` and print one deprecation warning. If you want it, allow it and I will add it.
2. `kind`, `helm` and `kubectl` are still in the Brewfile and `tools/versions.lock` from Stage 0, although ADR-0006 removed Kubernetes. My suggestion is to drop them from the toolchain (without uninstalling anything from your machine), unless you use them elsewhere.
3. D19 (whether the ERP or Databricks is the GL book of record) is still open, and it is needed at the Stage 5 gate.

## Carried forward

Stage 1b builds the read path (typed query specs over Spark Connect), the Unity Catalog spike, CI and image scanning, and it waits for your sign-off on this stage. Stage 10 wires the Lakebase OAuth credentials, the job permissions and per-target `app.yaml` values. It also confirms against a real workspace what the documentation leaves open: how long idempotency tokens are remembered, whether the Apps proxy overwrites client-supplied identity headers, and which termination codes a real cluster loss produces.
