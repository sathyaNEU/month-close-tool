# Stage 4 sign-off

Stage 4, the governance kernel, was signed off on 2026-09-29. It was built out of order, before Stage 3 is finished. The gate result is in [stage-4-report.md](stage-4-report.md): 62 of 62 criteria passed, and all 33 steps succeeded, with every Stage 3 reference-slice, 2b, 2a, 1b and 1a check re-run as regression.

The sign-off accepts:
- [ADR-0014](../adr/0014-governance-kernel.md), including the amendment to D12: identity comes from the Databricks Apps proxy, there is no dev JWT issuer or Keycloak profile, and machine clients wait for Stage 10;
- the query-template registry replaced by typed query specs and a route-inventory test;
- two rules the user decided at the sign-off, implemented and tested in the final gate run:
  - reading a match set that spans several entities needs a role in any one of them, and every change needs the role in all of them;
  - `audit.read` at `GLOBAL` shows every audit event.

The report's summary records the five gate runs:
- the first failed on two of my criterion definitions and a Stage 1a test that pinned the schema head;
- the third added two criteria that a review found missing;
- the fifth confirmed the fix after the Mac slept through the fourth.

Next, by the user's choice, Stage 6's inputs:
- the accounting-policy sheet, answered and signed by the controller;
- the chart of accounts;
- the dimensions.

What to send is in [stage-6-inputs.md](../domain/stage-6-inputs.md). When they arrive, the journal oracle is built first, in pure Python.

Carried forward:
- the Spark rule compiler, and with it the rest of Stage 3 and most of Stage 5;
- the Spark Connect restart defect, before Stage 10;
- a production way to create the first `platform_admin`, in Stage 10;
- D19, until Stage 5.
