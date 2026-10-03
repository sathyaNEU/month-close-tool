# Stage 7b sign-off

Stage 7b was signed off on 2026-10-01. It covers posting to the Databricks ledger, the ERP write-back, trial balance and XLSX import, and was built on sample inputs. The gate result is in [stage-7b-report.md](stage-7b-report.md): 97 of 97 criteria passed on the second run, and all 34 steps succeeded, with every earlier gate's checks re-run as regression. The first run passed 96 of 97. It failed only on a test in the Jobs API emulator's suite that still listed the bundle's three jobs, before `post_journals` became the fourth.

The sign-off accepts:
- **[ADR-0017](../adr/0017-posting-and-the-erp-write-back.md):**
  - posting runs per entity, exactly once, through the `post_journals` job and the reconcile task;
  - the read-only `close_ledger_reader` role, its `posting_queue` view, and the `pg-roles` step that creates it;
  - the ledger's `posted_at` being the run's request time;
  - the ERP write-back and the stub ERP.
- **The two differences from the plan:**
  - the tie-out compares line counts, debits and credits rather than hashes, with a per-journal hash comparison in Stage 10;
  - the posting-against-close race test shows the close refused with and without the locks, since it can't fail without them.
- **The contract snapshot change:** the table registry gains `ledger.journal_lines` and `ledger.journal_postings` (DDL V007).
- **The new dev user `erp` (1006),** the stub ERP's integration client in US01.

Decided at the sign-off:
- **D19 keeps its default for now.**
  - An ERP stays the general ledger's book of record, `posted` means committed to the Delta ledger, and `erp_confirmed` is optional.
  - It is still to be confirmed before production, as the roadmap planned (Stage 5).
- **`psycopg[binary]` is approved as an engine dependency.** The posting job uses it to read Postgres, so Databricks job clusters install it with the engine wheel. `et-xmlfile` came with the approved `openpyxl`.
- **Next: 7c on sample inputs,** multi-currency manual journals, which finishes Stage 7.

The samples stay test-only. Before production they must be replaced by the real inputs:
- Stage 6's: [stage-6-inputs.md](../domain/stage-6-inputs.md);
- Stage 7's: [stage-7-inputs.md](../domain/stage-7-inputs.md), plus which ERP will confirm postings.

Carried forward:
- **7c:** multi-currency manual journals, with rates frozen at submit through a narrow, typed rate lookup.
- **Blocked on the Spark rule compiler:** the engine's automated entries in the journal population, and GL-history risk factors.
- **Stage 10:**
  - the posting job's Lakebase access (a recorded risk);
  - a real ERP integration in place of the stub;
  - a per-journal hash comparison in the tie-out.
- **Stage 5:** the configuration-change lock, and D19's confirmation.
- **The rule builder:** save-time checks for required dimensions and for mapping rows keyed on policy switches.
- **Also open:** the Spark Connect restart defect, and the first production `platform_admin`.
