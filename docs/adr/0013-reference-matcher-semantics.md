# ADR-0013: reference matcher semantics and the Stage 3 templates

Status: proposed with Stage 3. Date: 2026-09-28. Implements roadmap Stage 3 items 5 and 6, and fixes the semantics that item 2 (the Spark rule lowering) must reproduce.

## Context

Gate 3 requires Spark matcher groups to equal the reference matcher's groups exactly. That makes the pure-Python reference the specification of matching, not just a test aid. The roadmap ("How one pass runs") settles most of the behaviour, but it leaves some choices open:
- how candidates are counted for aggregate kinds;
- the base of a basis-point tolerance;
- what happens to contested items and to superseded suggestions within a run.

The Spark compiler is not written yet, so the reference is also the only matcher that runs against the answer key in this part of Stage 3.

## Decision

**Where it lives.** `close_engine.reference` holds the matcher. It has no Spark dependency:
- `items` normalizes rows through a bound data source;
- `conditions` compiles and evaluates the rule conditions;
- `subset` runs the bounded subset search;
- `kinds` holds the rule kinds and the ambiguity policies;
- `matcher` runs the waterfall;
- `score` and `runner` score the templates against the answer key.

Every decision is made from sorted item ids and never from input order, which a test checks by shuffling the input.

**Conditions.**
- **Orientation.** A rule's first side is `left` and its last side is `right`, for every kind.
- **Pairwise conditions** are lowered to one boolean DSL expression over both sides' prefixed fields and evaluated by the interpreter. That covers text (except fuzzy), date windows (calendar or business days, asymmetric), equal currency and free expressions. The Spark compiler therefore compiles the same IR, and there is one definition of each operator. For `contains`, `starts_with` and `ends_with`, the right operand is the needle, and it must be at least `min_needle_length` characters.
- **Fuzzy text** stays in Python: Levenshtein, or Jaro-Winkler, quantized to integer basis points. The Spark engine must reuse the same function in a UDF.
- **Keys** compare as tuples. A null or empty key value never matches.
- **Amounts and signs** are aggregate: each side's operand is summed over the side's items.
  - A basis-point tolerance is taken of the one side's total: the left side, except in `M:1`. The subset search therefore works to a fixed tolerance.
  - `sign: absolute` compares absolute totals.
  - `same` and `opposite` check the signs of the two sides' `amount_minor` totals and leave the amount comparison as written. For example, Adyen's negative payout line is compared with `neg(a.amount_minor)`.
- **Excluded items.** An item whose amount operand is null takes no part in the rule.

**Blocking and limits.**
- **Keyed rules** only pair items with equal keys. A key block with more than `max_block_pairs` pairs is skipped, and its items are flagged `too_many_candidates`.
- **Keyless rules** are one block in the reference. The block limit doesn't apply to them there, because the Spark engine's date and amount buckets are an implementation detail.
- **`1:1` candidates.** An item with more than `max_candidates_per_item` candidates is flagged and never matched. Its edges still count against its neighbours, so it cannot make them look unique.
- **Aggregate candidates.** For `1:M` and `M:1`, the candidate limit counts candidate groups (distinct `group_by` values) per one-side item, not many-side items. A Stripe payout has hundreds of balance transactions, and a tax remittance has thousands of documents, but each is one group.

**Kinds and ambiguity.**
- **`1:1` builds the candidate graph**, and the policy then decides:
  - `unique_only` takes a pair when each item is the other's only candidate. It makes no suggestion otherwise, like Oracle's "No Ambiguous".
  - `best_unique` takes the strictly best pair for both items, where best means the lowest total amount difference.
  - `greedy_total_order` scans the pairs in (difference, left id, right id) order. A pair taken while another free pair with the same difference touched either item is flagged ambiguous. An ambiguous automatic pair becomes a suggestion unless the rule sets `allow_ambiguous_auto`. The Spark engine's mutual-best rounds give the same result as this scan, because the order is strict.
- **`1:M` and `M:1` group each one-side item's candidates** (those passing the pairwise conditions) by `group_by`, and search each group.
  - **The search.** The whole group is tried first, and it wins when it balances, even if a subset would too. Otherwise a meet-in-the-middle search on the first amount operand lists every balancing subset. It is held to brute force by a property test.
  - **Limits.** A group over `subset_max_candidates` is flagged `too_many_candidates`. A search over `work_budget` is flagged `search_limit_reached`, and a partial answer is never used. Work counts the half-subsets listed plus the pairings checked.
  - **Outcomes.** One solution gives a group with the rule's outcome. Two or more give a suggestion of the lexicographically smallest, flagged ambiguous.
  - **Contested items.** A many-side item in the solutions of two one-side items is contested, and neither item gets a group from the rule. An item that could not be decided (a limit) still counts as a claimant of its candidates.
- **Not in the reference yet:** `best_unique` and `greedy_total_order` for aggregate kinds, `M:M`, `self_net`, `chain`, and FX-converted currency. Each raises `NotImplementedError` rather than guessing. The Stage 3 templates don't use them. They will be specified here before the Spark engine implements them.

**The waterfall.**
- **Membership.** An item belongs to a match set when its entity is one of the set's entities and its source's scope holds. An item in the scope of two roles (one source bound twice, as in Adyen's payout and batch lines) is a configuration error.
- **Entities.** In a `matching` set each rule runs once per entity, so no group ever mixes entities. A user scoped to one entity can therefore always see a whole group. An `intercompany` set runs each rule across its entities at once, because crossing entities is its purpose.
- **The pool** of a rule is the items on its sides without an active group. Items with a pending suggestion are in the pool of automatic rules only.
- **Deciding a rule.** A rule decides from the pool as it stood when it started, then its groups are applied in item-id order.
- **Supersession.** An automatic group supersedes any pending suggestion of its items. The suggestion's other items return to the pool for the rules that follow in the same run.
- **Suggestions** are only made from items that are neither matched nor already suggested.
- **Timing rules** run last. An open item that meets the rule's condition is in transit while `as_of` is on or before its transaction date plus the expected clearing business days.
- **Group ids** hash the match set, the run, the rule and the sorted item ids.

**Templates.** `close_contracts.templates` defines templates 1, 2, 3, 6, 9 and 10 as 12 match sets over the default data sources. A role binds one source, so a template spanning sources becomes several match sets:
- templates 1 and 2: Stripe and Adyen apart;
- template 3: each payout source against each bank format its entities use;
- template 10: two match sets, because each tax document is reconciled once to its order and once to its remittance, and needs its own state in each.

Tolerances carry a business reason, recorded in the module:
- rounding within one currency unit is suggested;
- a bank credit more than two business days late is suggested;
- a payout that didn't complete never matches.

The match sets are part of the JSON Schema snapshots, so any change shows as drift.

**Scoring.** Items are compared by (landing table, source key).
- **Automatic groups** must be reached exactly.
- **Suggested groups** are reached when none of their items was matched automatically and their one-side items are suggested. The rest of the suggestion is not compared, because a tie between identical items has no single right answer.
- **Open groups** are reached when none of their items was matched automatically.
- **A false automatic match** is any automatic group that is not exactly an expected one.

## Consequences

- **Results.** `tools/reference_scores.py` reruns the scoring and prints one JSON verdict. Every template family scores 0 false automatic matches, 100% clean recall and 100% of expected outcomes on `small` seeds 11, 23 and 47 and on `medium` seed 11. `medium` takes about 25 s, and the whole run peaks at about 4.2 GiB in pure Python on the host.
  - The breaks reach their expected outcomes. On `medium`, for example: 514 rounding suggestions, 63 late bank credits suggested, 526 duplicates never auto-matched, and 1,051 carrier corrections left open.
- **Answer-key corrections.** Building the matcher corrected two of the Stage 2b answer key's expectations. Both were justified by what the break means, not by what the matcher produced:
  - **Carrier corrections (template 9).** A label is matched to its transportation line. The correction line billed on top is an open exception. Only when the correction equals the quoted price, so either line alone balances, is the whole group suggested.
  - **Duplicate payables (template 6).** A payout whose batch holds a double-posted payable is a suggestion, because the original and the duplicate are identical and the batch balances two ways. The duplicate's own group stays open with the break.
  - The self-consistency checks gained an outcome-aware shape for the open correction line.
- **Data-source fix.** The default carrier data source mapped the entity as two letters of the account number (`EU`, not `EU01`), so no invoice line belonged to any match set. It now maps carrier accounts to entities through a fixed table, and an unknown account is rejected by normalization and shows in the control totals. The generator reads the same table. The every-row mapping test now also checks that every mapped entity exists.
- **Invariants.** A property test runs random match sets (keyed and keyless rules, all three kinds, every implemented policy, limits, suggestions and supersession) over random items in two entities: 200 examples in `make test-reference` and 10,000 in the gate. It checks four things:
  - an item holds at most one active group or one pending suggestion;
  - every group, re-checked from scratch, satisfies the rule that made it;
  - no group in a matching set mixes entities;
  - shuffled input gives the same decisions.
- **A limit the template 6 expectation relies on.** A payout whose batch holds a duplicate is expected as a suggestion only while the batch has no more than `subset_max_candidates` (24) payables. A bigger batch would be flagged `too_many_candidates` and left open. That is still never an automatic match, but it is a different expected outcome. Every gate seed holds this. A profile with much larger seller batches would need the expectation, or the template's limit, revisited.
- **What the Spark engine must satisfy.** It must reproduce these semantics exactly, including flags, supersession and the order-independence test, and it can reuse `subset.solve` and the fuzzy functions as UDFs.
