# Stage 3 plan: the matching engine

The roadmap specifies Stage 3 in detail ([ROADMAP.md](../plan/ROADMAP.md), Stage 3). This note records how it is split into gated parts and the design decisions that the roadmap leaves open. You asked for Stage 3 to be built autonomously, so the split is decided here rather than put to you. Each part ends with its own gate and a report to you.

## Three parts

**3a: compile and normalize.**
- The Spark compiler for the rule DSL: IR to Spark Columns, never SQL text.
- A differential test holding it to the Python interpreter on 5,000 random rule and row cases.
- Fuzzing: 10,000 invalid rules rejected with structured errors, and the ReDoS corpus.
- The normalize job: data sources bound to landing tables, item versions and corrections, incremental reads, and the completeness control totals (read = inserted + unchanged + corrected + excluded + rejected, by count and signed amount).
- The commit and manifest plumbing that every later job uses.

**3b: match.**
- Pass rules on Spark: blocking, candidate limits, the three ambiguity policies and assignment.
- The match kinds: 1:1, 1:M and M:1 with the bounded subset search, M:M, self_net and chain.
- Timing rules, the waterfall and the item state machine.
- The pure-Python reference matcher over the same IR.
- Templates 1, 2, 3, 6, 9 and 10 as configurations with golden tests.
- The correctness gate: engine equals reference, 0 false automatic matches, the recall targets, and every injected break reaching its expected outcome.
- Determinism across partitions, masters and input order.
- The `medium` performance run and its memory measurement.

**3c: commands and crash safety.**
- The `apply_commands` job: manual match, unmatch, confirm and reject.
- Adjustment requests (create and cancel).
- Random command sequences interleaved with runs.
- Killing jobs at eight or more points in the commit order, ending with the clean-run digest.

## Progress (2026-09-28)

- **The blocker.** The Spark rule compiler (3a) could not be written in this session.
- **What was built instead.** The reference-matcher slice of 3b was built first: the pure-Python reference matcher, templates 1, 2, 3, 6, 9 and 10 as match sets, and their golden tests. Its semantics are in [ADR-0013](../adr/0013-reference-matcher-semantics.md), and its gate is `make gate-3-ref` ([stage-3-ref-report.md](stage-3-ref-report.md)).
- **What still needs the compiler:**
  - the normalize job;
  - the Spark matcher, held to the reference;
  - 3c (commands and crash safety).

## Decisions the roadmap leaves open

1. **Row errors.** In ANSI mode a bad value fails the whole Spark job, but the interpreter rejects one row. So each compiled expression carries an error lane next to its value: `try_` arithmetic and casts, plus shape checks before parsing. A mapping that cannot evaluate a row counts it as rejected in the control totals instead of stopping the run.
2. **Business days in Spark.** `business_days_between` looks up each date's business-day index in a map literal built from the calendar for the data's date range. It stays a pure Column expression, with no join and no SQL.
3. **No SQL strings in the compiler.** This is enforced by the banned-API lint, which gains the matching package, and by a test.
4. **Memory for `medium`.** The roadmap sized the performance run as one 4-core, 4 GB pod. Locally the jobs container is 3,072 MiB with `local[2]`, so 3b measures templates 2 and 3 on `medium` there first. If they don't fit in 15 minutes and 85% of the limit, I'll resize the container and bring the numbers to you in the 3b report, with the memory budget adjusted if needed.
