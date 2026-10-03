## What this part delivers

On 2026-10-01 you signed off Stage 7b and chose 7c next, on sample inputs. This part finishes Stage 7 with manual journals in foreign currencies. The plan is [stage-7c-plan.md](stage-7c-plan.md) and the decisions are in [ADR-0018](../adr/0018-multi-currency-manual-journals.md).

- **Journals in a foreign currency.** A template lists the currencies it allows besides the entity's own, and a journal is entered in one of them. The new sample template `sample_fx_journal` allows EUR, GBP and USD.
- **Converted at submit, under D9.**
  - **The arithmetic.** Each line is converted exactly at the accounting date's daily spot rate and rounded half-even, the way the journaling engine does it, and one rounding line balances the journal.
  - **Frozen per submission.** The conversion is kept and bound into every approval, and a rate published later never changes it.
  - **A missing rate** refuses the submit, and the journal stays a draft.
- **Rates read narrowly.** One typed lookup reads `ref.fx_rates` through Spark Connect. It is not a query spec, so the read path's entity filter stays as Stage 1b signed it off. It runs only after every authorization, ETag and state check, and outside every transaction. `GET /fx-rates/{from}/{to}/{date}` shows a preparer the rate before they submit.
- **Functional terms where they matter.**
  - Routing, bypass caps and attachment thresholds use the functional total.
  - A reversal reuses its original's rate, so the two net to zero.
  - Batches and auto-approved recurring journals convert the same way.
- **Posting.** Ledger lines carry both amounts, the rate and the rounding line. DDL V008 adds the rate columns with `ADD COLUMNS`, which becomes parity feature 19. The trial balance, the drill-down and the tie-out work in the functional currency.
- **Import and evidence.** CSV and XLSX gain a `currency` column, with amounts read at that currency's decimals. The population export shows each journal's currency, rate and rounding.

## Results

| Check | Result |
|---|---|
| App tests (Postgres) | 191 passed, plus the audit-chain residual as a strict xfail; 18 of them new for multi-currency journals |
| Unit tests | 477 passed, contracts coverage 96%; the D9 property test runs 10,000 generated journals |
| Spark tests | the posting job writes a EUR journal's functional amounts, rate and rounding line; V008 evolves the ledger table and every table matches its contract |
| Rate lookup on Spark | the latest version of each key at one pinned table version, and nothing for a missing key or table |
| Parity | 19 allowlisted features, 3 of 3 runs on the S3-compatible store |
| Authorization matrix | 194 operations × 12 roles × 3 scopes = 6,984 checks, 0 unexpected allows or denies |
| End-to-end suite | 52 passed on the stack in 15 minutes, before the gate |
| Stack | alice prepares a EUR journal in US01 at the rate the route shows, dave and erin approve, dave posts. Its ledger lines are USD under D9, the trial balance balances, the tie-out holds and the stub ERP confirms it |

## Found and fixed while building

1. **Routing would have compared euros with dollar thresholds.** `route()` summed the draft's own amounts, so a 9,500 EUR journal (10,300.02 USD) would have needed one approval level instead of two. Routing, bypass caps and the template's attachment threshold now all use the functional total, and a test pins it.
2. **The rate lookup's module loaded Spark into the app at start-up.** It imported the DataFrame module, and so pyspark. It now imports Spark only when it runs. It also moved under `close_platform/query`, where the Spark-ban lint covers it.
3. **A journal approved by a bypass was never submitted.** That covers an automatic reversal and an auto-approved recurring journal. Their submission number is 0, and the first version of the conversion table refused it. It now accepts 0, before the migration was applied anywhere but the test database.
4. **A replay would have failed its own checks.** A submit now checks authority, ETag and state before its rate lookup, outside the transaction. A replay of an earlier, successful submit would have met the journal's new ETag and got a 412 instead of its stored response. A read-only replay lookup now runs first. It also fixes 7b's XLSX limitation: a replayed import no longer parses the workbook again.
5. **Two earlier tests pinned what 7c changes.**
   - The Stage 0 regression expected exactly 18 parity features. It now counts them, and there are 19.
   - The migration test expected one statement per table. It now counts one `CREATE` per table, plus `ALTER`s on registered tables.
6. **The no-lock test can fail.** A mutation that moves the lookup inside a locked transaction makes it fail with `LockNotAvailable`, so it really checks the lock.
7. **A new vulnerability in both images.** Trivy found CVE-2026-103111 (HIGH, in Debian's `libpcre2-8-0`), published since 7b's scan. Debian has a patched package, so both Dockerfiles now upgrade it the way they upgrade OpenSSL, without pinning a version.
8. **A 7a contract test** still called `route()` without the functional total. The full unit suite found it in the pre-check; it was the only one.
9. **A review before the gate found a race.** With the checks moved before the transaction, a duplicate submit could look for a stored response just before the first request committed, then fail its own ETag check with a 412. Before 7c it would have waited on the idempotency key and replayed. A refused pre-check now looks once more, and a duplicate that lost the race replays. A test proves it, and fails without the fix. The same review widened the "checks before the lookup" test to cover state, batch membership, separation of duties and a batch's ETag, and showed batch approvals carry each member's conversion hash.

## Where the build differs from the plan

1. **The drill-down shows functional amounts.** `/gl/lines` now also returns each line's functional amount and currency, and whether it is an entry or the rounding line. That is a change to the queryable registry, alongside the table registry.
2. **Rates on the stack come from the generator.** The stack test writes the generator's `tiny` profile into the local lake when it has no rates yet. Your real rate feed is a Stage 10 input.
3. **The replay lookup** also changed 7b's XLSX import (item 4 above). ADR-0017 is amended to say so.

## Needs your decision

1. **Accept ADR-0018,** including its three rules:
   - **The accounting date's rate, with no fallback.** A month-end entry waits until that day's rate is published.
   - **No triangulation.** A pair without a direct rate is refused.
   - **Reversals reuse their original's rate.** A plain flip of the original reuses it, so the reversal fully undoes the original. This departs from D9's "accounting date's rate" on purpose.
2. **Accept the contract changes:**
   - the journal draft gains a currency, and the template allowed currencies;
   - the recurring journal gains a currency;
   - the table registry gains the ledger's rate columns;
   - ADR-0002's allowlist gains feature 19, `ADD COLUMNS`;
   - ADR-0017 is amended for XLSX replays.
3. **The real inputs** before production:
   - where rates come from, and how they reach `ref.fx_rates`;
   - whether a month-end entry may fall back to the last published rate;
   - whether pairs may triangulate through USD.

   These join the Stage 6 and Stage 7 inputs already requested.
4. **How images take Debian's security fixes.** This is the second gate a new Debian vulnerability has blocked (OpenSSL in 7a, PCRE2 now), each fixed by naming the package to upgrade.
   - **Keep naming packages:** exact control, at the cost of a fix like this each time.
   - **`apt-get -y upgrade` at build time:** takes every fixed package, and the SBOM still records what was installed.
5. **Next:** Stage 8 on sample inputs (close management: reconciliations, roll-forward, flux, and the close checklist), Stage 9 (the frontend), or wait for the real inputs.

## Carried forward

- **Stage 8:** revaluation of foreign-currency balances at period end.
- **Consolidation:** translation to a reporting currency.
- **Blocked on the Spark rule compiler:**
  - the engine's automated entries in the journal population;
  - GL-history risk factors.
- **Stage 10:**
  - a real rate feed;
  - the posting job's Lakebase access;
  - a real ERP integration;
  - a per-journal hash comparison in the tie-out.
- **Stage 5:** the configuration-change lock, and D19's confirmation.
