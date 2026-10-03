# Stage 6 plan: the journaling engine, first slice (on sample inputs)

On 2026-09-29 you asked for the journaling engine to be built now on sample data: example policy answers, and journal lines produced by automated journal definitions. You also asked that in production the accounting team can set up their own matching and journaling rules. This slice does that in pure Python, as Stage 3 did for matching.

You approved this plan on 2026-09-29 by asking for plans to be approved automatically and executed. The real inputs stay requested ([stage-6-inputs.md](../domain/stage-6-inputs.md)): the signed policy sheet, the chart of accounts and the dimensions. Nothing sample-based is activated in production. The Spark journaling job waits for the rule compiler.

## BlackLine terms and ours

| BlackLine | Here |
|---|---|
| Matching pass rules | `MatchSet` and `PassRule`, already built. The 12 template match sets can be authored and approved through the API since Stage 4. |
| Automated Journal Definition | `AutoJournalDefinition`: the event types it journals, an optional condition, and its line templates. Each template has a condition, a side, an account, an amount, dimensions and a description. The roadmap's "posting rules" are these line templates, so there is one resource, `/auto-journal-definitions`. |
| Journal lines | `ledger.subledger_lines`, rolled up into `ledger.gl_rollup_lines` with drill-down ids |
| Account segments and mapping | `AccountMapping`: key values, such as a policy switch or an attribute, mapped to an account, with an optional default |
| Chart of accounts, dimensions | `ChartOfAccounts`, `DimensionSet` |
| Policy switches | `AccountingPolicy` (Stage 2a), under `/programs` |

## Decisions

1. **Event types declare their fields.**
   - **The problem.** Rule expressions can't read the map columns of `accounting_events`.
   - **Declared fields.** An `EventType` declares flat, typed fields: amounts as `amount_<role>` (BIGINT minor units) and attributes as `attr_<name>` (strings). The standard fields are entity, currency, accounting date, program, order and seller.
   - **Policy fields.** Rules also see the policy for the event's program: `policy_trigger`, `policy_model`, `policy_shipping` and `policy_incentives`. The global switches are always available: `policy_psp_receivable`, `policy_processing_fees` and `policy_chargebacks`.
   - **Validation.** Everything typechecks in `close_contracts`, so the app validates on save.
2. **Event sources may look up other landing tables.**
   - **The source.** An event type has one or more sources. Each reads one landing table, filters it, and maps the event fields with the DSL.
   - **Lookups.** A source may look up one row of another landing table by that table's full identity key, which makes its columns available as `<alias>_<column>`. Order events look up their order this way.
   - **Rejections.** A missing lookup row, or a mapping that can't evaluate, rejects the event. Per source, rows read = events + excluded + rejected.
   - **Why this doesn't wait for the compiler.** A lookup is a plain equi-join in Spark.
3. **Which events, and which balances they prove.** The sample event types:
   - `ORDER_CAPTURED` and `ORDER_STAGE` (verification, shipped, delivered, return window closed);
   - `ORDER_REFUND` (cancel, failed verification, partial, clawback) and `SELLER_CHARGE`;
   - `PSP_FEE`, `PSP_FEE_BILLED`, `PSP_DISPUTE` and `PSP_PAYOUT`;
   - `SELLER_PAYOUT` and `PAYOUT_FUNDING`;
   - `SHIPPING_LABEL` and `CARRIER_CHARGE`;
   - `TAX_REMITTED`.

   Each balance sheet account has one source of truth, and the invariants name every expected leftover, so injected breaks show as reconciling differences:
   - **Buyer deposits:** orders captured but not yet at their trigger or refunded.
   - **Seller payables:** booked from the trigger events, not from the landing payable ledger. The ledger's duplicate payables then show against it rather than entering the ledger's books.
   - **PSP clearing:** captures, refunds, fees, disputes and payouts. The leftovers are transactions not yet paid out, and realized FX on cross-border orders, which waits for Stage 5's cross-currency matches.
   - **Payout clearing:** funding less completed payouts, leaving returned and unclaimed items.
   - **Sales tax payable:** collected less remitted.
   - **Carrier accrual:** labels not yet invoiced.
4. **The oracle is independent.** It computes the expected entries from the policy answers and the events in its own code. The sample rules are JSON data, not generated from the policy.
   - **What is compared:** net amounts per (event, version, account, currency, dimensions), in transaction and functional currency.
   - **Guards.** Flipping each policy switch must change the oracle's output on the test data, and the data must have events of each type in each program.
   - **The matrix:** every combination of one program's switches, with the others at their sample values, plus every combination of the presentation switches.
5. **Data problems versus configuration problems.** Event types declare amount identities, for example card plus store credit equals price plus buyer fee plus shipping plus tax less incentive.
   - **Data problems** go to suspense, recording the intended account or the missing mapping key:
     - an event that breaks an identity;
     - a line whose amount comes out negative;
     - a missing mapping row with no default;
     - a missing required dimension.
   - **Configuration problems** fail the event type outright. The case is a rule that doesn't balance an event whose identities hold.
   - **Circuit breaker.** If more than 0.5% of a run's events are suspended, the run fails.
6. **FX, periods and corrections.**
   - **FX (D9).** Each line is converted exactly at the daily spot rate of its accounting date and rounded half-even. The residual goes on one rounding line per event, to the entity's FX rounding account. Each line records the rate type, date and version. A missing rate fails the run, because reference data is incomplete.
   - **Periods** are an input. An event dated in a closed period posts to the first open period, flagged as a prior-period adjustment with its original period.
   - **Corrections.** A changed source row (by row hash) gets a new event version: its old lines are reversed and new ones booked. A rerun with no changes books nothing.
7. **Configurability through the API.**
   - **New kinds.** Five new configuration kinds, plus the accounting policy's existing one, go through Stage 4's maker-checker: `/event-types`, `/auto-journal-definitions`, `/account-mappings`, `/accounts` (the chart), `/dimensions` and `/programs` (the policy). Migration `0003` widens the kind check, and the `ConfigKind` extension shows as schema drift, recorded here.
   - **Scope.** They are global configurations, so authoring, approving and activating them needs the role at `GLOBAL`.
   - **Activation checks.** A definition's event types, mappings and accounts must be active, and its accounts active in the chart. A chart version that would deactivate an account an active definition or mapping uses is refused.
   - **Tests.** The authorization matrix and the route inventory cover the new routes.
   - **Samples.** The sample configurations are JSON files that load through the API's own parsing, and one goes from draft to active through the stack.
8. **Samples are labelled.** Sample configuration ids start with `sample_`, and each file carries a header. `policy-sheet-status.md` isn't touched.

## Out of this slice

- **Waits for the compiler:** the Spark journaling job.
- **Waits for Stage 5's matching adjustments:** adjustment consumption, and realized FX on cross-currency matches.
- **Later slices:** period-end revaluation, stored-value breakage and escheat, and estimating the chargeback reserve (the sample rules use a reserve account without an estimate).
- **Stage 7:** manual journals, the journal workflow and XLSX import.

## Gate `6-ref`

Every Stage 4 step runs as regression, under `caffeinate`, and:
1. Engine equals oracle for every sample event on `tiny` and `small`, and across the full policy-switch matrix on `tiny`, with the flip and coverage guards.
2. Invariants:
   - every event balances per transaction currency, and per functional currency after its rounding line;
   - each balance above equals its source of truth plus its named leftovers.
3. Data problems go to suspense and configuration problems fail their event type. The circuit breaker trips.
4. Determinism: shuffled input and reruns give identical digests. Corrections reverse and rebook, and a rerun with no changes books nothing. A closed period's late events post as prior-period adjustments.
5. Configuration: every sample validates through the API's parsing, and a journaling configuration goes from draft to active through the stack. The activation checks refuse missing or inactive references. The authorization matrix and route inventory cover the new routes.
6. ADR-0015 is written.
