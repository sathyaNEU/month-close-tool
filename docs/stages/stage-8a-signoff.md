# Stage 8a sign-off

Stage 8a, the general ledger, was signed off on 2026-10-02. It was built on sample inputs, and it is the first of Stage 8's three parts. The gate result is in [stage-8a-report.md](stage-8a-report.md): 118 of 118 criteria passed on the first run, and all 34 steps succeeded, with every earlier gate's checks re-run as regression.

The sign-off accepts:
- **[ADR-0019](../adr/0019-the-general-ledger-on-databricks.md):**
  - jobs get their configuration as a content-addressed snapshot in Postgres, not on object storage (this amends the roadmap);
  - after an entity's first successful journaling run, a changed configuration, more landing data or a period closed since is refused, not applied, until the Spark journaling job brings the engine's correction state;
  - an ERP stays the book of record (D19's default), and the tie-out reports every difference without correcting either side.
- **The stand-in job, approved separately.** `journal_ref` runs the pure-Python reference engine as a Lakeflow job, on sample volumes only, until the Spark rule compiler exists. The Spark journaling job then replaces it and writes the same tables.
- **The contract changes:**
  - the table registry gains the rollups' functional amounts (DDL V009) and natural keys for the subledger lines and rollups;
  - the queryable registry gains the rollups.
- **Three changes to behaviour Stage 7b's sign-off covered:**
  - `/gl/trial-balance` covers both GL sources, and its response drops `version`;
  - posting's busy 409 is titled "The entity's ledger is busy", because journaling shares the lease;
  - the posting job checks its fence before its feed commit too (ADR-0007).

Decided at the sign-off:
- **Next: 8b on sample inputs:** the close checklist, flux analysis with explanations, and `PeriodCloseWorkflow`'s soft close.

The samples stay test-only. Before production they must be replaced by the real inputs:
- Stage 6's: [stage-6-inputs.md](../domain/stage-6-inputs.md);
- Stage 7's: [stage-7-inputs.md](../domain/stage-7-inputs.md), plus which ERP will confirm postings;
- 7c's: the rate source feeding `ref.fx_rates`;
- 8a's: which ERP, how it exports a trial balance, and whether its account codes need a mapping for the tie-out.

Carried forward:
- **8b:** the close checklist, flux analysis and the soft close.
- **8c:** account reconciliations.
- **Still to place in Stage 8:** revaluation of foreign-currency balances at period end.
- **Consolidation:** translation to a reporting currency.
- **Blocked on the Spark rule compiler:**
  - the Spark journaling job, which replaces the stand-in;
  - the engine's correction state, its automated entries in the journal population, and GL-history risk factors.
- **Stage 10:**
  - the jobs' Lakebase access (the posting queue and configuration snapshots);
  - a real ERP integration with its trial-balance extract;
  - a per-journal hash comparison in the tie-out.
- **Stage 5:** the configuration-change lock, and D19's confirmation.
