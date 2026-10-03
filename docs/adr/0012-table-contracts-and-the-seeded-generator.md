# ADR-0012: table contracts, migrations and the seeded marketplace generator

Status: proposed with Stage 2b. Date: 2026-09-28. Implements roadmap engineering rules 1 and 8, and the Stage 2 generator.

## Context

Stage 2b freezes the data contracts the rest of the platform builds on:
- the landing shapes the generator writes;
- the canonical item fields data sources fill;
- the matching and ledger output tables.

It also builds the synthetic marketplace that Stages 3 to 12 are tested on. Four facts shaped the design:
1. Delta tables need an applied, auditable schema history (roadmap rule 8).
2. The answer key has to name source rows exactly as Stage 3's normalizer will.
3. `medium` (about 1.2 million landing rows) has to run as a job in the local jobs container.
4. Determinism has to hold across seeds, reruns and parallelism.

## Decision

**Table contracts.** `close_contracts.table_schemas` describes all 45 tables: columns, types, nullability, partitions, append-only and, for append-only reference data, a natural key. The `V###__*.sql` files in the engine wheel are the history. They name tables only through `{schema__table}` placeholders, rendered by the resolver through `run_ddl`. `ddl_apply` records a SHA-256 per file and refuses to run if an applied file changed. A Spark test applies all migrations to an empty lake and compares every table with the registry, and a JSON snapshot of the registry is checked for drift. `V001` reproduces the three Stage 1 ops tables exactly, which a test holds equal to the smoke job's and the fence's own DDL.

**Default data sources.** `close_contracts.default_sources` gives every landing table an identity key, and gives 13 data sources DSL mappings to the canonical item fields. The item fields are core fields plus generic typed slots, like BlackLine's configurable columns. `close_contracts.binding.bind` typechecks a source against the landing schema and the item field types, and requires entity, currency, amount and transaction date. A test evaluates every default mapping with the DSL interpreter on every generated row: no row is rejected, and the mapped amounts equal the control totals. Row identity is RFC 8785 JSON of the identity key values, the same form the answer key uses.

**The generator.** The `close-datagen` package is a job registered as `close-job datagen`, in the bundle and in the Jobs API emulator.
- **Randomness.** Every random value comes from a Python stream seeded by SHA-256 of the seed and a stable key, one stream per entity and day or per object. The helpers use only `random()`, whose sequence Python keeps stable.
- **Break injection.** Breaks are chosen by exact quota: the eligible rows are ranked by a seeded hash and the first `round(rate x eligible)` are taken. The four breaks that change a capture are mutually exclusive.
- **Duplicates.** A duplicate is a double-posted seller payable under a new identity. The platform deduplicates by identity, so only a new identity makes a duplicate visible.
- **Streaming.** Tables are written and released as soon as nothing later reads them, and the answer key is kept as compact tuples and streamed out in chunks. `medium`'s Python memory fell from 1.4 GiB to about 0.54 GiB this way.
- **Control totals.** The generator computes them in Python, and a separate Spark implementation reads them back from the lake. The job fails if they differ.

**The answer key.** It covers templates 1, 2, 3, 6, 9 and 10, the Stage 3 set. Each group has members by side, an expected outcome and the break it carries:
- `auto` means an exact keyed match that balances.
- `suggested` covers the rounding difference (template 1), the late bank credit (template 3) and the carrier correction (template 9).
- `open` covers a returned or unclaimed payout and a double-posted payable (template 6).
- Partial captures, split tender and FX differences still match `auto` in template 1. The capture sum equals the card amount in the presentment currency; their effect shows in settlement and in journaling.

The key's self-consistency checks read only the landing rows and the key:
- every member exists;
- every group has its template's shape;
- no row is in two groups of one kind;
- every `auto` group balances by its template's rule;
- every labelled break is in the injected-break log;
- a scan for captures with no reference that share currency, amount and date.

What we did not choose:
- **Generating in Spark**, with seeded hashes as column expressions. The lifecycle logic reads far more clearly in Python, `medium` generates in about a minute on one core, and determinism does not depend on where the Python runs.
- **pandas for the hand-off to Spark.** Nullable integers become floats there.
- **A random draw per row for breaks.** At 25 expected occurrences, that misses the 10 percent band often.

## Consequences

- Stage 3 reads these landing tables through the default data sources and scores itself against this answer key.
- A change to any table or default source shows up as snapshot drift and needs a new migration.
- Answer keys for templates 4, 5, 7, 8, 11 and 12 come with the stages that build those templates. The breaks they need (chargebacks, late wins, clawbacks, unclaimed payouts, fee invoices) are already in the data.
- The jobs container limit rose from 2,560 to 3,072 MiB, because `medium` peaks at about 2.3 GiB there. The roadmap's roughly 4 GB matching pod in Stage 3 will need the memory budget revisited.
- The gate checks that the policy sheet is complete and that the user's confirmation of sending it is recorded (`docs/domain/policy-sheet-status.md`).
- Amended by [ADR-0013](0013-reference-matcher-semantics.md) in Stage 3:
  - the template 6 and template 9 answer-key expectations for duplicate payables and carrier corrections;
  - the default carrier data source's entity mapping, which is now a fixed account-to-entity table.
