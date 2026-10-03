# ADR-0019: the general ledger on Databricks

Status: proposed with Stage 8a. Date: 2026-10-01. Implements, on sample inputs, the first part of the roadmap's Stage 8:
- the engine's journals in Delta, through a sample stand-in for the Spark journaling job;
- balances and roll-forward from the whole Databricks GL;
- the ERP's trial balance, and the Delta-to-ERP tie-out.

Still blocked on the Spark rule compiler: the Spark journaling job itself, which replaces the stand-in, and the engine's correction state (applying a changed configuration or late data to a ledger already written).

## Context

You signed off 7c, which completed Stage 7, and chose Stage 8 on sample inputs ([stage-8-plan.md](../stages/stage-8-plan.md)). D19 keeps its default: an ERP stays the general ledger's book of record. A survey of the code found:
- **The Databricks GL held only manual journals.** The journaling engine's output tables (`ledger.subledger_lines`, `ledger.gl_rollup_lines`) had no writer, because the Spark journaling job waits for the compiler. The pure-Python reference engine (ADR-0015) produces the same lines, but only inside tests and the gate's scoring tool.
- **There was no ERP trial-balance extract,** so nothing could show that the Databricks GL and the ERP agree.
- **Jobs had no way to receive configuration.** The roadmap planned content-addressed snapshots on object storage, but the app holds no object-store keys (ADR-0006's least privilege, checked by `test_02`).
- **`ref.periods` says every period is open.** The generator writes it that way, and nothing syncs it with the periods in Postgres, which are what the close changes.

## Decision

**Jobs get their configuration as a content-addressed snapshot in Postgres.**
- **What it holds.** Starting a run writes everything the run depends on to `config_snapshots` as canonical JSON: the active journaling configuration (policy, chart, dimensions, mappings, event types and definitions, each with its version), the active entity set, and the entity's period statuses. The key is the content's SHA-256. The configuration's own hash is kept next to it.
- **Read under a share lock.** The period statuses are read `FOR SHARE`, so a close can't commit between the snapshot and the run's start.
- **Insert-only.** The app's role has no `UPDATE`, `DELETE` or `TRUNCATE` on it, and `test_migrations` checks that. Identical snapshots share one row.
- **The job reads it as the jobs' read-only role** (`close_ledger_reader`), through the `job_config_snapshots` view. The view is a `security_barrier` view that shows only snapshots of runs in progress, like 7b's `posting_queue` (ADR-0017). The run's parameters carry only the hash (with the entity and the pinned digest), and the job recomputes it and refuses a mismatch.
- **The future Spark matching and journaling jobs** take their configuration the same way.
- **Not object storage,** because that would give the app object-store keys it doesn't otherwise need. This amends the roadmap.

**A sample stand-in for the Spark journaling job.** `journal_ref` runs the reference engine, the one Stage 6 holds to its oracle and invariants, and writes its subledger lines and GL rollups for one entity to Delta.
- **Started like a posting run.** A controller's `journaling.run` (`POST /journaling/runs`) starts it through the coordinator, under the entity's `ledger:<entity>` lease, the one posting runs take, with fencing. A periodic reconcile task settles it.
- **One run at a time, settled before the next.** A new run waits for the previous run to be settled, not only for the lease. The lease is released when the job ends, but its pin is written when it is settled, and the next run must see the pin.
- **Fenced before every commit.** The job checks its fencing token before the line commit, again before the rollup commit, and once after the last (ADR-0007). The check between the two commits is new, and 7b's posting job gains the same check before its feed commit.
- **Write order.** Subledger lines first, rollups last. Both are insert-only `MERGE`s on the engine's deterministic ids, within the entity's partition. A crash between the two is completed by the retry, and a rerun writes nothing.
- **Periods come from the snapshot,** not from `ref.periods`. An event dated in a closed or locked period is booked in the first open one as a prior-period adjustment, as in Stage 6. A period can't close while a journaling run for its entity is in progress.
- **One source of functional currencies.** The job refuses to run if the snapshot's entity set and `ref.entities` disagree on an entity's functional currency.
- **Refusals are results, not failures.** A refused run ends `refused` with its reason, and writes nothing.

**Nothing is booked twice.** The engine's line ids hash the event (with its version) and its definition's line, and a rollup id hashes its group (entity, period, account, dimensions, currency). So a different configuration or more landing data would give lines that differ under the same ids, or rollups whose amounts change under the same id. An insert-only write would keep the old rows and silently skip the new ones.
- **Pins.** The first successful run for an entity pins its configuration hash and a digest of every input table it read. A later run with another configuration is refused when it is started (409). The job refuses inputs whose digest differs from the pin. A matching rerun writes nothing.
- **The backstop, for what the pins don't cover.** A run that failed after writing never pins. And the pins don't cover period statuses: after July closes, a rerun would book July's events in August under the same line ids, so the old lines would stay and new August rollups would count them twice. So before writing, the job compares every row the entity's ledger already holds with what it is about to write, all but the run's own stamps (`run_id`, `created_at`). If any row differs, or is one it wouldn't write, the job refuses. The tests cover a changed configuration, more landing data, and a period closed since.
- **Incomplete results don't settle.** A success result that doesn't name the run's configuration hash, or has no inputs digest, settles the run as `failed`, so it pins nothing.
- **Changes need the Spark job.** Applying a changed configuration or late data to a written ledger needs the engine's correction state, which only the Spark job will keep.

**The rollups gain functional amounts.** DDL V009 adds nullable `functional_debit_minor` and `functional_credit_minor` to `ledger.gl_rollup_lines` with `ADD COLUMNS` (parity feature 19, ADR-0018). The stand-in always fills them. The rollups join the read path's queryable registry, entity-scoped like the other ledger tables.

**Balances and roll-forward from the whole Databricks GL.** They read through the read path (ADR-0009), so the caller's entities always apply.
- **Two sources:** the engine's rollups and the posted manual journals, functional amounts only. For each source, one grouped query by account covers the periods before P (the opening balance), and one covers P (the activity), well inside the 1,000-group limit.
- **No silent zeros.** An account whose rollups have no functional amounts is a 500. Sums skip nulls, so a null among an account's other rollups can't be seen this way, and the writer's guarantee covers that case.
- **`GET /gl/balances`** gives each account's opening balance, the period's debits and credits, and its closing balance. "Balanced" means the debits equal the credits and the closing balances sum to zero.
- **`GET /gl/roll-forward`** (at most 24 periods) reads each period's opening independently. So its check that each period opens at the previous period's close is a real one, and a line in an odd period breaks it.
- **The trial balance** (`/gl/trial-balance`, ADR-0017) now covers both sources.

**The ERP's trial balance, and the tie-out.**
- **The extract's contract.** CSV with the header `account,balance`, one row per account. The balance is the closing balance for the period in the entity's functional currency, in major units with that currency's decimals. Debits are positive and credits negative. There is no thousands separator, and a row with more fields than the header is refused, so an unquoted `1,234.56` can't be read as 1. Balances are converted with exact integer arithmetic on their digits, so no value can round, overflow or build a huge number.
- **All or nothing.** Every row is checked before anything is stored, and each problem names its CSV row. An integration client (`erp.extract`) uploads it for an entity and period. Every upload is kept by version with its SHA-256, accepted or rejected, insert-only and audited.
- **`GET /gl/erp-tie-out`** compares each account's closing balance in the Databricks GL with the latest accepted extract. It lists every difference: `differs`, and an account only one side has (`erp_only`, `gl_only`). Nothing is smoothed over. The response names the extract's version and hash.
- **The stub ERP is circular by construction.** It builds its extract from the GL's own balances, plus entries a sample books only in the ERP. So its accounts tie by construction, except the injected ones. The app tests cover what a real extract can get wrong: an account only one side has, a sign error, and an extract uploaded for the wrong period.

**The shared lease speaks for both.** Posting's busy response now says the entity's ledger is busy, because a journaling run may hold the lease.

**Governance.** The new operations join the authorization matrix, the route inventory and the idempotency suite. Migration 0007 runs up, down and up. The reader role's two views are checked in Postgres and from inside the jobs container.

## Consequences

- **The Databricks GL has the engine's journals,** so balances, the roll-forward and the ERP tie-out compare real ledgers on sample data, and 8b's flux analysis and 8c's reconciliations have something to read.
- **The stand-in is for sample volumes only.** It reads its inputs into Python, and the gate runs it on the `tiny` profile. The Spark journaling job replaces it when the compiler exists, writing the same tables.
- **A written ledger is frozen until then.** After an entity's first successful run, a changed configuration, more landing data or a period closed since is refused, not applied. A matching rerun writes nothing. On the samples, the way out is a fresh lake.
- **Running the stand-in as a job needs your approval,** which the sign-off asks for separately.
- **Snapshots live in Postgres,** next to the configuration they copy. The sample configuration's snapshot is about 53 KB, and only a run whose configuration or period statuses changed adds one, which is small at seven users and monthly runs.
- **The ERP stays the book of record (D19).** The tie-out shows where the two disagree, and it doesn't correct either side.
- **Not in 8a:** the close checklist, flux analysis and the soft close (8b); account reconciliations (8c); revaluation of foreign-currency balances, and consolidation.
