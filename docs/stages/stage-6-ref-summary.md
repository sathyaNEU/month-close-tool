## What this slice delivers

You asked for journaling built now on sample data, with example policies and journal lines produced by automated journal definitions. You also asked that, in production, the accounting team sets up its own matching and journaling rules. This slice builds that in pure Python, as Stage 3 did for matching. The plan is [stage-6-plan.md](stage-6-plan.md), the decisions are in [ADR-0015](../adr/0015-journaling-configuration-and-reference-engine.md), and the sample accounting is written out in [sample-journal-rules.md](../domain/sample-journal-rules.md).

- **Journaling rules are configuration.** Six kinds go through Stage 4's maker-checker as global configurations:

  | Resource | What it holds |
  |---|---|
  | `/event-types` | How landing rows become accounting events |
  | `/auto-journal-definitions` | BlackLine's Automated Journal Definitions, with their line templates |
  | `/account-mappings` | Key values mapped to accounts |
  | `/accounts` | The chart of accounts |
  | `/dimensions` | The dimensions and where they are required |
  | `/programs` | The policy answers |

- **Active rules stay consistent.**
  - **On save,** every version is validated: expressions typecheck against the event type's fields, and references must exist.
  - **A definition goes live** only when its event types, mappings and accounts are live, together with a chart, a dimension set and a policy.
  - **New versions can't break a live definition.** A new version of an event type, a mapping, the dimension set or the chart is refused if it would.
  - **Nothing in use can be retired.** A new version replaces it instead.
- **Matching was already configurable.** Data sources, and match sets with their pass rules, go through the same API since Stage 4, and the 12 template match sets are ready to author.
- **The reference journaling engine** turns landing rows into events, and events into journal lines through the definitions.
  - **Data problems go to suspense** with the intended account:
    - a negative amount;
    - a missing mapping row;
    - a missing dimension;
    - a broken amount identity.
  - **A configuration problem fails its event type:** lines that don't balance while the identities hold. The run fails when more than 0.5% of events are suspended.
  - **Each line is converted** at the day's rate, with one rounding line per event.
  - **Closed periods:** a late event posts to the first open period as a prior-period adjustment.
  - **Corrections:** a corrected row is reversed and rebooked, a rerun books nothing, and a vanished row is voided.
  - **Rollups** carry drill-down ids.
- **The sample configuration,** every id starting `sample_`:
  - policy answers for the 4 programs and the 3 presentation switches;
  - 30 accounts and 2 dimensions;
  - 5 account mappings;
  - 13 event types and 16 definitions.

  They are published as API request bodies in `docs/samples/journaling/` (37 files, numbered in activation order), and a test keeps them equal to the code.

## Results on the generated data

| | tiny, seed 11 | small, seed 11 |
|---|---|---|
| Landing rows read | 2,096 | 181,543 |
| Events | 1,101 | 93,526 |
| Posted | 822 | 68,323 |
| No entry (order stages that aren't the program's trigger) | 279 | 25,203 |
| Journal lines (FX rounding lines) | 2,229 (15) | 188,608 (594) |
| Suspense lines, failed event types, rejected rows | 0, 0, 0 | 0, 0, 0 |
| Engine against the independent oracle, net amounts by event, account, currency and dimensions | 2,214 keys, 0 differ | 188,014 keys, 0 differ |
| Deriving the events, then booking them (one Python process) | under 0.1 s each | 1.2 s, then 1.7 s |

- **Performance baseline.** The table's timings are the pure-Python baseline the roadmap's Gate 6 asks for. The scoring process peaked at 808 MiB, including generating the data. The Spark job's baseline comes with that job.
- **Every policy combination:** all 212 on tiny, the engine against the oracle, with 0 mismatched.
- **Every switch matters:** each of the 32 single-switch flips changes the books on small, and the engine equals the oracle under every flipped policy.
- **Balances.** Every balance sheet account ties to its source of truth on small at 8 July, 20 July, 1 August and 31 December, with the injected breaks as named reconciling items. At 1 August:
  - 9 buyer deposit balances;
  - 48 duplicate seller payables as reconciling items;
  - 2,822 labels still in the carrier accrual;
  - 141 failed-verification refunds reconciling sales tax;
  - 8 PSP clearing balances awaiting realized FX.

## Found and fixed while building and testing

1. **Lookups ran before filters.** 234 events were rejected because the order lookup ran on rows the source's filter would have excluded. A filter that reads only the source's own columns now runs first.
2. **The generator's FX rates stopped short.** The engine refuses to book without a rate, and it found dates the generator emits with no rate: late reversals land about 100 days after the period. Rates now run to 130 days after, and a test checks every emitted date has one. Rows are only appended, so earlier rates are unchanged.
3. **The flip guard found switches that changed nothing.** The sample cap on contra revenue never bound, so contra revenue and "contra revenue up to the cap, then marketing" booked the same.
   - **The new cap.** The sample cap is now the revenue from the buyer on that order: the buyer fee, plus shipping when booked gross, plus the price for owned inventory. The direct-ship sample books shipping net, so the cap binds there too.
   - **This is a sample choice, not an accounting finding.** The policy sheet's option caps at the revenue earned from the buyer, which can mean all their orders to date. The sheet now asks your controller which period and which revenue the cap uses.
4. **The sales tax invariant was wrong at mid-July cutoffs.** A tax document shows only its final status, so one voided later looked voided at earlier cutoffs. It now counts as committed until its order is cancelled.
5. **Two edge tests could not fail at first.** The circuit-breaker test put 2 broken events among 401 (under 0.5%), and the FX test used amounts that left no rounding residual. Both now use values where the behaviour shows.
6. **A review before the gate found three more gaps.**
   - **Configurations in use could be retired.** A chart, dimension set, policy, event type or mapping could be retired while an active definition used it. A new version of an event type, a mapping or the dimension set could also break a live definition. Both are now refused. The same gap existed for data sources used by active match sets since Stage 4, and that is refused now too. Tests prove each case both ways: the tests fail when the guards are switched off.
   - **The engine was never booked under a flipped policy on small.** On tiny some flips change nothing, so the engine alone was never checked against those switch values. The flip guard now books every flip with the engine as well.
   - **The invariants could pass while comparing nothing.** At the year-end cutoff, buyer deposits and the carrier accrual have no open balances. The test now requires every check to compare real balances at the mid-period cutoffs.
7. **Migrations 0002 and 0003 said a test compared their role and kind lists with the code, but none did.** That test now exists, with a test that 0003's downgrade removes journaling versions and its upgrade allows them again.

## Where the build differs from the plan

1. **The flip guard runs on small, not tiny** (item 3 above). The policy matrix still runs on tiny.
2. **The journaling configuration APIs came forward from Stage 7.** The journal workflow stays there: manual journals, approvals, posting and XLSX import.
3. **There is no separate `/posting-rules` resource.** The roadmap's posting rules are a definition's line templates.
4. **Rollups need a contract change.** The reference rollups sum functional amounts, which `ledger.gl_rollup_lines` doesn't have yet. The Spark job adds them with a migration.
5. **Realized FX is not booked yet.** Cross-currency settlements stay in PSP clearing in their own currency until Stage 5's cross-currency matches, and the invariant names them.

## Needs your decision

1. **Accept ADR-0015 and the sample semantics:**
   - suspense for data problems, a failed event type for a configuration problem, and the 0.5% circuit breaker;
   - prior-period adjustments into the first open period;
   - corrections by row hash.
2. **Accept the new retirement rule, which changes signed-off Stage 4 behaviour.** A configuration that an active one uses can't be retired. Replace it with a new version, or retire its users first. This covers data sources used by active match sets.
3. **Accept the generator change to the signed-off Stage 2b data.** FX rates now cover every date the generator emits. Rows are only appended, and the Stage 3 golden digests still match unchanged.
4. **Accept the contract snapshot change.** There are 5 new snapshots, and `ConfigKind` gained 5 values in the envelope snapshots.
5. **The real inputs are still needed** before anything goes live in production. Templates are in [stage-6-inputs.md](../domain/stage-6-inputs.md):
   - your controller's policy answers (the signed policy sheet), including the new question on the incentive cap: per order or across the buyer's orders, and which revenue it counts;
   - the chart of accounts;
   - the dimensions.

   Nothing sample-based is activated in production.
6. **Still open from earlier stages:**
   - the Spark rule compiler, which the Spark matching and journaling jobs wait for;
   - the Spark Connect restart defect, before Stage 10.

## Carried forward

- **Stage 5:**
  - the matching APIs;
  - adjustment consumption;
  - realized FX on cross-currency matches;
  - deeper checks on a data source's new version (whether it still suits the active match sets).
- **Stage 7:** the journal workflow on these configurations.
- **Stage 9:** the rule builder screens, on these same APIs.
- **Later Stage 6 slices:**
  - the Spark journaling job, once the compiler exists;
  - a cumulative incentive cap, if your controller chooses one;
  - period-end revaluation;
  - stored-value breakage and escheat;
  - estimating the chargeback reserve.
- **Some configuration problems show only when the rules run.** They send the affected lines to suspense and trip the circuit breaker, instead of being refused on save. A save-time check for each comes with the rule builder:
  - a definition that never supplies a dimension its accounts require;
  - a new policy version choosing a switch value that a mapping keyed on that switch has no row for.

  The samples avoid both, as the 212-policy matrix shows.
- **Two changes at the same instant.** The consistency checks read other configurations without locking them. So an approver activating a definition and another retiring its event type in the same instant could both pass, and Stage 4's match-set checks have the same exposure. With 7 users this is unlikely. The fix is a transaction-level advisory lock around configuration changes, due with Stage 5's hardening, or sooner if you prefer.
