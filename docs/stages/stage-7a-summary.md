## What this part delivers

You chose Stage 7 next, built on sample inputs as Stage 6's first slice was. This is its first part, 7a: the journal workflow in the control plane. The plan is [stage-7-plan.md](stage-7-plan.md), the decisions are in [ADR-0016](../adr/0016-journal-workflow.md), and the real inputs are requested in [stage-7-inputs.md](../domain/stage-7-inputs.md). Posting to the Databricks ledger and the ERP write-back are 7b.

- **Accountants set the rules as data,** through the same maker-checker API:

  | Resource | What it holds |
  |---|---|
  | `/entities` | Each entity's functional currency |
  | `/journal-templates` | BlackLine-style templates: typed header and line columns, allowed accounts, line limits, an attachment rule, automatic reversal |
  | `/approval-matrices` | Level thresholds per currency, routing rules, the risk-score weights, reminders and escalation, and what may skip approval |
  | `/recurring-journals` | BlackLine's Journal Masters, owned by their entity |

- **Journals** (`/journals`) go from draft to pending approval, level by level, to approved, or back to draft on a rejection or recall. A draft can be cancelled, and an approved journal reversed. Every move is idempotent, needs the current ETag, and is audited.
- **Approvals.**
  - **Levels.** The levels a journal needs come from its amount, its AS 2401.61 risk score and routing rules. They are frozen at submit and bound to the journal's content.
  - **SoD.** Separation of duties covers preparers, approvers and delegates. Delegation hands an approver's authority to a colleague for a date window.
- **Periods** (`/periods`) are opened, closed, locked and reopened by the controller. A journal can't be submitted or approved into a closed or locked period, and a period can't close while journals in it wait.
- **Batches** (`/journal-batches`) are approved all or none. **Recurring journals** are created each period, and **automatic reversals** land in the next period. **Reminders** and escalation reach approvers through `/notifications`.
- **CSV import** (`/journal-imports`) is all or nothing, with row-numbered problems. **Attachments** are stored safely, and a template can require one.
- **Evidence for auditors:** the complete, gapless journal population, a completeness summary, and an exception report of everything rejected, recalled, cancelled, bypassed or skipped.

## Results

| Check | Result |
|---|---|
| App tests (Postgres) | 156 passed, plus the audit-chain residual as a strict xfail |
| Unit tests | 459 passed, contracts coverage 96% |
| Authorization matrix | 185 operations × 12 roles × 3 scopes = 6,660 checks, 0 unexpected allows or denies |
| Idempotency | every POST (53 of them new) replays byte-identically, with the exact audit events expected |
| Period race | with the row locks, a submit racing a close waits and is refused; without them the same race leaves a journal waiting in a closed period (the test's positive control) |
| Stack | alice prepares a journal, dave approves level 1, erin level 2; the audit chain verifies |

## Found and fixed while building

1. **Authorization has to come before state.** At first, approving checked the journal's state and ETag before the approver's authority. So someone with no approval role could get a 409 or 428 instead of a 403. Authority is now checked first, as the Stage 4 rule requires, and the authorization matrix holds it.
2. **A draft showed approvals that no longer counted.** After a recall, the journal still listed the withdrawn submission's approvals. A journal now shows approvals only while a submission is live, and the history keeps every approval.
3. **The sample recurring journals named their preparers by name.** Role assignments use the user id the proxy sends (1001 for alice), so the preparer check could never pass. The samples now use ids.
4. **The end-to-end scheduler test counted every task.** It required a minimum number of ticks from each task it saw. The new journal tasks run every 60 seconds and would have failed that, so the minimum now applies to the 1-second tasks it was written for.
5. **Tests that could not fail.** One batch assertion was always true, and it now checks the "one level per person" rule. The period-lock test has a positive control that removes the locks and shows the race breaking the rule.
6. **Earlier migrations.** Migration 0003's test compared the kind list with 0003's own list, so 0004 would have broken it. It now reads the newest migration that sets the list.
7. **A review before the gate found three more gaps:**
   - **Batch moves** were only spot-checked. They now have a full state-by-move table, like journals, and the period table covers locking a locked period.
   - **The reminder task's batch branch had never run.** A fault there would have stopped every reminder. A test now reminds a waiting batch's approvers and skips its preparers, even one who also approves.
   - **The auditors' export could carry spreadsheet formulas.** A description typed as `=HYPERLINK(...)` would run when an auditor opened the file in Excel. Such text now gets a leading quote and shows as text.
8. **The first gate run passed 83 of 84 criteria.** The one failure was trivy, from two HIGH OpenSSL vulnerabilities (CVE-2026-75804 and CVE-2026-84782) published after the last scan. They affect the Debian 13 base image of both our images. The pinned base image hasn't been rebuilt with the fix yet, so both Dockerfiles now install Debian's patched OpenSSL, pinned to exactly 3.5.7-1~deb13u3. This report is the second run.
   - **The pin is temporary, and it will break.** Debian's archive keeps only the newest version of a package. When Debian ships its next OpenSSL update, installing exactly this version will fail, and with it every cold image build (`make images`, `make up`, and the gate's bring-up).
   - **The fix** is 7b's first task: upgrade without an exact version, which the SBOM records anyway, or install through snapshot.debian.org.

## Where the build differs from the plan

1. **An entity set was added.** Journals need each entity's functional currency. That lived only in Delta's reference data, so it is now a configuration kind (`/entities`).
2. **Automatic reversals** are created when the next period opens, if it wasn't open at approval (the roadmap's "auto-reversals on period open").
3. **Notifications are in-app only.** Email is out of scope.
4. **A fifth dev user,** erin (1005), approves level 2 in US01, because one person can't approve two levels. alice now also prepares US01 journals, and dave approves level 1 and manages US01's periods. Everyone sees the same entities as before.
5. **Periods aren't seeded from the generator's calendar.** Plan decision 12 said they would be. A period exists once the controller opens it through the API; the stack test opens September. Seeding the close calendar comes with your real calendar.

## Needs your decision

1. **Accept ADR-0016,** including its choices:
   - functional-currency journals in 7a;
   - attachments in Postgres;
   - hierarchical approval authority (an L2 approver may approve level 1);
   - the risk-score factors and their sample weights.
2. **Accept the contract snapshot changes.** There are 5 new snapshots, and 4 new `ConfigKind` values in the envelope snapshots.
3. **`openpyxl`,** for XLSX import (a new package). CSV works without it.
4. **The real inputs** before production: your approval matrix and thresholds, your journal SoD rules, templates, recurring journals and the close calendar ([stage-7-inputs.md](../domain/stage-7-inputs.md)).
5. **Next:** 7b (posting to the Databricks ledger and the ERP write-back).

## Carried forward

- **First in 7b: the OpenSSL pin** (item 8 above), before a newer Debian update breaks image builds.
- **7b:**
  - posting and `erp_confirmed`;
  - the full journal population, with the engine's entries;
  - GL-history risk factors;
  - multi-currency manual journals.
- **Stage 5:** the configuration-change lock.
- **The rule builder:** save-time checks for required dimensions and for mapping rows keyed on policy switches.
- **Stage 8:** close management, which will use these periods.
