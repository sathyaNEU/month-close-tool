# Stage 8b sign-off

Stage 8b, the period close, was signed off on 2026-10-03. It was built on sample inputs, and it is the second of Stage 8's three parts. The gate result is in [stage-8b-report.md](stage-8b-report.md): 128 of 128 criteria passed on the first run, and all 34 steps succeeded, with every earlier gate's checks re-run as regression.

The sign-off accepts:
- **[ADR-0020](../adr/0020-the-period-close.md):**
  - **A soft close.** It stops automated entries (recurring journals, automatic reversals, the journaling engine) and keeps people's adjustments and posting.
  - **The close checklist and flux rules are configuration** the accounting team authors and approves. A period's checklist is frozen when generated, with business-day due dates from the entity's calendar, read through a narrow lookup.
  - **Flux.** Explanations are bound to the amounts the server computed, and the review is signed off by someone who wrote none of them, bound to a hash of the lines.
  - **The hard close.** A period with a checklist closes only from soft-closed, when every task is done and no system check is older than the ledger's last change. A period without one closes as before.
- **The contract changes:**
  - the `close_checklist` and `flux_rules` configuration kinds, with their JSON Schema snapshots;
  - the `soft_closed` period status;
  - the `close.prepare` action (journal preparers and the controller) and the `close.review` action (approvers at every level and the controller).

Decided at the sign-off:
- **Next: 8c on sample inputs,** account reconciliations, in Claude Code in the cloud. The repository is on GitHub, and [CLAUDE.md](../../CLAUDE.md) and the resume note ([stage-1b-next-steps.md](stage-1b-next-steps.md)) say how a session starts.

The samples stay test-only. Before production they must be replaced by the real inputs:
- Stage 6's: [stage-6-inputs.md](../domain/stage-6-inputs.md);
- Stage 7's: [stage-7-inputs.md](../domain/stage-7-inputs.md), plus which ERP will confirm postings;
- 7c's: the rate source feeding `ref.fx_rates`;
- 8a's: which ERP, how it exports a trial balance, and whether its account codes need a mapping for the tie-out;
- 8b's: the close checklist, the flux thresholds, who may soft-close and reopen periods, and the business calendars feeding `ref.calendars`.

Carried forward:
- **8c:** account reconciliations, which join the close checklist as a system check.
- **Still to place in Stage 8:** revaluation of foreign-currency balances at period end.
- **Later:**
  - flux against the same period last year or a budget, and by dimension;
  - attachments on close tasks and explanations;
  - email notifications.
- **Blocked on the Spark rule compiler:** the Spark journaling job, and the engine's correction state.
- **Stage 10:**
  - real business calendars and ERP extracts;
  - the jobs' Lakebase access;
  - a real ERP integration.
- **Stage 5:** the configuration-change lock, and D19's confirmation.
