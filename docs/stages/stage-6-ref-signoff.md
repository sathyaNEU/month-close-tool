# Stage 6 first slice sign-off

The first slice of Stage 6 was signed off on 2026-09-30: journaling built on sample inputs, with the rules as configuration. The gate result is in [stage-6-ref-report.md](stage-6-ref-report.md): 73 of 73 criteria passed on the first run, and all 34 steps succeeded, with every Stage 4, Stage 3 reference-slice, 2b, 2a, 1b and 1a check re-run as regression.

The sign-off accepts:
- [ADR-0015](../adr/0015-journaling-configuration-and-reference-engine.md) and the sample semantics:
  - suspense for data problems, a failed event type for a configuration problem, and the 0.5% circuit breaker;
  - prior-period adjustments into the first open period;
  - corrections by row hash.
- **The contract snapshot changes:** 5 new snapshots, and 5 new `ConfigKind` values in the envelope snapshots.
- **The retirement rule,** which changes signed-off Stage 4 behaviour and amends [ADR-0014](../adr/0014-governance-kernel.md).
  - A configuration that an active one uses can't be retired, including a data source that an active match set uses.
  - A new version can't break a live definition.
  - The transaction lock for changes made at the same instant comes with Stage 5's hardening, by the user's choice.
- **The generator's FX rates** now run 130 days past the period, a change to signed-off Stage 2b data. Rows are only appended.

The sample configuration stays test-only. Before anything goes live in production, the samples must be replaced by the real inputs ([stage-6-inputs.md](../domain/stage-6-inputs.md)):
- the accounting-policy sheet, answered and signed by the controller, including the incentive-cap question added on 2026-09-30;
- the chart of accounts;
- the dimensions.

Next, by the user's choice: Stage 7, the journal workflow, built on sample inputs as this slice was.

Carried forward:
- the Spark rule compiler, and with it the rest of Stage 3, most of Stage 5 and the Spark journaling job;
- the transaction lock for configuration changes, and a check of a data source's new version against the active match sets, both with Stage 5's hardening;
- save-time checks for required dimensions and for mapping rows keyed on policy switches, with the rule builder;
- the Spark Connect restart defect, before Stage 10;
- a production way to create the first `platform_admin`, in Stage 10;
- D19, until Stage 5.
