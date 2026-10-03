## What this part delivers

On 2026-10-02 you signed off 8a and chose 8b next, on sample inputs. This part adds the close itself: a soft close, the close checklist and flux analysis as configuration, and a hard close that waits for them. The plan is the "8b decisions" in [stage-8-plan.md](stage-8-plan.md), and the decisions are in [ADR-0020](../adr/0020-the-period-close.md).

- **A soft close.** A period can be soft-closed before it closes. It then refuses automated entries: recurring journals skip it, automatic reversals aren't made into it, and the journaling engine books events dated in it in the next open period. People can still create, import, approve and post journals in it, because the close review finds adjustments.
- **The close checklist is configuration** the accounting team authors and approves, like every other rule.
  - **Each task** has a due day in business days from the period end (BD-1 is the last business day of the period, BD+1 the first after it), the tasks it depends on, and either a preparer and a reviewer, or a system check.
  - **Generating.** The controller generates a period's checklist. Its tasks are frozen, with due dates from the entity's business calendar, read from the lake through a narrow lookup like 7c's rates.
- **Working the tasks.**
  - **Who acts.** Only the assigned preparer prepares a task, once its dependencies are done, and only its reviewer signs it off or sends it back. Nobody reviews their own work.
  - **System checks:** journals settled, suspense clear, trial balance balanced, GL tied to the ERP, flux reviewed.
  - **Reminders** go to whoever a due task waits on, once a day, and to the controller when it is overdue.
- **Flux analysis**, also configuration.
  - **The comparison.** Each account against the prior period: closing balances for the balance sheet, and the period's activity for the income statement.
  - **Thresholds** are an amount and a percent, per account type with per-account overrides.
  - **Explanations** are bound to the numbers the server computed, and go stale when those change.
  - **The review** is signed off by someone who wrote none of the explanations.
- **The hard close (`PeriodCloseWorkflow`).** A period with a checklist is soft-closed first, and closes only when every task is done and no system check is older than the ledger's last change. A period without a checklist closes as before.

## Results

| Check | Result |
|---|---|
| App tests (Postgres) | 217 passed, plus the audit-chain residual as a strict xfail; 15 of them new for the close |
| Unit tests | 513 passed, contracts coverage 96%; 35 new, including the due-date vectors (a month ending mid-week, on a weekend and on a holiday, and BD+n over Labor Day and New Year) |
| Calendar lookup on Spark | an entity's calendar days at one pinned version, and nothing without a calendar code or the tables |
| Spark tests | the stand-in books a soft-closed July's events in the first open period |
| Authorization matrix | 237 operations × 12 roles × 3 scopes = 8,532 checks, 0 unexpected allows or denies |
| End-to-end suite | 54 passed on a fresh stack in 21 minutes, before the gate |
| Stack | dave generates US01's July checklist from the sample: 9 tasks, with due dates from US_FED in the lake (BD-1 July 31, BD+1 August 3, BD+5 August 7). He soft-closes July. The flux has 19 lines, none over the sample's thresholds, and the largest is explained anyway before erin reviews it. Every task is done, and all five system checks pass after the ERP's tying extract is uploaded. dave closes July |

## Found and fixed while building

1. **A check could have read the ledger and gone stale unnoticed.** A system check reads the GL outside any transaction, so the ledger can change after it. Each check now records Postgres time before it reads. The close refuses one older than the ledger's last change: a journal posted, a journaling run with lines written, or an ERP extract accepted. For the flux review, a later explanation counts too. A test changes the ledger after a check and sees the close refused until the check runs again.
2. **The engine and the app disagreed on what "closed" means.** The journaling stand-in treated only closed and locked periods as closed, while the reference runner treated anything not open as closed. Both now treat a soft-closed period as closed for automated entries, and the Spark test books a soft-closed July's events in September.
3. **Four call sites asked "is the period open" on their own.** Manual entries (journals, imports, approvals, posting) and automated ones (recurring journals, automatic reversals) now ask one of two rules in one place, and each call site is tested with the period soft-closed.
4. **Two of the earlier tests would have broken quietly.**
   - **Reminders.** The idempotency suite gave the sample's reviewer a level 2 approver's role, which made a 7a journal reminder go to two approvers. The reviewer now gets the controller's role, which can review close tasks without joining the journal approvers.
   - **The readiness test** pinned the schema head. It now reads the head from the app.

## Where the build differs from the plan

1. **The checklist's preparers and reviewers are user ids,** like recurring journals' preparers, so the sample names the local development users. Your real checklist will name your people.
2. **Close tasks have no attachments.** Attachments belong to journals today. A task has a note, and an automatic task has its evidence.
3. **Flux compares with the prior period only.** The same period last year and budgets come later, with dimension-level flux.
4. **Reminders use the date in UTC,** not the entity's timezone.

## Needs your decision

1. **Accept ADR-0020.**
2. **Accept the contract changes:**
   - two configuration kinds, `close_checklist` and `flux_rules`, with their JSON Schema snapshots;
   - the period status `soft_closed`;
   - two actions, `close.prepare` (journal preparers and the controller) and `close.review` (approvers and the controller).
3. **The real inputs** before production:
   - your close checklist, with each task's preparer, reviewer, due day and dependencies;
   - your flux thresholds by account type, and any accounts that need their own;
   - who may soft-close and reopen periods;
   - your business calendars, feeding `ref.calendars`.

   These join the inputs already requested.
4. **Next:** 8c on sample inputs (account reconciliations joining the close gate), or something else.

## Carried forward

- **8c:** account reconciliations, which join the close checklist as a system check.
- **Still to place in Stage 8:** revaluation of foreign-currency balances at period end.
- **Later:**
  - flux against the same period last year or a budget, and by dimension;
  - attachments on close tasks and explanations;
  - email notifications.
- **Blocked on the Spark rule compiler:** the Spark journaling job, and the engine's correction state.
- **Stage 10:** real business calendars and ERP extracts, and the jobs' Lakebase access.
