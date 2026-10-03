# Marketplace close platform

This repository holds an in-house month-close platform for a GOAT and StockX style resale marketplace, with transaction matching and journaling at the level of BlackLine, tuned to marketplace money flows (orders, PSP captures and payouts, seller payouts, refunds, chargebacks, shipping labels, sales tax and store credit). Source data lands in Databricks landing tables and results go back to Databricks, so matching and journaling run as PySpark jobs on Lakeflow Jobs, and a backend application running as a Databricks App, with its state in Lakebase Postgres, orchestrates them through an API ([ADR-0006](docs/adr/0006-databricks-native-platform.md)).

## Where things stand

We build in stages. Each stage ends with `make gate-N`, a report in `docs/stages/` with the real command output, and a sign-off before the next stage starts.

- **Signed off.** Stages 0 to 2b, and the first slice of Stage 3 (the pure-Python reference matcher and the template match sets, scored against the generator's answer key).
- **Waiting.** The rest of Stage 3 waits for the Spark rule compiler.
- **Signed off, out of order.** Stage 4 (the governance kernel): roles and entity scopes, separation of duties, the hash-chained audit log, and maker-checker configuration versions.
- **Signed off, on sample inputs.** Stage 6's first slice (journaling):
  - the rules are configuration the accounting team authors and approves through the API (event types, automated journal definitions, account mappings, the chart of accounts, dimensions and the policy);
  - a pure-Python engine is held to an independent oracle and balance invariants.
- **Signed off, on sample inputs.** Stage 7a, the journal workflow: templates, manual journals and CSV import, approvals with thresholds and a risk score, delegation, periods, batches, recurring journals, reversals, attachments and the auditors' evidence.
- **Signed off, on sample inputs.** Stage 7b: posting approved journals to the Databricks ledger exactly once, the ERP write-back (with a stub ERP), trial balance and drill-down, a Postgres-to-Delta tie-out, and XLSX import.
- **Signed off, on sample inputs.** Stage 7c: manual journals in foreign currencies, converted at submit under D9 at rates from a narrow, typed lookup, and posted in functional terms. Stage 7 is complete.
- **Signed off, on sample inputs.** Stage 8a, the first part of close management: the engine's journals in the Databricks GL through a sample stand-in job, balances and roll-forward from the whole GL, and the ERP's trial balance with a Delta-to-ERP tie-out.
- **Built, waiting for sign-off.** Stage 8b, the close: a soft close, the close checklist and flux analysis as configuration, and a hard close that waits for them. Its gate passed 128 of 128.
- **Next.** 8c, account reconciliations.
- **Needed before production.** The real Stage 6 inputs: the signed accounting-policy sheet, the chart of accounts and the dimensions ([docs/domain/stage-6-inputs.md](docs/domain/stage-6-inputs.md)).

[docs/stages/stage-1b-next-steps.md](docs/stages/stage-1b-next-steps.md) has the current checkpoint. The full plan with all 13 stages is in [docs/plan/ROADMAP.md](docs/plan/ROADMAP.md).

## Getting started

You need macOS on Apple Silicon with Homebrew and Docker Desktop, with Docker set to at least 10 GB of memory (Settings, then Resources). Everything else is installed by the bootstrap target, which is safe to run as often as you like.

```bash
make bootstrap     # tools from the Brewfile, Python 3.12 env, local secrets, git hooks, version check
make up            # the local stack: devproxy, app, Postgres, SeaweedFS, the Jobs API emulator and Spark Connect
make smoke         # one smoke run through the proxy
make ci            # lint, strict types, unit and Spark tests, Delta parity, platform/app/emulator suites, security, trivy
make test-reference  # the reference matcher and journaling engine with their golden tests (pure Python, minutes)
make gate-8b      # the Stage 8b gate, which writes docs/stages/stage-8b-report.md
make help          # every target with a one-line description
```

To look around, open `http://127.0.0.1:8088/__dev/login/alice` and then `http://127.0.0.1:8088/docs`, which is the API's Swagger page behind the local stand-in for the Databricks Apps proxy. The local users are alice (US01), bob (EU01), carol (no entities), dave (both) and erin (US01). For journals in US01: alice prepares, dave approves level 1, manages periods and posts, and erin approves level 2. A sixth user, erp (US01), is the machine identity of the stub ERP (`uv run python tools/erp_stub.py`), which reads the ledger feed and writes document numbers back.

## Layout

| Path | What lives there |
|---|---|
| `libs/contracts/` | Pure Python contracts shared by every component: hash IDs, canonical JSON, money, business calendars, table names and resolvers, query specs, with shared test vectors |
| `libs/platform/` | The control plane: writer leases with fencing tokens, the outbox, the scheduler, the Jobs API client and the read path's query engine |
| `engine/` | The Databricks-deployable engine: Spark helpers, job entrypoints, the migrations, thin notebooks, the Delta parity suite and the pure-Python reference matcher |
| `datagen/` | The seeded marketplace generator and its answer key |
| `services/app/` | The FastAPI app that runs as the Databricks App |
| `services/jobs_emulator/` | A local Jobs API 2.2 emulator that runs the engine with `spark-submit`, standing in for Lakeflow Jobs |
| `deploy/`, `compose*.yaml` | Dockerfiles, the devproxy configuration and the local stack |
| `tests/e2e/` | The fault-injection suite against the whole local stack |
| `.github/workflows/` | CI workflows, which have not run on GitHub yet because the repository has no remote |
| `tools/` | Gate runner, banned Spark API lint, version and jar locks, secrets generation, measurements, security scans and waivers |
| `docs/adr/` | Architecture decision records, including the findings that amended the approved plan |
| `docs/stages/` | Gate reports, stage summaries and sign-offs |
| `reports/`, `research_notes/` | The BlackLine research this platform is modelled on (kept private: not in the public repository) |

## Rules that hold everywhere

Money is always integer minor units with exact decimal FX math, IDs are deterministic 128-bit hashes, and every Spark job pins its session configuration, so the same inputs and configuration always give the same output digest. The engine never builds SQL from strings and never binds SQL parameters (open-source Delta drops them, see [ADR-0002](docs/adr/0002-spark-delta-parity-allowlist.md)), and the lint in `tools/lint_spark_bans.py` enforces this together with the other determinism rules. The app reads the lake only through typed query specs, and the engine adds the caller's entity filter to every one of them ([ADR-0009](docs/adr/0009-read-path-query-specs-final.md)).
