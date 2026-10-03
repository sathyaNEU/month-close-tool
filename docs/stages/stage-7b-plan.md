# Stage 7b plan: posting to the Databricks ledger, and the ERP write-back (on sample inputs)

On 2026-09-30 you signed off Stage 7a and chose 7b next, on sample inputs. This plan was approved automatically, as you asked for plans. It builds on [ADR-0016](../adr/0016-journal-workflow.md). Its decisions go in ADR-0017.

**D19 keeps its default unless you decide otherwise:**
- an ERP stays the general ledger's book of record;
- `posted` means committed to the Databricks ledger tables;
- `erp_confirmed` is optional, and records the document number an ERP writes back.

## What 7b covers, and what moves

- **In 7b:**
  - posting approved journals to Delta, exactly once;
  - the stub ERP and the write-back;
  - trial balance and drill-down;
  - the posting rules for periods, reversals and separation of duties;
  - the completeness tie-out between Postgres and Delta;
  - XLSX import (`openpyxl`, which you approved);
  - the OpenSSL pin fix, already done first.
- **Moved to 7c: multi-currency manual journals.** They need rates at submit, which live in Delta. The way to get them is a narrow, typed rate lookup rather than loosening the read path's entity filter, which Stage 1b signed off. That is its own piece of work: rates frozen at submit and bound into the approval, and one rounding line per journal.
- **Still blocked on the Spark rule compiler:**
  - **The engine's automated entries in the journal population.** They need the Spark journaling job. The option you chose listed the full population, and it can't be built yet.
  - **GL-history risk factors.** Until then, the GL holds only posted manual journals, which the risk score already sees in Postgres.

## Decisions

1. **Posting is a run per entity**, like every engine job (ADR-0006, ADR-0007).
   - **Starting it.** `POST /posting/runs {entity_id}` freezes the entity's approved journals into the run, moving them to `posting`. Batches are always taken whole. It then starts a `post_journals` job under the `ledger:<entity>` lease, with fencing.
   - **Size.** A run holds at most 500 journals, so its result line stays small.
2. **The job reads approved journals from Postgres, read-only.**
   - **The role.** A new role, `close_ledger_reader`, can `SELECT` one view, `posting_queue`: the frozen journals and lines of runs in progress. Nothing else.
   - **Credentials** live in their own secrets file, only in the jobs container. A test proves it.
   - **Creating the role.** A one-shot step creates it, or resets its password, before migrations on every `up`. The first-boot `initdb` scripts don't run on an existing volume.
   - **Production.** Lakebase access for the job's service principal is a Stage 10 item, and a recorded risk.
3. **One authoritative Delta table, written in one commit.**
   - **The lines.** `ledger.journal_lines` holds every posted line, with the journal's header fields repeated on each, written by an insert-only MERGE keyed on journal, content hash and line. Trial balance and drill-down read only this table.
   - **The ERP feed.** `ledger.journal_postings`, one row per journal with its canonical JSON, is written after the lines, also insert-only. A crash between the two only delays the ERP.
   - **Contracts.** Both tables arrive with DDL V007 through `ddl_apply`, with registry snapshots.
4. **Exactly once, even when a job commits and then fails.**
   - **The result line.** The job reports which journals it wrote and which were already there (the MERGE anti-join).
   - **A reconcile task** in the app's scheduler moves reported journals to `posted`, with the run and the Delta commit version. A failed run returns its journals to `approved`.
   - **A retry run** finds lines already written and reports them, so nothing is written twice and nothing is lost. The signed-off coordinator transaction isn't touched.
   - **Fault injection** proves it. The job is killed after its Delta commit and before its result line; there are then no duplicate lines, and the journals end `posted`.
5. **Separation of duties, and who posts.** `journal.post` is a new action, held by the controller.
   - **A requester's own journals are left out.** They stay `approved` for someone else, and the run lists them as skipped. This closes the roadmap's carried scenario, "preparing and posting the same journal".
6. **Periods and reversals.**
   - **Closing** is also refused while a period has approved-but-unposted or posting journals. The race test extends to a posting start racing a close.
   - **Reversing** is allowed for `posted` and `erp_confirmed` journals.
7. **The ERP write-back,** like BlackLine's `scheduleupdatepostingstatus`. `POST /posting/status` takes document numbers for posted journals, from an `integration_client` in the entity, and moves them to `erp_confirmed`.
   - **Repeats.** The same number again is a no-op, and a different one is a 409.
   - **The stub ERP** is a small script with a dev identity ("erp", an `integration_client`). It reads `journal_postings` through the read path, assigns document numbers, and writes them back.
8. **Trial balance and drill-down,** through the read path: queryable-registry entries for the new tables, with the entity filter always applied.
   - **`GET /gl/trial-balance`** totals by account for an entity and period.
   - **`GET /gl/lines`** pages through lines, each carrying its journal id.
9. **The completeness tie-out (AS 1105).** Per entity, posted journals in Postgres are tied to the lines in Delta by count and hash. A mismatch is reported, never smoothed over.
10. **XLSX import.**
    - **Packages.** `openpyxl` and its dependency `et_xmlfile`, added to close-app only, hash-pinned.
    - **Before parsing.** Each zip entry's uncompressed size and compression ratio are checked, against zip and XML bombs.
    - **Parsing** runs in a subprocess with a memory limit and a timeout. Only cached cell values are read, and a formula without one is refused.
    - **Amounts** go through `Decimal(str(value))`, and extra decimals are refused.
    - **One pipeline.** XLSX rows then go through the same pipeline as CSV.
11. **Coverage.** Every new operation joins the authorization matrix, the route inventory and the idempotency flow. Migration 0005 widens the journal statuses and adds the posting tables, the view and the grants.

## Gate 7b

Every Stage 7a step runs as regression, under `caffeinate`, and:
1. **States.** Posting and write-back moves are tested as a table, legal and illegal.
2. **Exactly once.** A run posts each journal once. A retry after a commit-then-crash finds the lines and adds none, and the journals end `posted`. Batches post all or none.
3. **Separation of duties and periods.**
   - A requester's own journals are skipped.
   - Closing is refused while journals are approved but unposted, or posting.
   - The posting-against-close race holds with the locks, and fails without them.
4. **The ERP round trip.** The stub ERP reads the feed, writes document numbers back, and the journals become `erp_confirmed`. A conflicting number is refused.
5. **Trial balance and drill-down** agree with the posted journals, and never cross entities.
6. **Completeness.** The Postgres-to-Delta tie-out holds on every run.
7. **Least privilege.** The reader role can select only the view, and its credentials live only in the jobs container.
8. **XLSX.** A valid workbook imports like its CSV. Bombs, formulas without values and extra decimals are refused.
9. **Governance.** The authorization matrix, the route inventory and idempotency cover every new operation, and the audit chain verifies.
10. **Through the stack.** alice prepares, dave and erin approve, the controller posts, and the stub ERP confirms. The trial balance shows the journal.
11. **Migration, contracts and ADR.** Migration 0005 and DDL V007 run, the snapshots show 0 drift, and ADR-0017 is written.
