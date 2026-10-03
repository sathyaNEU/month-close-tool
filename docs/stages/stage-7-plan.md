# Stage 7 plan: the journal workflow (on sample inputs)

On 2026-09-30 you chose Stage 7 next, built on sample inputs as Stage 6's first slice was. This plan was approved automatically, as you asked for plans. The real inputs are requested in [stage-7-inputs.md](../domain/stage-7-inputs.md), and nothing sample-based goes live in production.

Stage 7 is split in two, as Stages 1 and 2 were. Each part has its own gate, report and sign-off:
- **7a, journals in the control plane.** Templates, manual journals and import, validation, approvals with thresholds and a risk score, SoD and delegation, batches, recurring journals, reversals, periods, attachments, and the evidence auditors ask for. Everything lives in Postgres and the API. No Spark job is needed, so none of it waits for the rule compiler.
- **7b, posting and the general ledger.** Posting approved journals to the Delta ledger, the stub ERP write-back, trial balance and drill-down. It is outlined at the end and planned in full after 7a's sign-off.

## BlackLine terms and ours

| BlackLine | Here |
|---|---|
| Journal Template (header and line columns with `required`, `readOnly`, `defaultValue`, `restrictNegativeValues`, `balanceBy`, `maxNumLines`) | `JournalTemplate`, a maker-checker configuration (`/journal-templates`) |
| Manual journal, Journals grid | `/journals`, entity-scoped |
| Import from Excel | CSV import in 7a (`text/csv` body). XLSX needs the `openpyxl` package, which needs your approval |
| Journal Masters (recurring, `frequencyCode`, `canAutoCertify`) | `RecurringJournal`, a maker-checker configuration (`/recurring-journals`) |
| Automated Journal Definition | `AutoJournalDefinition`, built in Stage 6 |
| Journal Batching (certify and post as one) | `/journal-batches`: submitted, approved and (in 7b) posted all or none |
| Certification (prepare, approve, reject, decertify) | submit, approve by level, reject, recall |
| Supporting documents | Attachments on a journal |
| `scheduleupdatepostingstatus` (the ERP document number written back) | `POST /posting/status` and the `erp_confirmed` state, in 7b |
| Reversals (barely documented by BlackLine) | `:reverse`, and a template's auto-reverse into the next period |

## Decisions for 7a

1. **Three new configuration kinds** go through Stage 4's maker-checker as global configurations. Migration `0004` widens the kind check, and the new models are frozen as snapshots.
   - **Journal templates.** Each has typed header and line columns. A column can be required, read-only or restricted to non-negative values, and can carry a default. A template also limits which accounts may be used (account types, ranges, or a list), sets a maximum number of lines, and can require an attachment above an amount. It can also mark itself auto-reversing.
   - **The approval matrix.** It holds the amount thresholds per level, optional rules by entity and template, the risk-score weights and bands, reminder and escalation intervals, and what may bypass approval. It is a singleton, like the chart.
   - **Recurring journals.** Each names a template, a frequency (monthly or quarterly), the day, the entity, the preparer and the lines.
   - **Retirement.** Nothing in use can be retired, as for Stage 6. A template can't be retired while an active recurring journal uses it, and the approval matrix is replaced by a new version rather than retired.
2. **Journals are entity-scoped,** unlike the Stage 6 configurations.
   - **Visibility.** A journal is visible to anyone holding a role in its entity. An invisible journal is a 404, and a visible one without the permission is a 403.
   - **New actions:** `journal.read`, `journal.prepare`, `journal.approve.l1`, `journal.approve.l2`, `journal.approve.l3`, `period.manage` and `journal.export`.
   - **Approval authority is hierarchical.** An L2 approver may approve level 1 or 2, and an L3 approver any level. The authorization matrix's hand-written expectations and the route inventory cover every new operation.
3. **Manual journals are in the entity's functional currency in 7a.** Rates live in Delta (`ref.fx_rates`), and the read path only serves entity-scoped tables, which is a deliberate security invariant. Multi-currency manual journals come in 7b, with a reviewed way to read reference data.
4. **Nothing is deleted, and numbering is gapless.**
   - **No deletes.** Journals end as `cancelled`, never deleted. The app role has no `DELETE` on any journal table.
   - **Numbering.** Each entity numbers its journals gaplessly from a counter row updated in the journal's own transaction. Sequences are not used, because they leave gaps.
5. **The states:**

   | From | Action | To | Who |
   |---|---|---|---|
   | (none) | create | `draft` | preparer |
   | `draft` | edit (lines and header) | `draft` | preparer |
   | `draft` | submit | `pending_approval` (level 1) | preparer |
   | `pending_approval` level k | approve | level k+1, or `approved` after the last level | an approver with authority for level k |
   | `pending_approval` | reject (with a reason) | `draft` | an approver |
   | `pending_approval` | recall | `draft` | the preparer |
   | `draft` | cancel | `cancelled` | the preparer or the controller |
   | `approved` | reverse | a new `draft` reversal | preparer |

   - **Illegal moves** get a 409.
   - **Every move** needs `If-Match` and an `Idempotency-Key`, and is audited in its own transaction, as in Stage 4.
   - **7b** adds `posting`, `posted`, `posting_failed` and `erp_confirmed`.
6. **Validation on save and on submit:**
   - against the active chart: the account exists, is postable, and is allowed for the entity and currency;
   - against the dimension set: required dimensions and closed value lists;
   - against the template: its columns, allowed accounts, line limit and attachment rule;
   - debits equal credits;
   - the journal's date falls in an open period.

   Problems come back as issues with stable codes and paths.
7. **Routing is frozen at submit.** The journal stores:
   - the matrix version;
   - the risk score, its factors, their inputs and the score version;
   - the levels it needs.

   Each approval is bound to a content hash of the journal. Only a draft can be edited, and a recall or rejection clears every approval. A new matrix version activated mid-approval leaves journals already in approval unchanged.
8. **The risk score follows AS 2401.61.** Each factor names its source and stores its inputs with the score:
   - (a) an account with no approved journal line in the last 90 days (in 7a, from Postgres journal history; 7b adds GL history from Delta);
   - (b) a preparer with few journals in the last 90 days;
   - (c) a journal dated in a period's last days or after its close, with a short description;
   - (d) a posting to suspense or to an account the matrix marks as sensitive;
   - (e) round amounts or a repeated ending.

   **How it routes.** The weights and bands are control parameters in the approval matrix, under maker-checker. The levels a journal needs are the larger of those its total debits need under the amount thresholds and those its risk band adds.
9. **Separation of duties:**
   - the preparer can't approve any level of their own journal;
   - one person approves at most one level of a journal;
   - a delegate acting for an approver is checked as well as the approver;
   - nobody can delegate to themselves;
   - every refusal is audited, as in Stage 4.
10. **Delegation.** An approver can delegate their approval authority in named entities to a colleague for a date window.
    - **Who can receive it.** The colleague must already hold a role in each of those entities, so they can see the journals.
    - **The record.** Approvals record both people. Delegations can be revoked and are audited.
11. **Concurrency uses row locks, not the configuration lock you deferred.**
    - **Journal moves** lock the journal row `FOR UPDATE`, and its period row `FOR SHARE`.
    - **Closing or locking a period** locks the period row `FOR UPDATE`, and is refused while any journal dated in it is waiting for approval.
    - **A batch** locks its members in id order.

    A two-thread test (closing a period while a journal in it is being submitted, the interleaving that could leave a journal waiting in a closed period) proves the locks: it fails when they are removed.
12. **Periods are authoritative in Postgres (D6).**
    - **The moves.** Each entity's periods move `open → closed → locked`, and a closed period can be reopened with a reason. Only the controller role does these.
    - **What they block.** A journal dated in a closed or locked period can't be submitted or approved, and a locked period can't be reopened.
    - **The sample periods** come from the generator's calendar.
13. **Batches.** Journals of one entity can form a batch.
    - **Submitting and routing.** The batch is submitted as one, and routed at the highest level any member needs.
    - **SoD** covers every member's preparer.
    - **All or none.** Approving or rejecting applies to every member, so a batch is never partly approved.
14. **Recurring journals.** A scheduler task creates each due journal as a draft or, where the matrix allows it, auto-approved. It runs once per recurring journal and period, whatever the timing.
    - **Bypasses are flagged.** An auto-approved journal is marked as a bypass and appears in the exception report. This follows the research: every path that skips approval must be visible.
15. **Reversals.** `:reverse` on an approved journal creates a draft with the lines flipped, linked to the original. When an auto-reversing template's journal is approved, its reversal is created, dated the first day of the next period. It is routed normally unless the matrix lets reversals bypass approval, in which case it is flagged.
16. **Reminders and escalation.** A scheduler task writes notifications for journals that have waited longer than the matrix's reminder interval at their level. After the escalation limit, it also notifies the next level's approvers and the controller. Notifications are read through `/notifications`, and email is out of scope.
17. **Time-driven tasks take an as-of time.** Recurring creation, reminders, escalation and auto-reversal take it, so tests move time without sleeping. In production it is Postgres `now()`, the only clock, as since Stage 1a.
18. **CSV import.** The body is `text/csv`, so no new package is needed.
    - **Grouping.** Rows group into journals by a journal-key column.
    - **All or nothing.** The whole file is validated before anything is created. The problems come back as issues with row numbers.
    - **The record.** Each import records the file's SHA-256, its row count and the journals it created.
19. **Attachments** are stored in Postgres, because the app holds no S3 keys (a Stage 1 rule).
    - **Uploads** are base64 inside JSON, so no new package is needed.
    - **Limits.** There is a size cap and a content-type allow-list, and each file's SHA-256 is stored.
    - **Downloads** always send `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`, so an uploaded HTML or SVG file can't run in the app's origin.
20. **Evidence for auditors:**
    - **The population export.** It lists every journal with its number, preparer, approvers and delegates, timestamps, source (manual, import, recurring, reversal), risk factors, and a post-close flag. It ties to the journal table by count and gapless numbers. In 7a it covers the control plane's journals, and says so. The engine's automated entries join it in 7b.
    - **The exception report.** It lists journals that were rejected, recalled or cancelled, bypassed approval, or failed validation on import.
21. **Samples** are labelled `sample_`: templates (a standard journal, an auto-reversing accrual, a reclass), an approval matrix, two recurring journals, and a new dev user with journal roles. The existing dev users keep exactly what they see today.

## 7b, outlined (planned in full after 7a's sign-off)

- **D19 still defaults to an ERP as the book of record.** `posted` means committed to the Databricks ledger tables, and `erp_confirmed` is optional.
- **Posting.** A posting job writes approved journals to Delta `ledger.journals` and `ledger.journal_lines`, through the outbox, lease and fencing. It is idempotent by journal and version, and moves the journal to `posted`, recording the Delta commit.
- **The rollup gap.** Functional amounts are added to `ledger.gl_rollup_lines`, closing the gap ADR-0015 recorded.
- **The ERP round trip.** A stub ERP feed reads the GL outbox and writes document numbers back through `POST /posting/status`, which moves the journal to `erp_confirmed`.
- **Reading the ledger.** Trial balance and drill-down go through the read path.
- **Filling 7a's gaps:**
  - the full journal population, with the engine's entries;
  - GL-history risk factors;
  - multi-currency manual journals.

## Gate 7a

Every Stage 6 first-slice step runs as regression, under `caffeinate`, and:
1. **States.** Every legal and illegal move for journals, batches and periods is tested from a transition table, and each illegal move gets a 409.
2. **Separation of duties.** The preparer can't approve, one person approves one level, delegates are checked like the people they act for, and every refusal is audited.
3. **Routing.**
   - A table of amounts, risk factors and matrix rules gives the expected levels.
   - Routing is frozen at submit, so a new matrix version doesn't change a journal already in approval.
   - An approval is bound to the journal's content.
4. **Periods.**
   - No submission or approval into a closed or locked period.
   - Closing is refused while journals wait for approval.
   - The two-thread race test (a close against a submit) passes with the locks and fails without them.
5. **Batches, recurring journals, reversals and reminders.**
   - Batches are all or none.
   - Recurring journals are created once per period at any as-of time.
   - Auto-reversals land in the next period.
   - Reminders and escalation fire at the as-of times the matrix sets.
6. **Validation.**
   - Chart, dimensions, template and balance checks run.
   - CSV import is all or nothing, with row-numbered issues.
   - Attachments keep their size cap, allow-list and download headers.
7. **Completeness.**
   - Numbering is gapless per entity.
   - The app role can't delete anything (a privilege test).
   - The population export ties to the journal table.
   - The exception report lists every rejected, recalled, cancelled and bypassed journal.
8. **Governance.** The authorization matrix (with entity-scoped journal rows), the route inventory and idempotency cover every new operation, and the audit chain verifies.
9. **Through the stack.** Dev users prepare a journal, and two approvers approve it at two levels. The audit chain verifies.
10. **Migration and ADR.** Migration `0004` runs up, down and up; the snapshots show 0 drift; and ADR-0016 is written.

## Needs your decision (not blocking 7a)

- **`openpyxl`,** for XLSX import (a new package). CSV works without it.
- **The real inputs** listed in [stage-7-inputs.md](../domain/stage-7-inputs.md), before production:
  - your approval matrix and thresholds;
  - your journal SoD rules;
  - your current journal templates;
  - your recurring journals;
  - the close calendar.
