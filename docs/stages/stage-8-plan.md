# Stage 8 plan: close management (on sample inputs)

On 2026-10-01 you signed off Stage 7c, which completed Stage 7, and chose Stage 8 next, on sample inputs. This plan was approved automatically, as you asked for plans. D19 keeps its default: an ERP stays the general ledger's book of record.

## What a survey of the code found first

- **The Databricks GL holds only manual journals.**
  - **Why.** The journaling engine's output tables (`ledger.subledger_lines`, `ledger.gl_rollup_lines`) have no writer, because the Spark journaling job waits for the rule compiler. The pure-Python reference engine produces the same lines, but only inside tests and the gate's scoring tool.
  - **Effect.** Without the engine's journals, a trial balance, a roll-forward, flux analysis or a reconciliation of PSP clearing, seller payables or stored value would compare almost nothing.
- **There is no ERP trial-balance extract, and no PSP balance report.**
- **Templates 8 and 12 are reconciliations, not matches.** The stored-value roll-forward and the clearing-account zero-out compare balances, so they belong with account reconciliations, not the transaction matcher.
- **Nothing persists match results,** so no in-transit items exist to draw reconciling items from. That waits for the matching engine (the compiler).

## Three parts, each with its own gate and sign-off

- **8a, the general ledger** (`make gate-8a`):
  - the engine's journals in Delta, through a sample stand-in job;
  - balances and roll-forward from the whole Databricks GL;
  - an ERP trial-balance extract, and the Delta-to-ERP tie-out.
- **8b, the close** (`make gate-8b`):
  - the close checklist (task templates, business-day due dates, dependencies, preparer and reviewer);
  - flux analysis with explanations;
  - `PeriodCloseWorkflow`, adding a soft close, so a period closes only when its checklist and flux review are done.
- **8c, account reconciliations** (`make gate-8c`):
  - reconciliation profiles, with auto-certification rules, risk ratings and frequency;
  - a supporting-balances job for templates 8 and 12;
  - reconciling items with aging;
  - certification with separation of duties, and reconciliations joining the close gate.
- **Each part** gets its own detailed decisions and gate when it starts. 8a's are below.

## 8a decisions

1. **Jobs get their configuration as a content-addressed snapshot.** The roadmap planned this on object storage, but the app holds no object-store keys, so the snapshots live in Postgres.
   - **Starting a run** writes everything the run depends on as canonical JSON in `config_snapshots`, keyed by its SHA-256, and passes only the hash. That is:
     - the active journaling configuration (chart, dimensions, policy, mappings, event types and definitions);
     - the active entity set;
     - the entity's period statuses.
   - **Snapshots are insert-only.**
   - **The job reads its snapshot** as the jobs' read-only role (`close_ledger_reader`), through a view showing only snapshots of runs in progress, like 7b's `posting_queue`. It recomputes the hash and refuses a mismatch.
   - **The future Spark matching and journaling jobs** take their configuration the same way.
2. **A sample stand-in for the Spark journaling job.** `journal_reference` runs the pure-Python reference engine, the one held to the oracle and the invariants in Stage 6. It writes the engine's subledger lines and GL rollups to Delta for one entity.
   - **Its run is like a posting run.** It is started through the coordinator, under the entity's `ledger:<entity>` lease with fencing, as a controller's `journaling.run`. A reconcile task records its outcome.
   - **Write order.** Subledger lines are written first and rollups last, both insert-only and keyed on the engine's deterministic ids. A crash between the two is completed by the retry.
   - **Periods come from Postgres, not from `ref.periods`.** The generator marks every period open there, and nothing syncs it. The engine posts an event dated in a closed or locked period to the first open one, as a prior-period adjustment, as in Stage 6. A period can't close while a journaling run for its entity is in progress.
   - **One source of functional currencies.** The job refuses to run if the snapshot's entity set and `ref.entities` disagree on an entity's functional currency.
   - **A rerun never double-books.** The engine's ids hash the event and its grouping, so a different configuration or more landing data would give different lines or stale rollups next to the old ones.
     - **What gets pinned.** The first successful run for an entity pins its configuration hash and a digest of its inputs.
     - **Reruns.** A later run must match both. A matching rerun writes nothing, and anything else is refused before writing.
     - **Changes.** Applying changes to a written ledger needs the engine's correction state, which is the Spark job's work.
   - **Sample volumes only.** It reads the landing tables into Python, and the gate runs it on the `tiny` profile, within the memory budget. When the compiler exists, the Spark journaling job replaces it and writes the same tables.
3. **The rollups gain functional amounts.** DDL V009 adds nullable functional debit and credit columns to `ledger.gl_rollup_lines` with `ADD COLUMNS` (parity feature 19). The stand-in always fills them, and the rollups join the read path's queryable registry.
4. **Balances and roll-forward from the whole Databricks GL.** These read through the read path, with the entity filter always applied.
   - **Sources and queries.** It reads the engine's rollups and the posted manual journals, functional amounts only. For each source it runs one grouped query by account for periods before P (the opening balance) and one for P (the activity). That stays well inside the read path's 1,000-group limit.
   - **`GET /gl/balances?entity_id=&period=`** gives each account's opening balance, the period's functional debits and credits, and its closing balance.
   - **`GET /gl/roll-forward?entity_id=&from=&to=`** gives the same per period, and checks that each period's opening is the previous period's closing.
   - **The trial balance** (`/gl/trial-balance`) now covers both sources.
5. **The ERP's trial balance, and the tie-out.**
   - **The extract's contract.**
     - **Format.** CSV, one row per account: the account and its closing balance in the entity's functional currency, in major units with that currency's decimals.
     - **Sign.** Debit balances are positive and credit balances negative.
     - **Uploading.** An integration client uploads it for an entity and period. It is checked all or nothing, kept per version with its SHA-256, and audited.
   - **`GET /gl/erp-tie-out?entity_id=&period=`** compares each account's closing balance in the Databricks GL with the ERP's.
     - **What it lists.** Every difference, including accounts only one side has. Nothing is smoothed over.
     - **The latest extract counts,** and the response names its version and hash.
   - **The stub ERP** builds its extract from what it booked, the GL's own balances, plus entries made only in the ERP that a sample lists.
     - **Circular by construction.** Its accounts tie by construction, except the injected entries, and the ADR says so.
     - **The tests.** They also cover what an extract can get wrong: an account only the ERP has, an account only the GL has, a sign error, and an extract uploaded for the wrong period.
6. **Coverage.**
   - **Governance.** New operations join the authorization matrix, the route inventory and idempotency.
   - **Migrations.** Migration 0007 adds the snapshots, journaling runs and ERP extracts. DDL V009 runs through `ddl_apply`.
   - **Least privilege.** The reader role's two views are checked in Postgres and from inside the jobs container.
   - **Contracts.** The changed contracts are regenerated on purpose.
7. **At the sign-off,** you will be asked separately to approve the stand-in: the reference engine running as a job until the compiler exists.

## Gate 8a

Every Stage 7c criterion runs as regression, under `caffeinate`, and:
1. **The stand-in equals the engine, through Delta.**
   - The generator's `tiny` rows (seed 11) are written to a lake, and the job journals US01. Its Delta output equals the reference engine run on the same rows in memory, line for line and rollup for rollup. That compares decimals, dates, maps and nulls after the round trip through Delta.
   - The rollups sum to their lines.
2. **Reruns.** A rerun writes nothing. A rerun after more landing data arrives is refused, and so is a rerun with a changed configuration.
3. **Snapshots and periods.**
   - A run uses the configuration, entity set and period statuses frozen when it started, by their hash. A changed snapshot is refused.
   - Snapshots can't be changed.
   - The reader role sees only snapshots of runs in progress.
   - An event dated in a closed period lands in the first open one.
   - A period can't close while a journaling run is in progress.
4. **Balances.** Opening plus activity equals closing for every account and period, each period's opening equals the previous period's closing, and debits equal credits.
5. **The ERP tie-out.**
   - An extract that matches ties.
   - An ERP-only entry is reported against its account.
   - An account that only one side has, a sign error and an extract for the wrong period are each caught.
   - A bad extract creates nothing and says where it is wrong.
6. **Exactly once.** A journaling run killed after its commit is retried without writing twice, and the ledger lease keeps it apart from posting runs.
7. **Governance.**
   - The authorization matrix, route inventory and idempotency cover every new operation.
   - The reader role is checked in Postgres and from the jobs container.
   - Migration 0007 runs up, down and up.
   - The audit chain verifies.
8. **Through the stack.**
   - The generator's `tiny` data is journaled for US01.
   - The balances and roll-forward tie.
   - The stub ERP's extract is uploaded, and the tie-out reports exactly its ERP-only entry.
9. **Contracts and ADR.** The snapshots show 0 drift after the deliberate regeneration, and ADR-0019 is written.

## 8b decisions

You signed off 8a on 2026-10-02 and chose 8b next, on sample inputs. These decisions were approved automatically, as you asked for plans.

1. **A soft close.** Periods gain a `soft_closed` status: `open → soft_closed → closed → locked`. A soft-closed or closed period can be reopened with a reason, and `open → closed` stays legal for a period without a close checklist, so the 7a–7c behaviour and tests stand.
   - **What a soft-closed period still accepts:** manual entries. People can still create, edit, import, approve, reverse, batch and post journals dated in it, because the close review finds adjustments.
   - **What it refuses:** automated entries. Recurring journals skip it, and the journaling engine books events dated in it in the first open period, as it does for a closed one.
   - **One rule in one place.** One helper says which statuses take manual entries and which take automated entries, and every call site uses it. Every call site is tested in the `soft_closed` state.
   - **Like a close,** a soft close is refused while a journaling run is in progress, because its snapshot read the statuses.
2. **The close checklist is configuration** the accounting team authors and approves, like every other rule: a new `close_checklist` kind, one active at a time.
   - **Each task:** an id, a name, instructions, a due day, the tasks it depends on, and either a preparer (a manual task) or a system check (an automatic task). A manual task needs a reviewer, who can't be its preparer, and an automatic one may have a reviewer. A task can be limited to some entities.
   - **The due day** counts business days from the period end. BD+1 is the first business day after the period end, and BD−1 is the last business day in it. There is no BD0.
   - **Checked when saved:** unique ids, known dependencies, no cycles, and preparers and reviewers who are distinct. On activation, the entities must be in the entity set, and each preparer and reviewer must hold a role in each entity that lets them prepare or review close tasks.
3. **Generating a period's checklist.** The controller generates it from the active checklist for an entity and period. Its tasks are copied with their due dates and frozen, as journal routing is frozen at submit, so a later checklist version never changes a close in progress.
   - **Due dates use the entity's business calendar.** A narrow, typed lookup reads the entity's calendar code from `ref.entities` and its days from `ref.calendars`. It works like 7c's rate lookup: there is one source, it isn't a query spec, and it runs before the transaction.
   - **No silent dates.** Generation is refused when the calendar doesn't cover the due dates.
4. **Working the tasks.**
   - **Who acts.** Only the assigned preparer prepares a manual task, and only after the tasks it depends on are done. Only the assigned reviewer reviews it or sends it back, and a reviewer never reviews their own preparation. The controller can reassign a task, or reopen a done one with a reason.
   - **Automatic tasks.** A user runs the task's check. It reads Postgres, or the GL through the read path, outside the transaction, and the result and its evidence are recorded. The checks are: journals settled, suspense clear, GL balanced, ERP ties, and flux reviewed.
   - **Postgres-only reminders.** A scheduler task reminds the preparer, or the reviewer, once a day from the due date. After a configured number of days overdue it escalates to the controller.
   - **History.** Every move is audited and kept in an insert-only task history.
5. **Flux analysis**, configured as a new `flux_rules` kind, one active at a time.
   - **The comparison.** Each account is compared with the prior period: closing balances for asset, liability and equity accounts, and period activity for revenue and expense accounts.
   - **Thresholds** are per account type, with per-account overrides. Each has an amount per functional currency and a percent, joined by `and` or `or` (BlackLine's `{amount, percent, operator}` shape). A change from zero counts as over any percent.
   - **Explanations.** People with the preparer's role explain each line over its threshold. An explanation is bound to the amounts the server computed, never to amounts a client sends. An explanation whose amounts no longer match is stale.
   - **The review.** A reviewer signs off once every line over its threshold has a current explanation, and only if they wrote none of them. The review records a hash of the lines. The `flux_reviewed` check passes only while the current flux still hashes the same.
   - **On `tiny`,** every July account is new against an empty June, so every July line with a balance needs an explanation. The ADR says so.
6. **The hard close (`PeriodCloseWorkflow` without Temporal: a Postgres state machine and idempotent tasks).** When a period has a generated checklist, it closes only when every task is done.
   - **Fresh checks.** No check that read the GL may predate the ledger's last change. The check's time comes from Postgres before its read starts. At close, it is compared with Postgres times for the entity's ledger changes: a journal posted, a journaling run reconciled with lines written, an ERP extract accepted. A stale check is refused with "check it again".
   - **Unchanged rules.** The 7b and 8a refusals (journals waiting, a journaling run in progress) still apply.
7. **Who.**
   - **Actions:** `close.prepare` (journal preparers and the controller) and `close.review` (approvers at levels 1 to 3 and the controller).
   - **Generating, reassigning, reopening a task, and the soft close** need `period.manage`. Reads need `journal.read`.
   - **Assignment.** Acting on a task assigned to someone else is a 409 for a role that may act on close tasks.
8. **Coverage.**
   - **Migration 0008** adds the two kinds, `soft_closed`, the checklist, task, history, flux explanation and review tables, and task notifications. It runs up, down and up, and the downgrade refuses while any period is soft-closed.
   - **Governance.** New operations join the authorization matrix, the route inventory and idempotency.
   - **Samples.** Sample checklists and flux rules are published with the other samples, and the snapshots are regenerated on purpose.

## Gate 8b

Every 8a criterion runs as regression, under `caffeinate`, and:
1. **The soft close.** Manual entries are accepted and automated entries refused, at every call site. A soft close is refused during a journaling run, and the engine books events dated in a soft-closed period in the first open one.
2. **The checklist's configuration.** Duplicate ids, unknown dependencies, cycles, a reviewer who is the preparer, and a preparer or reviewer without the role are each refused.
3. **Due dates.** Test vectors pin BD+n and BD−n, including a month that ends on a weekend and one that ends on a holiday. Generation reads the entity's calendar through the narrow lookup, and is refused when the calendar doesn't cover the dates.
4. **Tasks.** Dependencies are enforced, only the assignee acts, a reviewer can't review their own preparation, a send-back reopens the task, reassignments and reopens are audited, and an automatic task records its check and evidence.
5. **Reminders.** Each reminder is sent once a day from the due date, to the right person, and escalates to the controller after the configured days.
6. **Flux.** Thresholds apply per type, override, currency and operator. An explanation is bound to the server's amounts and goes stale when they change. The review needs every line explained, and refuses a reviewer who explained any of them. The review's hash detects a later change.
7. **The hard close.** It is refused until every task is done, and refused when a GL check predates a ledger change. A period without a checklist still closes as before.
8. **Governance.** The authorization matrix, route inventory and idempotency cover every new operation. Migration 0008 runs up, down and up. The audit chain verifies.
9. **Through the stack.** For US01 July, the controller opens the period and generates the checklist from the sample. The preparers and reviewers work the manual tasks, and the automatic checks pass: journals settled, suspense clear, GL balanced, ERP ties and flux reviewed. The flux lines are explained and reviewed. The period is soft-closed, then closed.
10. **Contracts and ADR.** The snapshots show 0 drift after the deliberate regeneration, and ADR-0020 is written.
