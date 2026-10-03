# ADR-0016: the journal workflow in the control plane

Status: proposed with Stage 7a. Date: 2026-09-30. Implements, on sample inputs, these roadmap Stage 7 items:
- templates with typed columns;
- manual journals and CSV import;
- multi-level approvals with thresholds, delegation and reminders;
- the AS 2401.61 risk score;
- atomic batches, recurring journals and reversals;
- attachments;
- period open, close and lock.

Posting to the Databricks ledger and the ERP write-back are Stage 7b.

## Context

You chose Stage 7 next, built on sample inputs as Stage 6's first slice was ([stage-7-plan.md](../stages/stage-7-plan.md)). The real approval matrix, SoD rules, templates, recurring journals and close calendar are requested in [stage-7-inputs.md](../domain/stage-7-inputs.md).

The design follows BlackLine's journal model:
- templates with typed header and line columns;
- certification by level;
- Journal Masters and batches.

It adds what BlackLine leaves undocumented, taken from the audit standards in the research report:
- a risk score on AS 2401.61's characteristics;
- a complete, gapless journal population;
- an exception report;
- flagged bypass paths.

## Decision

**Four new configuration kinds**, maker-checker versioned like every rule:

| Kind | Resource | Scope |
|---|---|---|
| Entity set: each entity's functional currency and its minor units | `/entities` | global, one active |
| Journal template: typed header and line columns (required, read-only with a default, non-negative, allowed values), allowed accounts, line limit, an attachment rule, minimum description length, auto-reverse | `/journal-templates` | global |
| Approval matrix: level thresholds per functional currency, routing rules, risk weights and bands, reminders and escalation, bypass rules | `/approval-matrices` | global, one active |
| Recurring journal (BlackLine's Journal Master) | `/recurring-journals` | its entity, like a match set |

Nothing in use can be retired, as in ADR-0015:
- **templates** while a recurring journal uses them;
- **the matrix** while templates route through it;
- **the entity set** while an entity has periods or journals;
- **the chart** while a template lists its accounts or a recurring journal posts to them.

A recurring journal activates only when:
- its preparer holds the preparer role in its entity;
- if it auto-approves, the matrix lets recurring journals bypass approval.

**Journals are entity-scoped rows in Postgres,** in the entity's functional currency in 7a.
- **Why functional currency.** Rates live in Delta, and the read path only serves entity-scoped tables, a deliberate security invariant. Multi-currency manual journals come with 7b.
- **Nothing is deleted.** The app role can't delete from any journal table. Lines, approvals, the history and imports are insert-only. Editing a draft adds a new revision of its lines, and a journal ends as `cancelled`.
- **Numbering is gapless** per entity. A counter row moves in the journal's own transaction, so a refused journal takes no number.

**The states** are `draft`, `pending_approval` (at a level), `approved` and `cancelled`, moved by submit, approve, reject, recall and cancel. Reverse creates a new draft with the lines flipped.
- **Illegal moves** are 409s.
- **Every move needs** `If-Match` and `Idempotency-Key`, and is audited.
- **Authorization comes first.** Every operation checks permission before an object's state or ETag. A caller without the permission always gets a 403, never a 409 or 428.

**Routing is frozen at submit.** The journal stores the matrix version, the routing inputs, the risk score with each factor's inputs and weight, and the levels it needs.
- **How many levels.** The most that its total debits, its risk band, or a routing rule asks for.
- **Approvals are bound** to the journal's content hash.
- **After a recall or rejection,** the next submission starts over. Earlier approvals stay in the history but no longer count.

**The risk score follows AS 2401.61.** Each factor records its source and inputs:
- (a) an account with no approved line in 90 days;
- (b) a preparer with few recent journals;
- (c) a period-end or post-close date with a thin description;
- (d) suspense or a sensitive account;
- (e) round or repeating amounts.

In 7a, factors (a) and (b) read Postgres journal history. GL history joins them in 7b.

**Approval authority is hierarchical.** An L2 approver may approve levels 1 and 2, and an L3 approver any level.

**Separation of duties:**
- the preparer can't approve or reject;
- one person approves at most one level of a submission, or of a batch;
- a delegate counts as both themselves and the person they stand in for;
- nobody delegates to themselves.

Every refusal is audited.

**Delegation.** An approver may delegate their authority in named entities, for a date window, to a colleague who already sees those entities. It is revocable, and never deleted.

**Periods are authoritative in Postgres** (D6), per entity: `open → closed → locked`, and a closed period can be reopened with a reason.
- **Who.** Only the controller moves them.
- **Closing** is refused while journals wait for approval.
- **Locks.** Journal moves lock the journal row `FOR UPDATE` and its period `FOR SHARE`. Closing locks the period `FOR UPDATE`.

  A two-thread test proves the locks. Without them, a submit that races a close leaves a journal waiting in a closed period.

**Batches** are submitted, approved and rejected as one.
- **Routing.** A batch needs the most levels any member needs.
- **SoD** covers every member's preparer.
- **Order.** Members are locked in id order.

**Recurring journals and reminders** are scheduler tasks (ADR-0007) that take an as-of time, so tests move time without sleeping. Both are idempotent:
- recurring journals are unique per journal and period;
- notifications carry dedup keys.

A skipped recurring journal (period not open, preparer without the role) notifies its preparer once. Escalation starts after the matrix's number of reminders, and goes to the next level's approvers and the controller. Email is out of scope.

**Reversals.** A template can reverse automatically. The reversal is created on the next period's first day, at approval if that period is open, otherwise when it opens.
- **It runs in a savepoint,** so a failure never undoes the approval. The failure is recorded in the history instead.
- **It may bypass approval** if the matrix allows, and is then flagged.

**Attachments** are stored in Postgres, because the app holds no S3 keys (a Stage 1 rule).
- **Uploads** are base64 JSON, so no multipart package is needed. They are capped at 10 MiB, allow-listed and hashed, and accepted on drafts only.
- **Downloads** always carry `Content-Disposition: attachment` and `nosniff`.

**CSV import is all or nothing.** Rows group into journals by key.
- **Issues** carry CSV row numbers.
- **Every file is recorded,** accepted or rejected, with its SHA-256.

**Evidence for auditors:**
- **the population export,** with preparer, approvers and delegates, timestamps, source, risk factors, a post-close flag and the bypass reason;
- **a completeness summary:** a count, gapless numbers, and the counter agreeing;
- **the exception report:** rejected, recalled, cancelled, bypassed, failed automatic reversals, rejected imports, and skipped recurring journals.

## Consequences

- **Accountants set the rules as data:** the approval matrix, risk weights, templates and recurring journals. The Stage 9 screens build on these APIs.
- **Stage 7b builds on this:**
  - posting to Delta (`posted`), and the ERP write-back (`erp_confirmed`);
  - the full population, including the engine's automated entries;
  - GL-history risk factors;
  - multi-currency manual journals.
- **Two save-time checks wait for the rule builder** (ADR-0015): required dimensions and mapping rows keyed on policy switches.
- **The configuration-change lock is still to come.** It comes with Stage 5's hardening, as you chose. Journal and period moves already use row locks.
- **XLSX import needs `openpyxl`,** a new package that needs your approval. CSV covers the same data.
- **A new dev user,** erin (1005), approves level 2 in US01. alice now prepares journals in US01, and dave approves level 1 and manages US01's periods. Each existing user sees exactly the entities they saw before.
