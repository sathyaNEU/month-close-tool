## What this part delivers

On 2026-09-30 you signed off Stage 7a and chose 7b next, on sample inputs. This is Stage 7's second part: posting approved journals to the Databricks ledger, and the ERP write-back. The plan is [stage-7b-plan.md](stage-7b-plan.md) and the decisions are in [ADR-0017](../adr/0017-posting-and-the-erp-write-back.md). D19 keeps its default: an ERP stays the book of record, `posted` means committed to the Delta ledger, and `erp_confirmed` records the ERP's document number.

- **Posting** (`/posting/runs`). The controller posts an entity's approved journals as one run. The `post_journals` job writes them to `ledger.journal_lines` exactly once, and a scheduler task marks them `posted`. The requester's own journals are never in their run, and batches post whole.
- **The ERP round trip.** An ERP reads the feed (`/gl/postings`) and writes document numbers back (`/posting/status`), moving journals to `erp_confirmed`. A stub ERP (`tools/erp_stub.py`, signed in as the new `erp` dev user) plays that part.
- **Ledger reads.** A trial balance (`/gl/trial-balance`) and line drill-down (`/gl/lines`) come from the ledger through the read path, so they never cross entities.
- **Completeness.** A tie-out (`/posting/tie-out`) compares Postgres's posted journals with the ledger, per entity and period, and reports any difference.
- **Period and reversal rules.** A period can't close while journals are approved but unposted, or posting. Posted journals can be reversed.
- **XLSX import.** It goes through the same pipeline as CSV, with checks against zip and XML bombs, and refuses formulas without values and Excel's float noise.
- **The OpenSSL pin is gone.** Both images now upgrade Debian's OpenSSL without an exact version, so a newer Debian update can't break the image build.

## Results

| Check | Result |
|---|---|
| App tests (Postgres) | 173 passed, plus the audit-chain residual as a strict xfail; 17 of them new for posting and XLSX |
| Spark tests | the posting job writes each line once however often it runs, a crash between its two commits is finished by the rerun, and a stale token is fenced before it writes; DDL V007 applies and every table matches its contract |
| Unit tests | 465 passed, contracts coverage 96% |
| Authorization matrix | 193 operations × 12 roles × 3 scopes = 6,948 checks, 0 unexpected allows or denies |
| Idempotency | every POST replays byte-identically, including starting a posting run and the write-back |
| End-to-end suite | 51 passed on the stack in 15 minutes, before the gate |
| Stack | alice prepares, dave and erin approve, dave posts; the trial balance shows the journal and the stub ERP confirms it. A job killed after its ledger commit fails cleanly, and the retry posts the journal with its lines in the ledger once |
| Least privilege | the posting job's role reads only the posting queue (checked from inside the jobs container), and its password reaches only the jobs service and the step that creates the role |

## Found and fixed while building

1. **DDL V007 would have failed to apply.** Its two `CREATE TABLE` statements had no terminators, so the migration runner read them as one statement. The migration test's statement count caught it, before it had been applied anywhere.
2. **The posting job read the wall clock.** The Spark-ban lint caught `datetime.now()`. Jobs never read the wall clock, so the ledger's `posted_at` is now the run's request time (Postgres time), and the column's comment says so.
3. **Posting would have changed two 7a rules silently.** Both looked only at `approved` journals, and posted journals leave that state:
   - **Automatic reversals.** An accrual posted before its next period opened would never have been reversed.
   - **The seldom-used-account risk factor.** Once journals were posted, their accounts would have counted as unused, raising later journals' risk scores.

   Both now count every state from `approved` on, and each has a regression test.
4. **A 7a test closed a period right after approving a journal.** Under 7b's rule the close now waits for posting, so the test posts the journal first and asserts the new refusal.
5. **The write-back answered 409 for another entity's journal.** It now answers 404, as for every object the caller can't see, and the authorization matrix holds it.
6. **The step that creates the posting job's role got every database password.** It read the whole Postgres secrets file, including the migrator's and the app's. The least-privilege stack test caught it, and the step now has its own file with only the superuser's and the reader's passwords.
7. **The Spark image build broke on `tzdata`.** `psycopg` needs it on Windows only, and pandas everywhere, and the requirement merge refused the difference. It now keeps the broader of two identical pins, and still refuses a real disagreement. Unit tests cover both.
8. **A 7a refusal read "only the journal's preparer can add to a batch it".** Every preparer-only message now reads properly.
9. **Workbooks were parsed inside the import's transaction.** The conversion (up to 20 seconds) held a pooled database connection the whole time, and its 512 MiB memory limit was the app container's entire allowance. A few slow uploads could have used up the connection pool, and one large one could have got the app killed. Workbooks are now converted before the transaction opens, one at a time per app process (a second upload waits, then gets a 503), within 256 MiB.
10. **A test that could not fail.** `openpyxl` writes floats in their short form, so a workbook written by the test never held Excel's 17-digit `0.30000000000000004`. The test now stores the value the way Excel does, and the import refuses it.
11. **The first gate run passed 96 of 97 criteria.** The one failure was a test in the Jobs API emulator's suite that pins the bundle's list of jobs. It still listed three, and the bundle now has `post_journals` as a fourth. I hadn't run that suite before the gate. The test now expects four jobs, the emulator suite passes (21 tests), and this report is the second run.

## Where the build differs from the plan

1. **The posting-against-close race can't fail without the locks.** The plan said the race test "holds with the locks, and fails without them". But a posting start only takes approved journals, and those already block a close, so the close is refused whichever commits first. The test now shows that both ways (with the locks, the close waits for the posting start). The race that does need the locks, a submit against a close, keeps its 7a test with its positive control.
2. **The tie-out compares totals, not hashes.** The plan said "by count and hash". The read path aggregates by count and sum only, so the tie-out compares line counts, debits and credits per entity and period. A per-journal hash comparison comes with the SQL warehouse in Stage 10.
3. **The ledger's `posted_at` is the run's request time,** because jobs never read the wall clock (item 2 above).
4. **Batches post whole even when a run fills up.** A batch that doesn't fit waits for the next run, and smaller journals after it still go.
5. **The write-back is all or nothing,** and a journal outside the client's entities is a 404 (item 5 above).
6. **A sixth dev user,** `erp` (1006), is an integration client in US01, for the stub ERP.

## Needs your decision

1. **Accept ADR-0017,** with:
   - the read-only posting role and the step that creates it;
   - the tie-out by totals rather than hashes, and the reworded race criterion (both in "Where the build differs from the plan");
   - the ledger's `posted_at` being the run's request time;
   - the contract snapshot change: the table registry gains `ledger.journal_lines` and `ledger.journal_postings`.
2. **D19, on its own.** 7b was built on D19's default: an ERP stays the book of record, `posted` means in the Delta ledger, and `erp_confirmed` is optional. The roadmap said to confirm D19 at Stage 5, and nobody has confirmed it yet. Keep it as the default for now, confirm it, or make Databricks the book of record.
3. **`psycopg` in the engine (a new place for a package).** It was already in the lock file for the app. The posting job now needs it to read Postgres, so the engine wheel depends on `psycopg[binary]`, with its bundled libpq and OpenSSL, and Databricks job clusters will install it. `et-xmlfile` came with `openpyxl`, which you approved.
4. **Next:** 7c (multi-currency manual journals, which finishes Stage 7), or Stage 8 (close management), or wait for the real inputs.

The real inputs are still needed before production: your approval matrix, SoD rules, templates, recurring journals and close calendar ([stage-7-inputs.md](../domain/stage-7-inputs.md)), and which ERP will confirm postings.

## Carried forward

- **7c:** multi-currency manual journals, with rates frozen at submit through a narrow, typed rate lookup.
- **Blocked on the Spark rule compiler:**
  - the engine's automated entries in the journal population;
  - GL-history risk factors.
- **Stage 10:**
  - the posting job's access to Lakebase (a recorded risk);
  - a real ERP integration in place of the stub;
  - a per-journal hash comparison in the tie-out.
- **Stage 5:** the configuration-change lock.
