# Stage 7c sign-off

Stage 7c, multi-currency manual journals, was signed off on 2026-10-01. It was built on sample inputs, and it completes Stage 7. The gate result is in [stage-7c-report.md](stage-7c-report.md): 109 of 109 criteria passed on the first run, and all 34 steps succeeded, with every earlier gate's checks re-run as regression.

The sign-off accepts:
- **[ADR-0018](../adr/0018-multi-currency-manual-journals.md), with its three rules:**
  - each journal is converted at its accounting date's daily spot rate, with no fallback, so a month-end entry waits for that day's rate;
  - pairs never triangulate through a third currency;
  - a reversal that is still the plain flip of its original reuses the original's rate, so it fully undoes the original.
- **The contract changes:**
  - a journal currency, template currencies and a recurring journal's currency;
  - the ledger's rate columns (DDL V008);
  - the drill-down's functional amounts and line kind.
- **The amendments:**
  - ADR-0002's allowlist gains feature 19, `ADD COLUMNS` on an append-only table;
  - ADR-0017's note on XLSX replays: a replay now returns the stored response without parsing the workbook again.

Decided at the sign-off:
- **Images take every Debian security fix at build time.** Both Dockerfiles run `apt-get -y upgrade` instead of upgrading named packages (OpenSSL in 7a, PCRE2 in 7c). Builds on different days may differ, and the CycloneDX SBOM records exactly what each image installed. This is Stage 8's first task.
- **Next: Stage 8 on sample inputs,** close management.

The samples stay test-only. Before production they must be replaced by the real inputs:
- Stage 6's: [stage-6-inputs.md](../domain/stage-6-inputs.md);
- Stage 7's: [stage-7-inputs.md](../domain/stage-7-inputs.md), plus which ERP will confirm postings;
- 7c's: the rate source feeding `ref.fx_rates`.

Carried forward:
- **Stage 8:** revaluation of foreign-currency balances at period end.
- **Consolidation:** translation to a reporting currency.
- **Blocked on the Spark rule compiler:**
  - the engine's automated entries in the journal population;
  - GL-history risk factors.
- **Stage 10:**
  - a real rate feed;
  - the posting job's Lakebase access;
  - a real ERP integration;
  - a per-journal hash comparison in the tie-out.
- **Stage 5:** the configuration-change lock, and D19's confirmation.
