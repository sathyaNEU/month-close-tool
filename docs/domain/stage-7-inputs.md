# What Stage 7 needs from you

Stage 7 builds the journal workflow: manual journals, approvals, batches, recurring journals, reversals and periods. It is being built now on sample inputs, as you asked on 2026-09-30. Nothing sample-based goes live in production, so before production it needs these from your finance team.

## 1. The approval matrix

For each entity (or all of them), the amount at which each approval level starts. Amounts are in the entity's functional currency, and apply to the journal's total debits. For example: level 1 below 10,000, level 2 from 10,000, level 3 from 100,000. Also say:
- whether any journal types may skip approval (recurring journals, reversals, small reclasses), and above what amount they may not;
- who approves at each level, by name or role;
- how long a journal may wait before its approvers are reminded, and when it escalates.

## 2. Your separation-of-duties rules for journals

The platform already enforces these:
- a preparer can't approve their own journal;
- one person approves one level of a journal;
- a delegate is checked like the person they stand in for.

Tell me about any other rules, such as who may prepare journals to cash or revenue accounts, or amounts above which the controller must approve.

## 3. Your journal templates

An export of your BlackLine journal templates, or a list per template giving:
- the header and line fields;
- which fields are required, read-only or defaulted;
- which accounts each template may use;
- whether it must carry an attachment, and above what amount;
- whether it reverses automatically in the next period.

## 4. Your recurring journals

For each BlackLine Journal Master, give its template, entity, frequency and day, preparer, lines, and whether it is certified automatically today.

## 5. The close calendar

For each entity and period: the day the period closes, and the day it locks. Also say who may reopen a closed period.

## What happens next

The samples are replaced by your answers, as configurations your team authors and approves through the API, and the gate's tests run against them.
