# ADR-0020: the period close

Status: proposed with Stage 8b. Date: 2026-10-02. Implements, on sample inputs, the second part of the roadmap's Stage 8:
- a soft close;
- the close checklist and flux analysis, as configuration the accounting team authors and approves;
- `PeriodCloseWorkflow`: a hard close that waits for the checklist.

Account reconciliations, which join the close gate, are 8c's.

## Context

You signed off 8a and chose 8b next, on sample inputs ([stage-8-plan.md](../stages/stage-8-plan.md), "8b decisions").
- **Periods.** Until now a period moved `open -> closed -> locked`, and a close waited only for journals in approval or posting (ADR-0016) and for a journaling run (ADR-0019). Nothing tracked the close's own work.
- **`PeriodCloseWorkflow`.** The roadmap planned it as a Temporal workflow, but ADR-0007 dropped Temporal for Postgres state machines and idempotent scheduler tasks.
- **Calendars.** Business calendars live in the lake (`ref.calendars`, with each entity's calendar code in `ref.entities`), where the engine reads them. The app had no way to read them, and the read path serves only entity-scoped tables (Stage 1b).
- **The BlackLine research** has no field-level description of task lists or variance analysis. The design is ours, borrowing BlackLine's `{amount, percent, operator}` threshold shape and its per-module period flags.

## Decision

**A soft close.** Periods gain a `soft_closed` status: `open -> soft_closed -> closed -> locked`, with who and when. A soft-closed or closed period can be reopened with a reason.
- **It stops automated entries and keeps manual ones.** Recurring journals skip a soft-closed period and tell their preparer. Automatic reversals aren't created into it. The journaling engine books an event dated in it in the first open period, as it does for a closed one (`journal_ref` now treats `soft_closed` as closed, as the reference runner already treated anything not open). People can still create, edit, import, approve, reverse, batch and post journals dated in it, because the close review finds adjustments.
- **One rule in one place.** `store.TAKES_MANUAL` (open, soft-closed) and `store.TAKES_AUTOMATED` (open) are the two predicates, and every call site that asked "is it open" uses one of them. Each is tested in the soft-closed state.
- **It waits for a journaling run,** as a close does, because the run's snapshot read the period statuses.

**The close checklist is configuration:** a `close_checklist` kind, one active at a time, through the maker-checker API like every other rule.
- **Each task** has an id, a name, instructions, a due day, the tasks it depends on, and either a preparer (a manual task) or a system check (an automatic task). A manual task needs a reviewer who isn't its preparer. An automatic task may have a reviewer. A task can be limited to some entities, and its dependencies must apply wherever it does.
- **The due day** counts business days from the period end. BD+1 is the first business day after it, BD-1 the last business day in it, and there is no BD0 (at most 20 either way). Test vectors pin the convention, including a month ending on a weekend and one ending on a holiday.
- **Checked when saved:** unique ids, known dependencies, no cycles, distinct people, entities in the entity set. On activation, each preparer must hold `close.prepare`, and each reviewer `close.review`, in every entity their task applies to.

**Generating a period's checklist.** The controller generates it for an entity and period from the active checklist.
- **Frozen.** The tasks are copied in dependency order with their due dates, and the checklist records its version and the calendar. A later version never changes a close in progress, as journal routing is frozen at submit (ADR-0016).
- **Due dates from the entity's calendar, read narrowly.** `close_platform/query/calendars.py` reads the entity's calendar code from `ref.entities`, then that calendar's days from `ref.calendars`. Each table is read at one pinned version, and the calendar's version is recorded with the checklist. Like 7c's rate lookup (ADR-0018), it isn't a query spec, the Spark-ban lint covers it, and it runs before the transaction, after the idempotent replay check. The calendar code isn't copied into configuration, so there is one source.
- **No silent dates.** Generation is refused when the entity has no calendar code, or when the calendar doesn't reach a due date. Inside its transaction it checks that the active checklist is still the version it planned from.

**Working the tasks:** a task moves `open -> prepared -> done`, and one without a reviewer is done when prepared.
- **Who acts.** Only the assigned preparer prepares a manual task, and only after the tasks it depends on are done. Only the assigned reviewer reviews it or sends it back with a reason. A reviewer never reviews their own preparation, which the table's constraints also enforce. The controller can reassign a task that isn't done (to people holding the role), or reopen one with a reason.
- **A 409, not a 403.** Acting on a task assigned to someone else is a 409 for a role that may act on close tasks. A 403 means the role can't act at all.
- **Automatic tasks.** A user runs the task's check. A GL check reads the GL through the read path as that user, and so with their entities. A failing check leaves the task open with its evidence. Each check takes Postgres time before it reads anything. The checks are:
  - `journals_settled`: nothing in the period waits for approval or posting;
  - `suspense_clear`: the chart's suspense account has no closing balance;
  - `gl_balanced`: the period's balances balance;
  - `erp_ties`: the latest accepted ERP extract ties (ADR-0019);
  - `flux_reviewed`: the flux review is current.
- **Locking.** Tasks move only while their period takes entries, read `FOR SHARE`, so a close can't interleave with a task move.
- **History.** Every move is audited and kept in the insert-only `close_task_events`.
- **Reminders** are a scheduler task that reads Postgres only. It reminds whoever a due task waits on (its preparer, or its reviewer once it is prepared) once a day. It tells the controller once a day when the task is overdue by the checklist's number of days. A notification's key holds its day, so the task is idempotent across instances.

**Flux analysis** is configured as a `flux_rules` kind, one active at a time.
- **Lines.** Each account is compared with the prior period over both GL sources (ADR-0019): closing balances for asset, liability and equity accounts, and the period's activity for revenue and expense accounts.
- **Thresholds** are per account type, with per-account overrides. Each has an amount per functional currency and a percent, joined by `and` or `or` (BlackLine's shape). A change from zero is over any percent. An account the rules don't cover, or a currency without an amount, needs an explanation for any change. On activation, every threshold needs an amount for every functional currency in the entity set.
- **Explanations** are bound to the amounts the server computed when one was written, never to amounts a client sends. A line's latest explanation counts, and it is stale once the line's amounts change.
- **The review.** Anyone with `close.review` in the entity signs it off once every line over its threshold has a current explanation, and only if they wrote none of them.
  - **Its hash** covers every line and the explanation each one needing it had (`flux_hash`).
  - **When it stops being current.** The `flux_reviewed` check passes only while the current flux hashes the same, so a later posting or a new explanation makes the review stale.
- **Insert-only.** Explanations and reviews are kept, and audited.

**The hard close, `PeriodCloseWorkflow` without Temporal.** A period with a generated checklist is soft-closed first, and closes only when every task is done.
- **No stale GL checks.** No GL check may predate the ledger's last change, which is the latest of these Postgres times:
  - a journal reaching `posted` (set after its Delta commit);
  - a journaling run settled with lines or rollups written;
  - an ERP extract accepted;
  - for the flux review, a later explanation.

  A check records its time before its read starts. So a change during the read makes the check stale, never the other way round, and a stale check is refused with "check it again".
- **The earlier refusals still apply.** Journals waiting, and a journaling run in progress.
- **Without a checklist,** a period closes from open as before (7a), so the earlier stages' behaviour and tests stand.

**Who.**
- **New actions:** `close.prepare`, held by journal preparers and the controller, and `close.review`, held by approvers at every level and the controller.
- **Existing actions:** `period.manage` covers generating, reassigning and reopening a task, and the soft close. `journal.read` covers the checklist, task and flux reads.

**Governance.**
- **Migration 0008** adds the two kinds, `soft_closed` (with its own constraint, so a soft-closed period needs its timestamp), the checklist, task, history, explanation and review tables, and task notifications. The downgrade refuses while any period is soft-closed.
- **Coverage.** The new operations join the authorization matrix, the route inventory and the idempotency suite.

## Consequences

- **The accounting team configures its own close:** the tasks, who prepares and reviews each, when each is due, the system checks, and the flux thresholds. Every change goes through maker-checker approval.
- **A close is evidence:** each task's preparer, reviewer, notes, check results and history; each flux explanation with the numbers it explained; and the review with the hash of what it signed off.
- **A soft close changes where automated entries land.** Events dated in a soft-closed period are booked in the next open one, as prior-period adjustments. On the stand-in's frozen ledger (ADR-0019), a rerun after a soft close is refused like one after a close.
- **On the `tiny` sample,** every July account is new against an empty June, so any July line over its amount threshold needs an explanation. On the stack, none of the 19 lines reaches the sample's amounts, so the stack test also explains the line with the largest change.
- **Calendars come from the lake.** The generator writes 2026's holidays only. Production calendars come from your reference data into `ref.calendars`, a Stage 10 input.
- **Not in 8b:**
  - account reconciliations joining the close gate (8c);
  - flux against the same period last year, or against a budget;
  - flux by dimension;
  - attachments on tasks and explanations (attachments belong to journals today);
  - email notifications (ADR-0016 keeps them in the app).
