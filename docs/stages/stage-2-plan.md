# Stage 2 plan: domain, contracts and synthetic marketplace data

The roadmap already specifies Stage 2 in detail ([ROADMAP.md](../plan/ROADMAP.md), Stage 2), so this note covers only three things, which are how I would split the stage into two gated parts, what ADR-0006 changes in it, and how I read the gate items that need interpretation. Everything else is built as the roadmap says, and no new third-party package is needed (Pydantic and Hypothesis are already in the workspace).

## Two parts, each with its own gate

Stage 2 is as big as Stage 1 was, so I suggest the same shape, 2a and 2b, each with its own gate, report and sign-off. The boundary sits where a contract meets its first real consumer. The configuration models and the rule DSL have no data behind them and can be frozen on their own, but the landing schemas should be frozen at the same gate as the generator that writes them, because a schema frozen before anything writes it tends to need a second version right away.

Stage 2a (domain, configuration and the rule DSL) builds these:

1. The Spark Connect memory fix you chose as the first task, measured before and after under the read benchmark.
2. The domain spec in `docs/domain/`, covering the order lifecycle and money flows per program, the glossary, and the BlackLine concept map (public facts from our research only, with anything tenant-specific such as pass-rule conditions, tolerances and status values marked "confirm from your tenant").
3. The accounting-policy sheet for your controller.
4. The configuration envelope (draft, submitted, approved and active, effective dates, author and approver, RFC 8785 content hash) and the configuration models for data sources, match sets and pass rules, with JSON Schema snapshots.
5. The rule DSL, which is a typed JSON expression tree with its function catalog, typecheck, IR, a pure-Python interpreter and a regex validator for the RE2 subset (a small whitelist parser that rejects nested quantifiers, which Stage 3's ReDoS corpus builds on).

Gate 2a checks that the JSON Schema snapshots show 0 drift, that 1,000 generated examples per model round-trip exactly, that the typecheck accepts and rejects a fixed corpus with structured errors, that the regex validator rejects a corpus of unsafe patterns, and that the Stage 0, 1a and 1b gates still pass as regression.

Stage 2b (schemas, migrations and the generator) builds these:

1. The expected landing schemas, each with a default data-source mapping written in the DSL, so your real tables plug in through their own mapping.
2. The canonical item and accounting-event schemas, and the matching and ledger output schemas.
3. The Delta DDL as `V###` migrations and the `ddl_apply` job, which records a checksum per migration and fails if an applied file has changed.
4. The seeded generator (programs, entities in USD, EUR and GBP, sellers with fee tiers, the order lifecycle, Stripe-shaped and Adyen-shaped PSP data, payout providers, bank rows, carrier lines, tax documents and the stored-value ledger), with every break type, the answer key and the `tiny`, `small` and `medium` profiles.

Gate 2b holds every remaining roadmap criterion (below), plus the regression.

## What ADR-0006 changes in Stage 2

The `ddl_apply` and generator jobs follow D3 on the new platform, so each one is a `main(argv)` entrypoint with a thin notebook and an entry in the job bundle, and locally it runs through the Jobs API emulator the way Lakeflow will run it. "An empty catalog" means an empty lake root locally, since tables are addressed by path there, and the Unity Catalog form is already covered by the catalog spike (ADR-0010). `V001` takes over the three ops tables that the smoke job creates today, with identical definitions held equal by a test, so the Stage 1 flows keep working unchanged.

## How I read the gate items that need interpretation

1. Break rates are injected by exact quota rather than by a coin flip per row. The generator picks exactly `round(rate x eligible rows)` rows, ordered by a hash of the seed and the row's key, so "within 10 percent of the configured rate" holds by construction, and the `small` rates are set so every break type appears at least 25 times. A coin flip per row would miss the 10 percent band often at that sample size.
2. Determinism across seeds, runs and `local[1]` versus `local[4]` comes from making every random draw a pure function of the seed and a stable key, so no draw depends on partition order. The generator also follows the banned-API lint (it is added to the lint's paths), because `large` has to run on Databricks.
3. The answer key's self-consistency check also scans the generated data for collisions nobody intended, such as two captures with the same amount and date and no reference, so genuinely ambiguous cases are labelled `suggested` or `open` from the data and not only from the injection log.
4. The timing criteria (`small` in 3 minutes or less, `medium` in 25 or less) are measured the way Stage 3 will use the data, as a job through the stack writing to the lake. I start 2b with a spike of one flow at `medium` scale, measuring throughput, driver memory and SeaweedFS memory, and if the jobs container cannot make it in time, I come back to you with the numbers before building further.
5. "The policy sheet has been sent" depends on you, since you send it to your controller. The gate checks that the sheet exists and that your confirmation is recorded, and as the roadmap says, the controller's sign-off gates Stage 6, not Stage 3.

## What I need from you

Your approval of this split and these readings. After 2a, the policy sheet goes to your controller through you, which is the only other thing Stage 2 needs from your side.
