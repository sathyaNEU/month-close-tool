# Stage 3 reference slice sign-off

The Stage 3 reference slice was signed off on 2026-09-28. The gate result is in [stage-3-ref-report.md](stage-3-ref-report.md): 51 of 51 criteria passed, and all 33 steps succeeded, with every Stage 2b, 2a, 1b and 1a check re-run as regression.

The slice is the part of Stage 3 that doesn't need the Spark rule compiler:
- the pure-Python reference matcher;
- templates 1, 2, 3, 6, 9 and 10 as 12 match sets;
- their golden tests against the answer key.

The sign-off accepts:
- [ADR-0013](../adr/0013-reference-matcher-semantics.md), the matching semantics the Spark matcher will be held to, including the rule that a matching set never groups items from different entities (intercompany sets excepted);
- the corrections to signed-off Stage 2b work:
  - the template 9 answer key: a carrier correction is an open exception beside the automatic label and transportation match, or a suggestion when it ties;
  - the template 6 answer key: a payout over a batch holding a duplicated payable is a suggestion;
  - the fix to the default carrier data source's entity mapping;
  - the JSON Schema snapshot updates, including the new template match sets.

Stage 3 stays open. The normalize job, the Spark matcher, the commands and the crash-safety work all build on the Spark rule compiler, which still needs a developer.

The user chose to start Stage 4 (the governance kernel) now, out of order, since it doesn't depend on the matcher.

Carried forward:
- the Spark rule compiler, and with it the rest of Stage 3 and its memory measurement;
- the Spark Connect restart defect, before Stage 10;
- the policy sheet, before Stage 6;
- D19, until Stage 5.
