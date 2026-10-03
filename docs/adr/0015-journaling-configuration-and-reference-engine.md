# ADR-0015: journaling as configuration, and the reference journaling engine

Status: proposed with Stage 6 (first slice). Date: 2026-09-29. Implements, in pure Python and on sample inputs, these roadmap Stage 6 items:
- events with versions and corrections;
- posting rules and account derivation;
- validation and the suspense policy;
- FX conversion with rounding;
- prior-period adjustments;
- rollups;
- the journal oracle.

It also implements the configuration side of Stage 7's journaling config APIs.

## Context

The user asked for two things:
- **Now:** journaling built on example data, meaning example policies, and journal lines produced by automated journal definitions.
- **In production:** the accounting team sets up its own matching and journaling rules.

The matching half already exists: match sets and pass rules are maker-checker configuration since Stage 4. The Spark rule compiler is blocked, so, as for matching, the engine is built first in pure Python, and an independent oracle checks it. The real policy answers, chart of accounts and dimensions are still requested (`docs/domain/stage-6-inputs.md`).

## Decision

**Journaling rules are configuration** (`close_contracts.config.journaling`). Everything below is maker-checker versioned through the Stage 4 API as a global configuration:

| What | Resource | Model |
|---|---|---|
| How landing rows become accounting events | `/event-types` | `EventType`: sources, keyed lookups, declared fields (`amount_<role>`, `attr_<name>`) and amount identities |
| BlackLine's Automated Journal Definition | `/auto-journal-definitions` | `AutoJournalDefinition`: the event types, a condition, and line templates (side, account, amount, dimensions, description) |
| Account choice by key | `/account-mappings` | `AccountMapping`: key values to an account, with an optional default |
| Chart of accounts | `/accounts` | `ChartOfAccounts`: types, normal balances, postable, entities, currency, status, suspense and FX rounding accounts |
| Dimensions | `/dimensions` | `DimensionSet`: required by account type or account, closed or open value lists |
| Policy switches | `/programs` | `AccountingPolicy` (ADR-0011) |

There is one resource for definitions. The roadmap's "posting rules" are a definition's line templates, so `/posting-rules` is not a separate concept.

**Expressions are the rule DSL.**
- **Event mappings** read the landing row and its lookups.
- **Rule expressions** read the event's declared fields, its `event_type`, and the policy for its program (`policy_trigger`, `policy_model`, `policy_shipping`, `policy_incentives`) or the global switches (`policy_psp_receivable`, `policy_processing_fees`, `policy_chargebacks`). This is what makes a policy switch change the books without anyone editing a rule.
- **Validation** lives in `close_contracts.config.journal_validate`, so the app validates on save without shipping the engine.

**Event derivation.**
- **Lookups.** A lookup names the target table's full identity key, so it finds at most one row. Spark does this as an equi-join, without the compiler.
- **Filtering.** A filter that reads only the source's columns runs before the lookups.
- **Rejections.** A missing lookup, a failed mapping or a null required field rejects the event. Per source, read = events + excluded + rejected.
- **Identity and versions.** An event's id hashes its type, its source and the row's identity. Its row hash covers the row and the rows it looked up.

**Posting.**
- **Which lines are written.** Every definition covering the event type whose condition holds contributes its lines whose condition holds. A zero amount writes nothing.
- **Data problems go to suspense**, recording the intended account (or the mapping key that found nothing) and the reason:
  - a negative amount, which posts its absolute value on the other side;
  - a missing mapping row;
  - a missing or unknown dimension value;
  - an event that breaks one of its type's identities, whose difference is balanced to suspense.
- **Configuration problems fail the event type for the run:**
  - lines that don't balance while the identities hold;
  - an account the chart doesn't allow for the entity or currency.

  The event type's state stays as it was, so the next run retries it.
- **Circuit breaker.** More than 0.5% of journaled events in suspense fails the run.
- **FX (D9).** Each line is converted exactly at the accounting date's daily spot rate and rounded half-even. The residual goes on one rounding line per event, and each line records the rate, its date and its version. A missing rate fails the run.
- **Periods.** An event dated in a closed period posts to the first open period as a prior-period adjustment. The periods are an input: Postgres is authoritative (D6), and the Spark job will read the fenced status version.
- **Corrections.** A changed row hash books the next event version: the booked lines are reversed and new ones booked. An unchanged event books nothing, and a vanished one is reversed and voided.
- **Rollups.** Lines roll up by entity, period, account, dimensions and currency, with deterministic ids that every line carries for drill-down.
  - **The contract gap.** The reference rollup also sums functional amounts, which the `ledger.gl_rollup_lines` contract lacks. The Spark job adds them in a migration.

**Active rules stay consistent** (`close_app.journal_configs`). The checks keep every active definition's references live and valid, one change at a time (two changes made at the same instant are covered under Consequences):
- **Activating a definition** needs its event types, its mappings and its accounts active, plus an active chart, dimension set and policy.
- **A mapping's accounts** must be active in the active chart.
- **One at a time:** only one chart, one dimension set and one policy are active.
- **Event-type codes** are unique among active types.
- **New versions can't break what is live.**
  - A new chart version can't make an account inactive or unpostable while an active definition or mapping uses it.
  - A new version of an event type, a mapping or the dimension set can't give an active definition a problem it didn't have before, such as a field it reads, a mapping key or a dimension it names.
- **Nothing in use can be retired:**
  - an event type or a mapping while an active definition uses it;
  - the chart while any definition or mapping is active;
  - the dimension set or the policy while any definition is active.

  A new version replaces one instead. The same rule now covers a data source that an active match set uses, which Stage 4 missed.
- **Save-time checks.** When a version is saved, its expressions typecheck against the latest versions of what it names.

**Held to an independent oracle.**
- **The oracle.** `close_engine.reference.journal.oracle` books the sample accounting straight from the policy answers. It never reads the definitions or mappings, and it converts currencies with its own code.
- **What is compared:** net amounts per event, version, account, currency and dimensions, in transaction and functional currency.
- **The runs:**
  - under the sample policy on `tiny` and `small`;
  - under all 212 policy combinations on `tiny` (every combination of one program's switches, and every combination of the presentation switches).
- **The flip guard.** Each of the 32 single-switch flips changes the oracle's books on `small`, and the engine books the same as the oracle under each flipped policy. On `tiny` some flips change nothing, so the matrix there can't prove those switch values on its own.
- **The invariants** tie every balance sheet account to its source of truth at several cutoffs, with injected breaks as named reconciling items.

**Samples.** The sample configuration (`close_contracts.samples.journaling`, published as API request bodies in `docs/samples/journaling/`) is labelled `sample_` and documented in `docs/domain/sample-journal-rules.md`. Production activation waits for the controller's real answers.

## Consequences

- **The accounting team can change rules as data,** approved like any other configuration change: new event types, definitions, mappings, charts and policies. A rule builder UI comes in Stage 9, on these same APIs.
- **The Spark journaling job must reproduce this engine,** checked by the same oracle and invariants. Until the compiler exists, it waits.
- **Not in this slice:**
  - realized FX on cross-currency matches, and adjustment consumption (both need Stage 5);
  - the `gl_outbox` hand-off to the general ledger, which comes with the Spark job;
  - revaluation, stored-value breakage and escheat, and estimating the chargeback reserve;
  - manual journals and the journal workflow (Stage 7).
- **Two limits carried forward.**
  - **Concurrent changes.** The consistency checks read the other configurations without locking them, at Postgres's default isolation. An approver activating a definition and another retiring its event type at the same instant could both pass. Stage 4's match-set checks have the same exposure. The fix is a transaction-level advisory lock taken before any configuration transition's checks, due with Stage 5's hardening.
  - **Problems found only at run time.** Some problems don't show until the rules run, where they send lines to suspense and trip the circuit breaker:
    - a definition that never supplies a dimension its accounts require;
    - a new policy version choosing a switch value that a mapping keyed on that switch has no row for.

    Save-time checks for both come with the rule builder.
- **Found while building:**
  - **FX rates.** The generator's rates stopped before its last event dates; they now cover every date it emits, with a test. Earlier rates are unchanged.
  - **The flip guard** showed switches whose flip changed nothing on the data. The sample cap on contra revenue never bound, so contra revenue and "contra revenue up to the cap, then marketing" booked the same.
    - The sample cap is now the revenue from the buyer on that order: the buyer fee, plus shipping when booked gross, plus the price for owned inventory. The direct-ship sample books shipping net, so the cap binds on the data.
    - This is a sample choice, not an accounting finding. The policy sheet's option caps at the revenue earned from the buyer, which can mean all their orders to date, and the sheet now asks the controller which period and which revenue the cap uses.
    - A cumulative cap needs a running revenue balance per buyer, which this slice doesn't keep.
