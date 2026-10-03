## What Stage 2a delivers

Stage 2a is the first half of Stage 2 as split in [stage-2-plan.md](stage-2-plan.md): the domain spec, the configuration contracts and the rule DSL, plus the Spark Connect memory fix you chose as the first task.

- **Spark Connect memory (first task).** Four variants were measured under the full read benchmark: 100k and 1M rows, and four concurrent clients. Each ran on a fresh container, with a baseline control at the end to rule out order effects. Peak process memory:

  | Variant | Peak |
  |---|---|
  | Baseline, first run | 1,862 MiB |
  | Baseline, control at the end | 1,842 MiB |
  | `MALLOC_ARENA_MAX=2` | 1,697 MiB |
  | `-XX:ActiveProcessorCount=2` | 1,704 MiB |
  | Both | 1,651 MiB |

  Both settings are now applied to the `spark-connect` service only. The four-client p95 moved from about 530–600 ms to about 610 ms, still far inside the 1.5 s budget. The gate re-measures this against a new criterion: the process peak must be at most 85% of the container limit.
- **Domain spec** in `docs/domain/`:
  - [marketplace-lifecycle-and-money-flows.md](../domain/marketplace-lifecycle-and-money-flows.md): the programs, order lifecycle, the nine money flows, which template reconciles each, and the break types;
  - [glossary.md](../domain/glossary.md);
  - [blackline-concept-map.md](../domain/blackline-concept-map.md): public facts only, with tenant-specific items marked;
  - [accounting-policy-sheet.md](../domain/accounting-policy-sheet.md): for your controller to fill in and sign.
- **The rule DSL** ([ADR-0011](../adr/0011-rule-dsl-and-configuration-contracts.md)):
  - a typed JSON expression tree and a 46-function catalog;
  - a typecheck that reports every problem with a stable code and a path;
  - the IR with a canonical fingerprint;
  - a pure-Python interpreter that follows Spark's ANSI semantics;
  - a portable regex subset that rejects catastrophic and non-portable patterns.

  A Spark test confirms the interpreter's Spark behaviours and runs every accepted regex against Java on inputs chosen for the known differences.
- **Configuration contracts:**
  - the envelope: status, effective dates, maker-checker and the RFC 8785 content hash;
  - data sources;
  - match sets, with pass rules, typed conditions, limits and timing rules;
  - the accounting policy.

  Each model is frozen as a JSON Schema snapshot with a drift check.

## Found while building and testing

1. **A defect that is not fixed.** The Spark Connect memory experiment restarts the Spark Connect server, which showed that the app never recovers when that server restarts:
   - The Connect client closes its own session, and every later read fails with `NO_ACTIVE_SESSION`.
   - `/readyz` stays at 503 until the app itself is restarted.
   - The existing retry does not recognise those errors.
   - When the server is down, the client's default retries also hold a request for about ten minutes.

   The fix is small: drop a closed session and rebuild it, bound the client's retries to seconds, and return 503 instead of 500. My attempts to write it in `libs/platform/src/close_platform/query/connect.py` were repeatedly stopped by a safety classifier, which left that file incomplete, and restoring it needs your command (below). Restarting the app recovers the read path in the meantime.
2. **JSON-safe integers.** The 1,000-example round-trip property test found that a DSL integer literal above 2^53 made a configuration impossible to hash under RFC 8785. Literals are now limited to the JSON-safe range, and runtime arithmetic keeps the full BIGINT range.
3. **A lax boolean.** Pydantic's default lax mode turned the JSON literal `1` into `True`. Literal values are now strict.

## Where the build differs from the plan

1. The memory fix uses two JVM and allocator settings rather than a larger container limit, because they were measured to work and keep the stack's total budget unchanged.
2. The regex subset is stricter than RE2. It rejects `$` and requires escaping `]`, `}` and a literal `-` in a class, because Java and Python read those differently.
3. `close-contracts` now depends on Pydantic, its first dependency. Pydantic is already locked in the workspace at 2.10.6, the version Databricks Runtime 18 ships.

## Needs your decision

`connect.py` was restored to its last working version on your instruction, before the gate ran. The partial version with the new helper functions is kept outside the repository, so nothing was lost.

1. **The Spark Connect restart defect.** Either you or a teammate apply the fix described above, or we carry it as a known issue with the restart workaround until someone can.
2. **The policy sheet.** Please send the accounting-policy sheet to your controller. Their sign-off gates Stage 6, and the Stage 2b gate records that you sent it.

## Carried forward

- Stage 2b builds on these contracts: the landing, canonical and output schemas, the `V###` migrations with `ddl_apply`, and the seeded generator with its answer key and profiles.
- D19 (the GL book of record) stays open until Stage 5.
- **SeaweedFS memory margin (the first task of Stage 2b).** In this gate, SeaweedFS (the local S3 stand-in) peaked at 718 of its 768 MiB. It was 632 MiB in Stage 1b and 428 MiB in Stage 1a, because its memory grows as each gate run adds data to the lake. No process was killed, but Stage 2b writes the `medium` dataset through it. So 2b starts by measuring it during the generator spike and fixing the margin: a Go memory limit (`GOMEMLIMIT`), a larger container limit, or both, whichever the measurement supports.
- The BlackLine concept map found that BlackLine's `journalId` is a UUID string in `scheduleupdatepostingstatus` but an int32 everywhere else in its public specs. Whoever builds the ERP write-back feed (Stage 7) needs to handle both.
- The documentation polish pass (voice and spelling) is deferred to the end of the project, as you asked.
