## What Stage 2b delivers

Stage 2b is the second half of Stage 2 as split in [stage-2-plan.md](stage-2-plan.md): the data contracts, the Delta migrations and the seeded marketplace generator with its answer key ([ADR-0012](../adr/0012-table-contracts-and-the-seeded-generator.md)).

- **Table contracts and migrations.**
  - A registry describes all 45 tables: 17 landing, 6 reference, 7 matching, 5 ledger and 10 ops.
  - Six `V###` migrations create them, and a `ddl_apply` job applies each exactly once and refuses an edited file.
  - A Spark test holds every table to the registry.
  - `ddl_apply` runs as a job through the stack on an empty lake root.
- **Default data sources.** Every landing table has an identity key, and 13 default data sources map the landing shapes to canonical items in the rule DSL. A test runs every mapping on every generated row and matches the mapped amounts to the control totals.
- **The generator.** `close-job datagen` builds three entities (US, EU, UK) and four programs of orders with their full lifecycle. It then derives:
  - Stripe and Adyen settlement with daily payouts;
  - seller payouts through Hyperwallet-like and PayPal-like providers;
  - carrier invoices, tax documents and remittances, stored value and fee invoices;
  - bank statements in BAI2, camt.053 and MT940.

  Thirteen break types are injected by exact quota, and an answer key for the Stage 3 templates (1, 2, 3, 6, 9 and 10) passes its own self-consistency checks. `small` is about 116,000 landing rows and `medium` about 1.24 million.
- **Memory.** The SeaweedFS margin, the first task, is fixed with a measured Go memory limit, and the generator streams its output so `medium` fits the local jobs container.

## Found and fixed while building and testing

1. **SeaweedFS** peaked at 614 to 633 MiB of process memory under the read benchmark. The lake was only about 50 MB, so the growth was transfer buffers. `GOMEMLIMIT=512MiB` brings the peak to about 411 MiB.
2. **The jobs container** reached 92% of its limit on the first `medium` run. Four changes fixed it:
   - the two JVM and allocator settings from Spark Connect, now used for every job (`MALLOC_ARENA_MAX=2`, and `-XX:ActiveProcessorCount=2` matching `local[2]`);
   - streaming tables and the answer key out of the generator (its Python peak fell from 1.4 GiB to 0.54 GiB);
   - chunked writes;
   - a limit of 3,072 MiB.

   The gate measures the result.
3. **Job IDs in the emulator.** A job without a configured ID got a default one that could collide with a configured ID, and the later job silently replaced the earlier one. Adding a third job would have replaced the smoke job. Configured IDs are now reserved first, defaults take the next free ID, and repeated IDs are rejected.
4. **Append-only reference data.** Tables like `ref.fx_rates` refuse an overwrite, so the generator inserts only missing rows on the table's natural key, which also makes reruns idempotent.
5. **The answer key's own checks** found that some answer-key members and break-log entries used raw IDs instead of the canonical identity form. That was fixed in the generator before any gate ran.

## Where the build differs from the plan

1. The jobs container limit is 3,072 MiB instead of 2,560 MiB (above).
2. The duplicate break is a double-posted seller payable under a new identity. The platform deduplicates by identity, so a provider row repeated under the same ID would simply collapse instead of surfacing.
3. There are 13 break types rather than the roadmap's 10 categories:
   - partial captures and partial refunds are separate types;
   - chargebacks and late wins are separate types;
   - carrier corrections are added, for template 9.
4. The answer key covers the Stage 3 templates. Keys for templates 4, 5, 7, 8, 11 and 12 come with the stages that build them, and the breaks those templates need are already in the data.
5. The migrations ship inside the engine wheel (`close_engine/sql/ddl`), not in a separate `engine/sql/ddl`, so `ddl_apply` always runs exactly the files that were released.

## Gate runs

The first full gate run (57 minutes) passed 42 of 44 criteria. One failure was mine: an app test still assumed the job bundle holds exactly one job. Since this stage it holds three (smoke, `ddl_apply` and `datagen`), and I reran only the emulator tests after changing the bundle. The test now selects the smoke job by its pinned ID. The other failure was the policy-sheet confirmation, which waits for your answer. This report is the run after that fix.

## Needs your decision

1. **The policy sheet.** The gate checks that the sheet is complete and that your confirmation it went (or will go) to your controller is recorded in `docs/domain/policy-sheet-status.md`. Their sign-off gates Stage 6.
2. **The Spark Connect restart defect** is still open (described in the Stage 2a summary), for a developer to fix before Stage 10.
3. **Memory for Stage 3.** The roadmap sizes the Stage 3 matching pod at about 4 GB. The local jobs container is now 3,072 MiB, so Stage 3 will need either a larger jobs container or a revised memory budget, which I will bring to you with measurements at the start of Stage 3.

## Carried forward

- Stage 3 (the matching engine) reads these landing tables through the default data sources and scores itself against this answer key.
- D19 stays open until Stage 5.
- The documentation polish pass (voice and spelling) is deferred to the end of the project.
