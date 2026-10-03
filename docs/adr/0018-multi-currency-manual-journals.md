# ADR-0018: multi-currency manual journals

Status: proposed with Stage 7c. Date: 2026-10-01. Implements, on sample inputs, the last roadmap Stage 7 item:
- manual journals in a currency other than their entity's functional currency;
- a narrow, typed rate lookup;
- conversion at submit under D9, frozen and bound into the approval.

Still blocked on the Spark rule compiler: the engine's automated entries in the journal population, and GL-history risk factors.

## Context

You signed off 7b and chose 7c on sample inputs ([stage-7c-plan.md](../stages/stage-7c-plan.md)). Until now every manual journal was in its entity's functional currency (ADR-0016), because the rates live in Delta (`ref.fx_rates`), and the read path only serves entity-scoped tables through query specs, with the caller's entities always applied. Stage 1b signed off that invariant, and 7b's plan said it would not be loosened.

D9 fixes the arithmetic already used by the journaling engine (ADR-0015):
- each line is converted exactly at the accounting date's daily spot rate and rounded half-even;
- one rounding line balances the event;
- each line records the rate's type, date and version.

## Decision

**A journal may be in a foreign currency.**
- **Which currencies.** A template lists the transaction currencies it allows besides the functional currency (`currencies`, empty by default), so every existing template keeps its 7a behaviour. The sample template `sample_fx_journal` allows EUR, GBP and USD.
- **Amounts** are minor units of the journal's own currency, with ISO 4217 exponents (none for JPY, three for KWD). An account the chart restricts to one currency takes only journals in that currency.
- **Unchanged hashes.** A draft in the functional currency is stored without a currency, and the field is left out of the content hash when unset. Drafts are rebuilt from the database the same way, so journals saved before 7c, and journals in approval across the upgrade, keep the content their approvals are bound to.

**Rates are read narrowly.**
- **One typed call.** `rate(keys)` takes `RateKey` values only (two ISO currencies the platform knows, a date, and `daily_spot` or `closing`), at most 500 at once. It returns the latest version of each key's row in `ref.fx_rates`, with its source and the Delta table version it was read at.
- **Not a query spec, and not in the queryable registry.** The query engine's entity filter stays exactly as Stage 1b signed it off, and no spec can reach the table. Rates are global reference data, not entity data.
- **A fixed query.** Literal conditions on the key columns, at one pinned table version, through the read path's Spark Connect session pool (concurrency, tags and timeouts). It lives in `close_platform/query/rates.py`, so the Spark-ban lint covers it.
- **Outside every transaction.** A submit first runs every check it will repeat under the lock, in the same order: visibility, authority, ETag and state. Then it looks the rate up, and only then opens its transaction. Inside, it checks that the journal's currency and date still match the rate it was given. An idempotent replay returns the stored response before any of this, so it neither repeats the lookup nor fails a check that the first request has since changed. A refused check looks once more, so a duplicate that lost the race to the first request's commit still replays. A test locks the journal and its period while a lookup waits.
- **`GET /fx-rates/{from}/{to}/{date}`** shows the rate to anyone who reads journals in some entity.
- **The replay lookup** also serves the XLSX import, which no longer parses a workbook again on a replay (this amends ADR-0017).

**Conversion at submit follows D9.**
- **The arithmetic** is the journaling engine's: `close_contracts.money.convert_minor` on each line, then one rounding line to the active chart's FX rounding account for the residual. A property test over 10,000 generated journals holds every line to `convert_minor`, and keeps the rounding within half a minor unit per line.
- **A missing rate** refuses the submit, naming the pair and the date (422, `no_rate`). The journal stays a draft.
- **Frozen per submission.** Each submission of a foreign-currency journal adds one insert-only `journal_conversions` row. It holds the rate (date, type, version, source and Delta table version), the canonical conversion (each line's functional amount, and the rounding line) and its hash.
- **A later rate never changes it.** A recall or rejection returns the journal to draft and clears its functional totals. The next submission converts again, and the earlier conversion row is kept.
- **Bound into the approval.** The journal and every approval record the conversion hash. Approving recomputes the hash from the stored conversion and refuses a mismatch, the same second guard the content hash has.
- **Functional totals** (debits, credits, and the ledger line count with any rounding line) are set on the journal at submit or bypass. Migration 0006 backfills them for every live journal.

**Routing and bypass caps use the functional total,** because the matrix's thresholds and caps are per functional currency. So does the template's attachment threshold. The risk score's round-amount factor reads the amounts as entered.

**A reversal reuses its original's rate** when it is still the plain flip of the original: the same currency, and the same lines on the other side. It then undoes the original exactly in functional terms, rounding line included, and a test nets both journals to zero on every account. An edited reversal is a new journal in substance, and converts at its own date's rate. Automatic reversals are always plain flips, so they bypass at the original's rate.

**Batches and recurring journals.**
- **A batch's** members are converted when it is submitted. Their rates are looked up together before the transaction, and one missing rate refuses the whole batch.
- **An auto-approved recurring journal** in a foreign currency is converted as it bypasses approval. Its rate is looked up before the task's transaction. Without one, the journal is skipped and its preparer told once, and the next run creates it when the rate exists.

**Posting.**
- **The view** `posting_queue` gives each line its functional amount and rate, and the rounding line as one more line. `CREATE OR REPLACE` keeps its grant to `close_ledger_reader`.
- **DDL V008** adds nullable `fx_rate`, `fx_rate_date`, `fx_rate_type`, `fx_rate_version` and `line_kind` to `ledger.journal_lines`, with `ALTER TABLE ... ADD COLUMNS`. 7b's rows keep nulls.
  - **The allowlist.** `ADD COLUMNS` on an append-only table joins ADR-0002's allowlist as feature 19, proven three times on the S3-compatible store.
  - **The migration test** now counts one `CREATE` per registered table, plus `ALTER`s on registered tables.
- **Debits and credits** in the ledger are functional. The drill-down shows functional amounts and each line's kind, and the ERP feed's canonical JSON carries the conversion.
- **The tie-out** compares functional totals and line counts, rounding lines included. The sums never mix currencies, because each entity has one functional currency.

**Import and evidence.** CSV and XLSX gain an optional `currency` column per journal, and amounts are read with that currency's decimals. The population export adds the functional currency and totals, the rate (date, type, version and source, the source formula-safe) and the rounding amount.

**The downgrade refuses** while any conversion exists, rather than leave foreign-currency journals behind without them.

## Consequences

- **Accountants can book foreign-currency entries** in any currency a template allows, and auditors see the rate each one was booked at, frozen with its approval.
- **A month-end entry waits for its rate.** Under D9 the rate is the accounting date's, so an entry dated the last day of the month can't be submitted until that day's rate is published.
- **No triangulation.** A pair without a direct rate is refused, even when both currencies have a rate against a third.
- **A reversal departs from D9's "accounting date's rate"** on purpose: it reuses its original's rate, so that it fully undoes the original.
- **Not in 7c:** revaluation of foreign-currency balances at period end (Stage 8's close), and translation to a reporting currency (consolidation).
- **Rates on the stack** come from the generator's `tiny` profile, written into the local lake. In production they come from your rate feed into `ref.fx_rates`, a Stage 10 input.
