# Stage 2a sign-off

Stage 2a was signed off on 2026-09-28. The gate result is in [stage-2a-report.md](stage-2a-report.md): PASS, 35 of 35 criteria, with every Stage 1b and 1a check re-run as regression.

The sign-off accepts the deviations listed in the report's summary:
- The Spark Connect memory fix uses two measured settings (`MALLOC_ARENA_MAX=2` and `-XX:ActiveProcessorCount=2`) instead of a larger container limit. Its peak was 81% of the limit in the gate.
- The regex subset is stricter than RE2.
- DSL integer literals are limited to the JSON-safe range.
- `close-contracts` now depends on Pydantic.

It also accepts [ADR-0011](../adr/0011-rule-dsl-and-configuration-contracts.md), the rule DSL and configuration contracts, and the four domain documents in `docs/domain/`.

Carried into Stage 2b and beyond:
- The Spark Connect restart defect: a developer applies the fix before Stage 10, and restarting the app is the workaround until then.
- Sending the accounting-policy sheet to the controller, recorded at the Stage 2b gate.
- The SeaweedFS memory margin (718 of 768 MiB), which is the first task of Stage 2b.

The user asked for Stage 2b to be built autonomously, with a report when its gate is complete.
