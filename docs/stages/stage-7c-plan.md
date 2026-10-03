# Stage 7c plan: multi-currency manual journals (on sample inputs)

On 2026-10-01 you signed off Stage 7b and chose 7c next, on sample inputs. This plan was approved automatically, as you asked for plans. It finishes Stage 7, builds on [ADR-0016](../adr/0016-journal-workflow.md) and [ADR-0017](../adr/0017-posting-and-the-erp-write-back.md), and its decisions go in ADR-0018. D19 keeps its default (an ERP stays the book of record).

## What 7c covers

- **In 7c:**
  - manual journals in a currency other than their entity's functional currency;
  - a narrow, typed rate lookup that reads FX rates from Delta without loosening the read path's entity filter;
  - conversion at submit under D9, frozen and bound into the approval, with one rounding line per journal;
  - routing, bypass caps, reversals, batches, recurring journals, import, evidence and posting, all in functional terms where it matters.
- **Still blocked on the Spark rule compiler:** the engine's automated entries in the journal population, and GL-history risk factors.
- **Not in 7c:** revaluation of foreign-currency balances at period end (Stage 8's close), and translation to a reporting currency (consolidation).

## Decisions

1. **A journal may be in a foreign currency.**
   - **Which currencies.** A template lists the transaction currencies it allows besides the functional currency (`currencies`, empty by default). So every existing template keeps its 7a behaviour.
   - **Amounts** are minor units of the journal's own currency, using ISO 4217 exponents (0 decimals for JPY, 3 for KWD).
   - **Accounts** the chart restricts to one currency take only journals in that currency.
   - **Unchanged hashes.** A draft in the functional currency is stored without a currency, and the field is left out of the content hash when unset. Journals in approval at the upgrade keep the content their approvals are bound to.
2. **Rates are read narrowly.**
   - **One typed call** in the read path: `rate(from, to, on, rate_type="daily_spot")`. It returns the latest version of that day's rate from `ref.fx_rates`, with its source and the Delta table version it was read at.
   - **Shape.** The query shape is fixed and the inputs are typed: ISO currency codes, a date, and a rate type from a closed list. Values travel as literals, never SQL.
   - **Not a query spec, and not in the queryable registry,** so the read path's entity filter stays exactly as Stage 1b signed it off. Rates are global reference data, not entity data.
   - **Spark Connect,** through the same session pool, concurrency limit and timeouts as every read.
   - **Never inside a transaction.** A submit looks up its rate first, then checks inside the transaction that the journal's date and currency haven't changed. This is the lesson from 7b's XLSX import.
   - **`GET /fx-rates/{from}/{to}/{date}`** shows a preparer the rate before they submit. Anyone who reads journals in some entity may call it.
3. **Conversion at submit follows D9.**
   - **Each line** is converted exactly at the accounting date's daily spot rate, its latest version, and rounded half-even. It uses `close_contracts.money.convert_minor`, the function the journaling engine uses.
   - **The residual** goes on one rounding line to the active chart's FX rounding account.
   - **A missing rate** refuses the submit, naming the currency pair and the date, and the journal stays a draft. A journal dated after the last published rate waits for it.
   - **Frozen:** each submission of a foreign-currency journal gets one insert-only conversion row. It holds the rate, with its date, type, version, source and the Delta table version; each line's functional amount; the rounding line; and a hash of it all.
   - **A rate published later never changes a submitted journal.** A recall or rejection returns the journal to draft, and the next submission converts again.
   - **Bound into the approval.** Approvals record the conversion hash next to the content hash, and approving checks both.
4. **Routing and bypass caps use the functional total,** because the matrix thresholds are per functional currency. The risk score's round-amount factor reads amounts as entered.
5. **A reversal reuses the original's rate,** so it undoes the original exactly in functional terms, with no FX residue. Automatic reversals at period open do the same. This is the reversal rule under D9.
6. **Batches.** Every member is converted when the batch is submitted. The rates are looked up before the transaction, and one missing rate refuses the whole batch.
7. **Recurring journals** may carry a currency their template allows. The task looks up the rate before its transaction. A missing rate skips that period's journal and notifies its preparer, like the other skips.
8. **Posting.**
   - **Ledger lines** carry both amounts, and the rate with its date, type and version. DDL V008 adds nullable columns for these to `ledger.journal_lines`, plus `line_kind`; 7b's rows keep nulls.
   - **The rounding line** posts as its own ledger line (`line_kind = 'fx_rounding'`).
   - **Trial balance and tie-out** work in the functional currency. Live journals' functional totals are backfilled by migration 0006.
   - **The ERP feed's** canonical JSON carries the conversion.
9. **Import.** CSV and XLSX gain an optional `currency` column, one per journal. Amounts are parsed with that currency's decimals.
10. **Evidence.** The population export shows each journal's currency, rate, rate date and version, functional totals and rounding.
11. **Samples.** A multi-currency template allows EUR and GBP besides the functional currency. App tests use reference rates. On the stack, the rates come from the generator (`tiny` profile) written into the local lake.
12. **Coverage.**
    - The rate route joins the authorization matrix and the route inventory, and submit keeps its idempotency flow.
    - Migration 0006 adds the conversion table, the functional totals and the approvals' conversion hash. It also updates `posting_queue`, and runs up, down and up.
    - DDL V008 runs through `ddl_apply`.
    - The changed contract snapshots are regenerated on purpose.

## Gate 7c

Every Stage 7b criterion runs as regression, under `caffeinate`, and:
1. **D9 conversion.**
   - **Properties.** Over 10,000 generated journals in currencies with 0, 2 and 3 decimals, the functional debits equal the credits after rounding, and the rounding stays within half a minor unit per line. Every line equals `convert_minor`.
   - **Examples.** Fixed examples pin the rounding line's account, side and amount.
2. **Frozen and bound.**
   - A later rate version leaves a submitted journal unchanged, and a recall and resubmit converts again.
   - A missing rate refuses the submit.
   - A conversion row changed in the database is caught when someone tries to approve.
3. **Functional routing.** A EUR journal in US01 routes and is capped by its USD total.
4. **Reversals net to zero** in the functional currency, the automatic ones included.
5. **Narrow rates.**
   - Only `ref.fx_rates` is read, and inputs are typed. Rates are not queryable through query specs, and Stage 1b's entity-filter property still holds.
   - The lookup happens outside any transaction: while it waits, another transaction can lock the journal.
6. **Posting.**
   - Ledger lines carry both amounts and the rate, and the rounding line posts.
   - The trial balance balances in the functional currency, and the tie-out holds.
   - V008 applies, and a rerun applies nothing.
7. **Import.** A `currency` column parses amounts with that currency's decimals and refuses extra ones.
8. **Compatibility.** 7a and 7b journals keep their content hashes (a golden hash), journals in approval at the upgrade still approve, and the backfill covers every live journal.
9. **Governance.** The authorization matrix, route inventory and idempotency cover every operation. Migration 0006 runs up, down and up, and the audit chain verifies.
10. **Through the stack.**
    - The generator's rates are in the local lake.
    - A EUR journal in US01 is prepared, approved and posted.
    - Its ledger lines show USD amounts with the rate, the trial balance balances, the tie-out holds, and the stub ERP confirms it.
11. **Contracts and ADR.** The snapshots show 0 drift after the deliberate regeneration, and ADR-0018 is written.
