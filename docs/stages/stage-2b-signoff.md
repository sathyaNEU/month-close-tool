# Stage 2b sign-off

Stage 2b was signed off on 2026-09-28. The gate result is in [stage-2b-report.md](stage-2b-report.md): 45 of 46 criteria passed, and every one of the 30 steps succeeded, with every Stage 2a, 1b and 1a check re-run as regression.

The open criterion is the policy sheet. The sheet is complete, but the user did not know whether it had gone to the controller, so no confirmation is recorded ([policy-sheet-status.md](../domain/policy-sheet-status.md)). The user signed off with that item open. It carries forward and must be settled before Stage 6, whose posting rules follow the sheet's answers.

The sign-off accepts:
- the deviations in the report's summary: the jobs container at 3,072 MiB, the duplicate break as a double-posted seller payable, 13 break types, an answer key for the Stage 3 templates only, and the migrations shipped inside the engine wheel;
- [ADR-0012](../adr/0012-table-contracts-and-the-seeded-generator.md), covering the table contracts, migrations, default data sources and the generator.

Carried forward:
- The Spark Connect restart defect, for a developer before Stage 10.
- The Stage 3 memory question: the roadmap sizes the matching job at about 4 GB, and it will be measured at the start of Stage 3.
- The policy sheet, before Stage 6.

The user asked for Stage 3 to start now and be built autonomously, with a report when its gate is complete.
