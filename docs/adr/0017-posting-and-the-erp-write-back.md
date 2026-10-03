# ADR-0017: posting to the Databricks ledger, and the ERP write-back

Status: proposed with Stage 7b. Date: 2026-09-30. Implements, on sample inputs, the roadmap Stage 7 items left after 7a:
- posting approved journals to Delta, exactly once;
- the ERP write-back;
- trial balance and drill-down;
- the posting rules for periods, reversals and separation of duties;
- a completeness tie-out between Postgres and Delta;
- XLSX import.

Multi-currency manual journals move to 7c. The engine's automated entries in the journal population, and GL-history risk factors, wait for the Spark rule compiler.

## Context

You signed off 7a and chose 7b on sample inputs ([stage-7b-plan.md](../stages/stage-7b-plan.md)). D19 keeps its default:
- an ERP stays the general ledger's book of record;
- `posted` means committed to the Databricks ledger tables;
- `erp_confirmed` is optional, and records the document number an ERP writes back.

The design keeps the platform's earlier rules:
- **Only jobs write Delta,** under a lease with fencing (ADR-0007). The app holds no S3 keys (Stage 1).
- **Jobs never read the wall clock** (the engine's determinism rule, enforced by the Spark-ban lint).
- **The read path serves only registered, entity-scoped tables,** and always applies the caller's entities (Stage 1b).
- **Authorization comes before state,** and every POST is idempotent and audited (Stage 4).

BlackLine posts journals to the ERP through its connectors, and takes the posting status back through `scheduleupdatepostingstatus`. Here Delta is the ledger the platform owns, and the ERP confirms what it booked.

## Decision

**Posting is a run per entity,** like every engine job.
- **Starting it.** `POST /posting/runs {entity_id}` takes the entity's approved journals, locks them in id order and their periods `FOR SHARE`, and freezes them as `posting` in the run. It then starts the `post_journals` job under the `ledger:<entity>` lease, in the same transaction (the coordinator's outbox).
- **Size.** A run holds at most 500 journals, so its result line stays small. Batches are always taken whole: a batch that doesn't fit waits for the next run, and smaller journals after it still go.
- **Who.** `journal.post` is a new action, held by the controller.
- **Separation of duties.** The requester's own journals are left out, and so is a batch holding one. They stay `approved` for someone else, and the run lists them as skipped with the reason. This closes the roadmap's carried scenario, "preparing and posting the same journal".

**The job reads Postgres through one view.**
- **The view.** `posting_queue` shows the lines of journals frozen into runs in progress. It is a `security_barrier` view, so a predicate the reader adds can't see rows outside it.
- **The role.** `close_ledger_reader` can select that view and nothing else. A test proves it from inside the jobs container.
- **Creating the role.** A one-shot deploy step, `pg-roles`, creates it or resets its password before every migration, because the first-boot `initdb` scripts don't run on an existing volume. It has its own secrets file with only the superuser's and the reader's passwords.
- **Credentials** live only in the jobs service. Lakebase access for the job's service principal is a Stage 10 item, and a recorded risk.
- **A new engine dependency,** `psycopg[binary]`, lets the job connect. The app already used it.

**One authoritative Delta table, written in one commit.**
- **The lines.** `ledger.journal_lines` holds every posted line, with the journal's header fields repeated on each. The job writes it with an insert-only MERGE keyed on journal, content hash and line, scoped to the entity's partition. Trial balance and drill-down read only this table.
- **The ERP feed.** `ledger.journal_postings` holds one row per journal, with its canonical JSON. It is written after the lines, also insert-only, so a crash between the two only delays the ERP.
- **Both are append-only,** arrive with DDL V007 through `ddl_apply`, and are in the table registry and its snapshot.
- **The clock.** `posted_at` in Delta is the run's request time (Postgres time), the same on every line of a run, because jobs never read the wall clock. Postgres records its own `posted_at` when the run settles.

**Exactly once, even when a job commits and then dies.**
- **The result line** reports which journals the job wrote and which were already in the ledger.
- **A reconcile task** in the app's scheduler settles finished runs. A succeeded run's reported journals become `posted`, with the run and the ledger's commit version. Every other outcome returns the run's journals to `approved`, and the exception report lists each as `post_failed`.
- **A retry** is simply the next run. It finds lines a lost result hid, writes none of them again, completes the feed, and the journals end `posted`. The signed-off coordinator transaction isn't touched.
- **Which run, after a retry.** Each Delta row keeps the `run_id` of the run that wrote it, so after a crash the lines name the run that died, and so does the feed row if it got that far. The journal's `posting_run_id` in Postgres names the retry that settled it. Both runs are in `posting_runs`, the first one `failed`.
- **Proof.** On the stack, a job is killed after its ledger commit and before its feed and result line. The run fails, the journal returns to `approved`, and the retry posts it with its lines in the ledger once. The Spark tests cover the same at the job level, and show a stale token fenced before it writes.

**The states** add `posting`, `posted` and `erp_confirmed` to 7a's. A table test covers every posting and write-back move from every state:
- a run takes only `approved` journals;
- every 7a move on `posting`, `posted` or `erp_confirmed` is a 409, except reversing a `posted` or `erp_confirmed` journal;
- the write-back confirms only `posted` journals.

**Periods.**
- **Closing** is refused while a period has journals that are approved but unposted, or posting.
- **A close can't race a posting start into a bad state.** The posting start only takes approved journals, and those already block a close, so whichever commits first, the close is refused. A two-thread test shows this with the locks (the close waits for the posting start) and without them (the state check still refuses). The race that matters, a submit against a close, keeps its 7a test with its positive control.
- **Reversing** is allowed for `posted` and `erp_confirmed` journals.

**Posted journals keep the 7a rules.** Two 7a checks looked only at `approved` journals, so posting would have changed them silently. Both now count every state from `approved` on:
- an automatic reversal due when the next period opens is still created when the journal has been posted in the meantime;
- the seldom-used-account risk factor still counts a posted journal's accounts as used.

**The ERP write-back,** like BlackLine's `scheduleupdatepostingstatus`.
- **The call.** `POST /posting/status` takes up to 500 document numbers for posted journals, from an `integration_client` in their entity, and moves them to `erp_confirmed`.
- **All or nothing.** The same number again is a no-op, a different one is a 409, and a journal that isn't posted is a 409. A journal outside the client's entities is a 404, as for any object the caller can't see.
- **The feed** is `GET /gl/postings`, readable with `ledger.feed`, another integration-client action.
- **The stub ERP** (`tools/erp_stub.py`) plays the ERP through the same API, with a new dev identity, `erp` (1006). It pages through the feed, refuses an unbalanced payload, and numbers each journal `ERP-<entity>-<number>`, so a rerun is a no-op.

**Trial balance and drill-down,** through the read path:
- `GET /gl/trial-balance` totals debits, credits and lines by account, for an entity and optionally a period;
- `GET /gl/lines` pages through posted lines, each carrying its journal;
- the query engine always applies the caller's entities, so neither crosses entities.

**The completeness tie-out (AS 1105).** `GET /posting/tie-out` compares, per entity and period, the posted journals in Postgres with the lines in Delta, by line count, debits and credits. A difference is reported, never smoothed over.

**XLSX import** goes through the same pipeline as CSV.
- **Packages.** `openpyxl` (which you approved) and its dependency `et-xmlfile`, in the app only, hash-pinned.
- **Before parsing.** `defusedxml` isn't installed, so `openpyxl` alone doesn't stop XML entity expansion. Nothing is parsed until the zip passes: at most 200 entries, 50 MiB uncompressed in all, and no entry compressed more than 100 to one.
- **Parsing** runs in a subprocess with a CPU limit and a timeout everywhere, and a 256 MiB memory limit on Linux (macOS ignores it).
- **Never inside a transaction, one at a time.** The workbook is converted before the import's transaction opens, so no database connection is held while an untrusted file is parsed. Each app process reads one workbook at a time, and a second upload that waits too long gets a 503, so uploads can't use up the app's memory or its connection pool.
- **Cells.** Only calculated values are read, and a formula without one is refused. Numbers become exact decimals, so Excel's stored `0.30000000000000004` is refused for its extra decimals rather than rounded behind the user's back.
- **Records.** Every file is recorded under the workbook's own SHA-256, accepted or not.
- **A replay parses again.** Because conversion comes before the idempotency check, a replay with the same key converts the workbook again before returning the stored response. If another upload holds the parsing slot at that moment, the replay gets a 503 and can be retried. *Amended by ADR-0018:* a read-only replay lookup now runs first, so a replay returns the stored response without parsing the workbook again.

**Coverage.** Every new operation joins the authorization matrix (193 operations in all), the route inventory and the idempotency flow. Migration 0005 widens the journal statuses and adds the posting columns, `posting_runs`, the view and its grant, and runs up, down and up.

## Consequences

- **The ledger is queryable now.** Stage 9's screens and Stage 8's close management build on `/gl/*` and `/posting/*`.
- **Production needs** the job's Lakebase access (Stage 10, a recorded risk) and a real ERP integration in place of the stub.
- **The tie-out compares totals, not hashes.** The plan said "by count and hash", but the read path aggregates by count and sum only. A per-journal content-hash comparison needs a SQL warehouse query, and comes with Stage 10.
- **7c: multi-currency manual journals,** through a narrow, typed rate lookup, so the read path's entity filter stays as signed off.
- **Still blocked on the Spark rule compiler:** the engine's automated entries in the journal population, and GL-history risk factors.
- **A packaging fix.** `psycopg` pulls in `tzdata` for Windows only, while pandas needs it everywhere. The Spark image's requirement merge now accepts the same pin under different markers and keeps the broader one, and still refuses any real disagreement.
- **The residual fencing window** (ADR-0007) applies to the posting job as to every job: fencing detects a stale writer, and the lease prevents one.
