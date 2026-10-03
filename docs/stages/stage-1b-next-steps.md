# Where we are, and what happens next (resume note)

## Progress checkpoint (2026-10-03, Stage 8b signed off; 8c on sample inputs next, in Claude Code in the cloud)

- **Signed off:**
  - Stages 0 to 2b;
  - the Stage 3 reference slice ([stage-3-ref-signoff.md](stage-3-ref-signoff.md));
  - Stage 4 ([stage-4-signoff.md](stage-4-signoff.md), gate 62 of 62);
  - the Stage 6 first slice ([stage-6-ref-signoff.md](stage-6-ref-signoff.md), gate 73 of 73);
  - Stage 7a ([stage-7a-signoff.md](stage-7a-signoff.md), gate 84 of 84);
  - Stage 7b ([stage-7b-signoff.md](stage-7b-signoff.md), gate 97 of 97). D19 keeps its default for now (an ERP stays the book of record), and `psycopg[binary]` is approved for the engine;
  - Stage 7c ([stage-7c-signoff.md](stage-7c-signoff.md), gate 109 of 109). Stage 7 is complete on sample inputs;
  - Stage 8a, the general ledger ([stage-8a-signoff.md](stage-8a-signoff.md), gate 118 of 118). The user accepted ADR-0019 and approved the `journal_ref` stand-in job (the reference engine as a job, on sample volumes, until the rule compiler exists). Stage 8 is split into 8a, 8b and 8c ([stage-8-plan.md](stage-8-plan.md)), each with its own gate;
  - Stage 8b, the close ([stage-8b-signoff.md](stage-8b-signoff.md), gate 128 of 128). The user accepted ADR-0020 and its contract changes.
- **Next, by the user's choice.** 8c on sample inputs: account reconciliations (profiles with auto-certification rules, risk ratings and frequency; templates 8 and 12 through a supporting-balances job; reconciling items with aging; certification with separation of duties), joining the close checklist as a system check. Add its detailed decisions and gate to `stage-8-plan.md` (approved automatically), build, run `make gate-8c`, and stop for sign-off. Revaluation of foreign-currency balances at period end is still to place in Stage 8.
- **The repository** is on GitHub at `github.com/sathyaNEU/month-close-tool` (public). The research (`reports/`, `research_notes/`) stays private and isn't in it. [CLAUDE.md](../../CLAUDE.md) holds the owner's standing rules and what runs where. A new environment should check what it can run, from `make lint typecheck test-unit test-reference` up; the stack, the end-to-end suite and the gates need Docker with about 10 GB of memory.
- **Still waiting on the user.** The real inputs:
  - Stage 6's: the signed accounting-policy sheet with its incentive-cap question, the chart of accounts and the dimensions ([stage-6-inputs.md](../domain/stage-6-inputs.md));
  - Stage 7's: the approval matrix, SoD rules, templates, recurring journals and the close calendar ([stage-7-inputs.md](../domain/stage-7-inputs.md)), and which ERP will confirm postings;
  - 7c's: the rate source feeding `ref.fx_rates`, whether a month-end entry may fall back to the last published rate, and whether pairs may triangulate;
  - 8a's: which ERP, how it exports a trial balance, and whether its account codes need a mapping for the tie-out;
  - 8b's: the close checklist (tasks, preparers, reviewers, due days, dependencies), the flux thresholds, who may soft-close and reopen periods, and the business calendars feeding `ref.calendars`.
- **Open items:**
  - the Spark rule compiler (the rest of Stage 3, most of Stage 5, and the Spark journaling job that replaces 8a's stand-in), which also blocks the engine's correction state, its entries in the journal population and GL-history risk factors;
  - the configuration-change lock and the data-source version check, both with Stage 5's hardening;
  - the Spark Connect restart defect (before Stage 10);
  - Stage 10: creating the first `platform_admin` outside local, the jobs' Lakebase access (the posting queue and configuration snapshots), a real ERP integration with its trial-balance extract, and a per-journal hash comparison in the tie-out.
- **Before a gate,** run every suite the gate runs, `make test-emulator` and the platform suite included (7b's first gate run missed a pinned job list there). Run the end-to-end suite on a fresh stack (`make down-v`, `make up`, `make test-e2e`), as the gate's cold bring-up does: the journaling stand-in's pins stay in the stack's volumes between runs.
- **Gate runs** keep the Mac awake with `caffeinate`. The lid must stay open on battery.
- **Dev users:** alice, bob, carol, dave, erin (1005, the US01 level 2 approver) and erp (1006, the stub ERP's integration client in US01). The sample close checklist names alice (1001), dave (1004) and erin (1005).

To resume in a new session, say "resume from docs/stages/stage-1b-next-steps.md".

The notes below were written earlier on 2026-09-27, right before a computer restart, and are kept for the record.

## Status

Stage 0 and Stage 1a are both signed off ([stage-0-signoff.md](stage-0-signoff.md), [stage-1a-signoff.md](stage-1a-signoff.md)), and the Stage 1a gate passed 17 of 17 ([stage-1a-report.md](stage-1a-report.md)). Stage 1b has not started. It is blocked on one answer from you, the approval of its new dependencies (below). After the final 1a gate run, three more changes were made at your request: `httpx2` was added as a development dependency (the app tests now run with no warnings, 49 passed), `kind`, `helm` and `kubectl` were removed from the Brewfile, `tools/versions.lock` and the version check (`make check-versions` is 20 of 20, and nothing was uninstalled), and the sign-off was recorded. Nothing is committed to git yet; the files are all on disk.

## After the restart

1. Start Docker Desktop and wait until it is running. The containers do not start on their own, but all their data is kept in Docker volumes.
2. In a terminal: `cd ~/Developer/Projects/financial-month-close-tool`, then `make up` (about 30 seconds) and `make smoke` (one run, about 17 seconds).
3. To look around: open `http://127.0.0.1:8088/__dev/login/alice`, then `http://127.0.0.1:8088/docs`. Users are alice (US01), bob (EU01), carol (no entities) and dave (both).
4. To continue the build, start Claude Code in the project folder and say: "resume from docs/stages/stage-1b-next-steps.md". Answer the approval question below first, or in that first message.

## The decision Stage 1b is waiting for

The approved Stage 1 plan names these, but a new third-party package needs your explicit go-ahead. Options: approve all, approve all but the Unity Catalog trial (the catalog spike is then written up as deferred to Stage 10, which the plan allows), or review first.

| For | Package or tool | Version | Why |
|---|---|---|---|
| App (deployed) | `pyspark-client` | 4.1.3 | Apache Spark's official lightweight Spark Connect client for the read path. It brings `grpcio`, `grpcio-status`, `googleapis-common-protos` and `zstandard`, and makes pandas, pyarrow and numpy (already pinned to the DBR 18 versions) app dependencies |
| Development | `pyspark[connect]` extra | 4.1.3 | The same client code path in tests, on the full pyspark we already use |
| Development | `sqlglot` | 30.20.0 | Tests only: proves the SQL we would send a Databricks SQL warehouse parses as valid Databricks SQL |
| Development | `zizmor` | 1.30.1 | Security lint for the GitHub Actions workflows |
| Docker-run tool | `rhysd/actionlint` image | 1.7.12 | Workflow syntax lint, pinned by digest like gitleaks |
| Docker-run tool | `aquasec/trivy` image | 0.74.0 | Scans our images and configs and writes the SBOM; it downloads its vulnerability database when it runs |
| Catalog trial only | `unitycatalog/unitycatalog` image + `io.unitycatalog:unitycatalog-spark_4.1_2.13` jar | 0.6.0 | The Unity Catalog OSS server and its Spark 4.1 connector, for the `+uc` spike (the old `unitycatalog-spark_2.13` artifact stops at 0.4.1) |

## Stage 1b build order (from the approved spec)

1. Add the approved dependencies. `pyspark-client` goes in an optional `connect` extra of `close-app`, declared as a uv conflict with the root `local` group (both install the `pyspark` package). `tools/build_dist.py` then allows pandas, pyarrow and numpy in the app artifact but keeps rejecting full `pyspark`, `py4j`, `delta-spark` and test packages.
2. Add a `spark-connect` service to Compose. It runs on the `close-spark` image: `spark-submit --class org.apache.spark.sql.connect.service.SparkConnectServer`, `local[2]`, 1 GB driver, FAIR scheduler, Delta extension and catalog, the pinned session settings and the reader-only S3 keys, with a 1792m limit and no published port. The server jar already ships in pyspark 4.1.3 (`spark-connect_2.13-4.1.3.jar`), so no new jar is needed.
3. Build `QuerySpec` in `libs/contracts`: `Eq`, `In` (200 values or fewer), `Range`, `Prefix`, sort keys ending in a unique key, `count` and `sum`, pages of 500 rows or fewer (1,000 grouped), an HMAC-signed cursor bound to the spec hash and the page-1 Delta version, and a pure-Python reference evaluator.
4. Build the `QueryEngine` in `close_platform/query`. It always adds the caller's entity filter. It keeps one remote session per process, runs at most 4 queries at a time, tags and interrupts each request on timeout, returns results with `toArrow()`, and rebuilds the session if it closes. Readiness checks the server version (4.1.3) and the pinned settings. Lint rules SB009 to SB011 extend to this package.
5. Build the SQL renderer for a future SQL warehouse: named markers and backticked identifiers from the resolver. Test it on plain Spark 4.1.3 without Delta (where parameter binding works) against a Parquet copy, and with `sqlglot` in the Databricks dialect.
6. Switch the smoke GET to a QuerySpec over Spark Connect.
7. Run the S1 catalog spike (`+uc` profile, time-boxed to two days): `forName`, `saveAsTable`, and time travel through `.table()`. "Inconclusive, defer to Stage 10" is an acceptable result.
8. Add CI: `.github/workflows/ci.yml` (mirrors `make ci`) and `e2e.yml` (the Compose smoke flow). Every action is pinned to a 40-character SHA, with `permissions: {}` and `persist-credentials: false`, and actionlint and zizmor must be clean. `e2e.yml` is marked "not yet executed" until a GitHub remote exists.
9. Add trivy: gate our two images and `trivy config` (fixable critical and high findings, `--ignore-unfixed`, expiring waivers), and write a CycloneDX SBOM. Third-party images are reported, not gated.
10. Write the ADRs: S3 (the final QuerySpec format, amending ADR-0005) and S1 (the catalog spike). Then write the Stage 1b summary, run `make gate-1b` (with the Stage 0 and 1a steps as regression), and stop for your sign-off.

## A risk to settle when 1b starts

Memory. `spark-connect` adds a 1792m container. The default stack's limits then add up to about 5.9 GiB, and with `app2` and the `faults` container on top they pass the 6.5 GiB budget. So the gate-1b end-to-end suite must not run the zombie test (`faults`) while `spark-connect` and `app2` are up, or Docker needs more memory than its current 7.75 GiB. The plan already says to raise Docker to about 10 GB before Stage 3, so doing that at the start of 1b is the simple option. I will propose it then rather than decide it alone.
