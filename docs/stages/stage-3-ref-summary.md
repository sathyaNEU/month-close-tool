## What this slice delivers

Stage 3's Spark rule compiler could not be written in this session. You chose to build everything in Stage 3 that doesn't need it first. That is this slice: the reference matcher, the template match sets and their golden tests. It is part of 3b in [stage-3-plan.md](stage-3-plan.md), done ahead of 3a. The semantics are in [ADR-0013](../adr/0013-reference-matcher-semantics.md).

- **The reference matcher** (`close_engine.reference`, pure Python, no Spark) is the specification the Spark matcher will be held to.
  - **Normalization.** It normalizes landing rows through the bound default data sources.
  - **Conditions.** It compiles each pass rule's conditions once. Text, date, currency and expression conditions are lowered to the rule DSL, so the future Spark compiler has one definition to match.
  - **Rule kinds.** `1:1` uses the three ambiguity policies. `1:M` and `M:1` try the whole keyed group first, then a bounded meet-in-the-middle subset search.
  - **The waterfall.** Rules run in order: suggestions, supersession by automatic matches, candidate and search limits, and timing rules. No group ever mixes entities in a matching set.
- **The templates.** Templates 1, 2, 3, 6, 9 and 10 are 12 match sets over the default data sources (`close_contracts.templates`). A role binds one data source, so Stripe and Adyen are separate match sets, as are the bank formats and the two sides of the tax reconciliation. The match sets are frozen in the JSON Schema snapshots.
- **Scoring.** The matcher's results are scored against the generator's answer key by source identity:
  - 0 false automatic matches;
  - clean recall;
  - every other expected outcome reached, breaks included.

  `tools/reference_scores.py` reruns the scoring on `small` seeds 11, 23 and 47 and on `medium` seed 11.
- **Tests.** `make test-reference` covers:
  - conditions;
  - the subset search, held to brute force by a property test;
  - the waterfall;
  - golden tests of every template on `tiny` and `small`, with pinned digests;
  - an order-independence test;
  - an invariant property test (200 examples, and 10,000 in the gate).

## Results

| Run | Items | False automatic matches | Clean recall | Every expected outcome reached |
|---|---|---|---|---|
| `small` seed 11 | 78,783 | 0 | 100% | yes |
| `small` seed 23 | 78,676 | 0 | 100% | yes |
| `small` seed 47 | 79,978 | 0 | 100% | yes |
| `medium` seed 11 | 849,043 | 0 | 100% | yes |

The gate's run times and peak memory for these four runs are in the criteria below.

On `medium`:
- 514 rounding differences, 63 late bank credits and 520 payouts over a duplicated payable become suggestions;
- all 526 duplicates, 418 unclaimed payouts and 1,051 carrier corrections stay out of automatic matches;
- 3,209 split tenders, 2,455 FX differences and 1,037 partial captures are matched automatically, as their breaks expect.

## Found and fixed while building and testing

1. **A defect in a signed-off Stage 2b data source.**
   - **The bug.** The default carrier-invoice source mapped the entity as two letters of the carrier account (`EU`, not `EU01`). No invoice line belonged to any entity, so template 9 matched nothing.
   - **Why 2b missed it.** Its test checked amounts against the control totals, never the entity.
   - **The fix.** A fixed carrier-account-to-entity table, which the generator now also reads. An unknown account is rejected at normalization and shows in the control totals. The every-row mapping test now checks that each mapped entity exists.
2. **Two answer-key expectations for template 9 and template 6.** Each was corrected for what the break means, not to fit the matcher.
   - **Carrier corrections (template 9).** The key expected the whole label, transportation line and correction as one suggestion. The right result is to match the label to its transportation line and leave the correction line billed on top open as the exception. The whole group is a suggestion only when the correction equals the quoted price, because then either line alone balances the label. No seed produced that case, so a test builds it.
   - **Duplicate payables (template 6).** The key expected the payout over a batch holding a double-posted payable to match automatically. The original and the duplicate are identical, so the batch balances two ways, and a reviewer has to choose. It is now a suggestion, and the duplicate's own group stays open with the break.
3. **Groups could have mixed entities.** Nothing stopped a keyless rule, or a key shared across entities, from pairing a US01 item with an EU01 item. A user scoped to one entity would then see half a group. Each rule in a matching set now runs one entity at a time, and the property test checks it. Intercompany sets cross entities on purpose.

## Where the build differs from the plan

1. **Order of work.** This is a slice of 3b done before 3a, because 3a's compiler is blocked.
2. **Match set count.** The templates are 12 match sets rather than six, because a role binds a single data source.
3. **Not in the reference yet.** Each of these raises an error rather than guessing, and none is used by these templates. They will be specified in ADR-0013 before the Spark engine implements them:
   - `M:M`, `self_net` and `chain`;
   - `best_unique` and greedy for the aggregate kinds;
   - FX-converted currency;
   - state across runs (item versions, broken groups) and commands.
4. **Gate 3 criteria this slice cannot meet** (they need the Spark engine):
   - engine groups equal reference groups;
   - determinism across partitions and masters;
   - crash safety;
   - the Spark and interpreter differential test;
   - compiler fuzzing;
   - the `medium` performance run in one pod;
   - compiler coverage.
5. **Break coverage.** The 100% break criterion covers the 8 break types these templates score: rounding, FX differences, split tenders, partial captures, timing, duplicates, unclaimed payouts and carrier corrections. The generator's other 5 (partial refunds, clawbacks, chargebacks, late wins and invoiced fees) are scored when their templates (4, 5, 7, 8, 11 and 12) are built.

## Needs your decision

1. **The changes to signed-off Stage 2b work.**
   - The two answer-key expectations (templates 6 and 9).
   - The carrier data source's entity mapping.
   - The JSON Schema snapshots: the default data sources changed, and the template match sets are new.

   ADR-0013 records all three, and ADR-0012 points to it. Please confirm you accept them.
2. **The Spark rule compiler** still needs a developer, or a later session. Everything else in Stage 3 (the normalize job, the Spark matcher, commands and crash safety) builds on it.
3. **The policy sheet** is still "unknown". It has to be settled before Stage 6.
4. **The Spark Connect restart defect** is still open, to fix before Stage 10.

## Carried forward

- **Stage 3's memory budget.** It will be measured when the Spark matcher exists. The pure-Python reference is only the oracle, and it runs on the host, not in the jobs container.
- **D19** stays open until Stage 5.
- **The documentation polish pass** is deferred to the end of the project.
