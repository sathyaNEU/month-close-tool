# Stage 7a sign-off

Stage 7a, the journal workflow in the control plane, was signed off on 2026-09-30. It was built on sample inputs. The gate result is in [stage-7a-report.md](stage-7a-report.md): 84 of 84 criteria passed on the second run, and all 34 steps succeeded, with every earlier gate's checks re-run as regression. The first run passed 83 of 84. It failed only on two new OpenSSL vulnerabilities in the images' Debian base, now patched.

The sign-off accepts:
- **[ADR-0016](../adr/0016-journal-workflow.md):**
  - journals in each entity's functional currency in 7a;
  - attachments stored in Postgres;
  - hierarchical approval authority (an L2 approver may also approve level 1);
  - the sample risk-score factors and weights.
- **The contract snapshot changes:** 5 new snapshots, and 4 new `ConfigKind` values in the envelope snapshots.
- **The new dev user erin (1005),** and the journal roles given to alice and dave.
- **The change to the Stage 1a scheduler test,** whose minimum tick count now applies only to the 1-second tasks.

Decided at the sign-off:
- **`openpyxl` is approved** for XLSX import. It is added hash-pinned, and built and tested with 7b.
- **Next: Stage 7b on sample inputs.** It starts by fixing the OpenSSL pin, which would break image builds once Debian ships its next OpenSSL update. Then it plans posting to the Databricks ledger and the ERP write-back, with the D19 default (an ERP stays the book of record).

The samples stay test-only. Before production they must be replaced by the real inputs:
- Stage 6's: [stage-6-inputs.md](../domain/stage-6-inputs.md);
- Stage 7's: [stage-7-inputs.md](../domain/stage-7-inputs.md).

Carried forward:
- **7b:**
  - posting and `erp_confirmed`;
  - the full journal population with the engine's entries;
  - GL-history risk factors;
  - multi-currency manual journals;
  - XLSX import.
- **Stage 5:** the configuration-change lock.
- **The rule builder:** save-time checks for required dimensions and for mapping rows keyed on policy switches.
- **The Spark rule compiler,** the Spark Connect restart defect, the first production `platform_admin`, and D19.
