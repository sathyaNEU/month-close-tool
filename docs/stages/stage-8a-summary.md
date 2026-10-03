## What this part delivers

On 2026-10-01 you signed off Stage 7c, which completed Stage 7, and chose Stage 8 next, on sample inputs. Stage 8 is split into three parts, each with its own gate. This first part, 8a, puts the general ledger on Databricks. The plan is [stage-8-plan.md](stage-8-plan.md) and the decisions are in [ADR-0019](../adr/0019-the-general-ledger-on-databricks.md).

- **The engine's journals in Delta.** A sample stand-in for the Spark journaling job, `journal_ref`, runs the reference engine Stage 6 holds to its oracle. It writes the engine's subledger lines and GL rollups for one entity to `ledger.subledger_lines` and `ledger.gl_rollup_lines`.
  - **Started like a posting run.** A controller starts it (`POST /journaling/runs`) under the entity's ledger lease, with fencing, and a reconcile task records the outcome.
  - **Lines first, rollups last,** both insert-only. A crash between the two is completed by the retry, and a rerun writes nothing.
  - **Nothing booked twice.** The first successful run pins its configuration and a digest of its inputs. After that, a changed configuration or more landing data is refused, not applied. Applying changes needs the Spark job's correction state.
- **Configuration as a snapshot.** Starting a run freezes the active configuration, the entity set and the entity's period statuses in Postgres, keyed by their SHA-256. The job reads the snapshot as the jobs' read-only role, through a view of runs in progress, and refuses one whose hash doesn't match. An event dated in a closed period is booked in the first open one, and a period can't close while a run is in progress.
- **Balances and roll-forward from the whole Databricks GL.** Both sources count: the engine's rollups and the posted manual journals.
  - `GET /gl/balances` gives each account's opening balance, the period's debits and credits, and its closing balance.
  - `GET /gl/roll-forward` checks that each period opens where the last one closed.
  - The trial balance covers both sources now.
- **The ERP's trial balance, and the tie-out.**
  - **Uploading.** An integration client uploads the ERP's closing balances as CSV for an entity and period. Each upload is checked all or nothing, and kept by version with its hash.
  - **`GET /gl/erp-tie-out`** lists every account where the Databricks GL and the ERP differ, including an account only one side has.

## Results

| Check | Result |
|---|---|
| App tests (Postgres) | 202 passed, plus the audit-chain residual as a strict xfail; 11 of them new for the ledger |
| Unit tests | 478 passed, contracts coverage 96%; one new for the stub ERP's trial balance |
| Spark tests | 46 passed and the 4 known xfails; 7 new. The stand-in equals the engine through Delta. A rerun writes nothing, and every changed input is refused. A crash between the two commits is completed by the retry, and a writer fenced between them writes no rollups (nor, in 7b's posting job, a feed) |
| Authorization matrix | 202 operations × 12 roles × 3 scopes = 7,272 checks, 0 unexpected allows or denies |
| End-to-end suite | 53 passed on a fresh stack in 18 minutes, before the gate |
| Stack | dave journals the generator's `tiny` data for US01: 339 events give 927 lines and 67 rollups, and a rerun writes nothing. July balances across 21 accounts, with 48,240.36 USD of debits and of credits, and the roll-forward from July to September ties. The stub ERP's extract adds one accrual booked only in the ERP, and the tie-out reports exactly its 2 accounts |

## Found and fixed while building

1. **A run could start before the previous one's pin existed.** The ledger lease is released when the job ends, but the pin is written later, when the run is settled. A run started in between would have run unpinned, and could have booked rollups twice. A run now waits for the entity's previous run to be settled, and a test starts one in exactly that gap.
2. **The pins missed two ways to book twice.**
   - **A run that failed after writing never pins.** A rollup's id hashes only its group, so a later run with more landing data would compute new amounts under the old ids, and the insert-only write would silently keep the stale rows.
   - **The pins don't cover period statuses.** After July closes, a pinned rerun would book July's events in August under the same line ids. The July lines would stay, and new August rollups would count them a second time.

   Before writing, the job now compares every row the entity's ledger holds with what it is about to write, and refuses any difference. A Spark test covers a changed configuration, more landing data and a period closed since.
3. **The two commits had no fence check between them.** ADR-0007 says every commit is preceded by one. The stand-in had a check only before its first commit, and so did 7b's posting job. Both now check before their second commit. Two Spark tests let a newer attempt claim the fence right after the line commit, and show that no rollups, and no feed, are written.
4. **An incomplete result would have jammed the reconcile task.** A success result without the run's configuration hash or an inputs digest would have been marked reconciled. Then the pin insert would have failed on every pass. Such a run now settles as failed, and pins nothing.
5. **The ERP parser had four ways to read a balance wrong.**
   - An unquoted thousands separator (`1110,1,234.56`) would have been read as a balance of 1.00. A row with more fields than the header is now refused.
   - `sNaN` raised an unhandled error (a 500).
   - `1e999999999` overflowed Decimal's own exponent limit.
   - A value with more than 28 digits rounded silently.

   Balances are now converted with exact integer arithmetic on their digits, at most 40 characters, within BIGINT. A test covers each case.
6. **The lease test accepted any 409.** Posting with nothing approved also gets a 409, so the test didn't prove the lease kept posting out. It now posts an approved journal and checks the lease both ways. Posting's busy message now says the entity's ledger is busy, since a journaling run may hold the lease.
7. **The job's name was too long.** Job keys are at most 14 characters, so the job is `journal_ref` (its module is still `journal_reference`).
8. **Sums skip nulls.** The check for rollups without functional amounts can only see an account whose rollups are all null. The test uses that case, and the limit is documented. The writer always fills the amounts.
9. **A 7c test pinned the schema head.** It expected migration 0006 to be the newest, and now reads the head from the app.

## Where the build differs from the plan

1. **Two guards beyond the plan's pins:** a run waits for the previous one to be settled, and the job compares what is already written before it writes (items 1 and 2 above).
2. **The trial balance's response drops `version`.** It now reads two tables, each at its own Delta version, so one version would be misleading.
3. **The roll-forward covers at most 24 periods per request.**
4. **The extract format refuses thousands separators,** quoted or not.
5. **Posting's busy message changed,** and the test that checks the reader role from the jobs container was renamed for its two views.

## Needs your decision

1. **Accept ADR-0019.**
2. **Approve the stand-in:** the reference engine running as the `journal_ref` job, on sample volumes, until the rule compiler exists. The sign-off asks this as its own question.
3. **Accept the contract changes:**
   - the table registry: the rollups' functional amounts (DDL V009), and natural keys for the subledger lines and rollups;
   - the queryable registry gains the rollups;
   - the trial balance's response drops `version`.
4. **The real inputs** before production:
   - which ERP, and how it exports a trial balance;
   - whether its account codes are the platform's, or need a mapping for the tie-out.

   These join the Stage 6 and Stage 7 inputs already requested.
5. **Next:** 8b on sample inputs (the close checklist, flux analysis and the soft close), or wait for the real inputs.

## Carried forward

- **8b:** the close checklist, flux analysis with explanations, and `PeriodCloseWorkflow`'s soft close.
- **8c:** account reconciliations.
- **Still to place in Stage 8:** revaluation of foreign-currency balances at period end, carried from 7c.
- **Blocked on the Spark rule compiler:**
  - the Spark journaling job, which replaces the stand-in;
  - the correction state that applies a changed configuration or late data to a written ledger.
- **Stage 10:**
  - a real ERP extract;
  - the jobs' Lakebase access for snapshots and the posting queue.
