# Working on this repository with Claude Code

This is a month-close platform for a GOAT/StockX-style marketplace: BlackLine-level transaction matching and journal entry, on Databricks (a Databricks App with Lakebase, and Lakeflow Jobs), developed locally on Docker Compose. [README.md](README.md) explains the layout, and [docs/plan/ROADMAP.md](docs/plan/ROADMAP.md) is the plan with its changelog.

**Start every session with the resume note,** [docs/stages/stage-1b-next-steps.md](docs/stages/stage-1b-next-steps.md). It says what is signed off, what is waiting for the owner, and what comes next.

## Standing rules from the project owner

These apply to every session, local or in the cloud.

1. **Stage-gated delivery.** Build one stage, or one part of a stage, fully. Run every suite its gate runs, then `make gate-N`, which writes `docs/stages/stage-N-report.md`. Then stop and ask for sign-off before starting the next stage.
2. **Plans are approved automatically.** After writing a stage's plan, start building it. Still stop at the stage report.
3. **No new third-party package without the owner's approval.** List a package you want under "Needs your decision" in the stage summary.
   - Approved so far: `openpyxl` (and `et-xmlfile` with it), and `psycopg[binary]` in the engine.
   - Not approved: `defusedxml`.
4. **Commit and push only when the owner asks.**
5. **The research stays private.** `reports/` and `research_notes/` are kept out of this public repository.
6. **Don't mark the policy sheet "Confirmed"** ([docs/domain/policy-sheet-status.md](docs/domain/policy-sheet-status.md)).
7. **ADR status lines stay "proposed with Stage N".** The stage's sign-off document records the acceptance.
8. **Leave wording polish for the end.** The plain-English style pass, and American against British spelling, wait until the whole app is near the finish line.
9. **The Spark rule compiler stays unwritten** in these sessions. Build what doesn't need it (the stand-in, reference engines and samples are the pattern).
10. **If a safety classifier refuses an action,** don't pursue the same outcome another way, and don't reproduce the refused content.
11. **Secrets live in the gitignored `.local/secrets`** (`make secrets`). The app holds no object-store keys.
12. **Docker housekeeping.** Don't prune Docker's global build cache, and don't touch Docker Desktop's Group Containers folder.
13. **No `brew trust` of the Databricks tap** without the owner's go-ahead. It is needed for the Databricks CLI in Stage 10.
14. **The data is sample data.** It is not the owner's accounting until the real inputs arrive ([docs/domain/stage-6-inputs.md](docs/domain/stage-6-inputs.md) and [docs/domain/stage-7-inputs.md](docs/domain/stage-7-inputs.md)).

## What runs where

Each tier needs more than the one before it:

1. **Python only:** `make lint typecheck test-unit test-reference`.
2. **Java 21 as well:** the host Spark tests, `make test-spark`.
3. **Docker:** the app and platform suites, `make test-app test-platform`, against the test Postgres (`compose.test.yaml`).
4. **Docker with about 10 GB of memory:** the full stack (`make up`), the end-to-end suite (`make test-e2e`) and the gates (`make gate-N`).

**Where this was built.** The gates were calibrated on an Apple Silicon Mac with Docker Desktop: `make bootstrap` uses Homebrew, and the gate keeps the Mac awake with `caffeinate`.

**In a new environment,** check what it can run, starting with tier 1. The CI workflow (`.github/workflows/ci.yml`) runs the same targets on Ubuntu.

**Fresh stacks.** Run the end-to-end suite on a fresh stack (`make down-v`, then `make up`, then `make test-e2e`), as each gate's cold bring-up does. The journaling stand-in's pins and the closed sample periods stay in the stack's volumes between runs.
