## What Stage 4 delivers

Stage 4 is the governance kernel that every business API will sit behind. You chose to build it before Stage 3 is finished. It is re-specified for the Databricks App platform in [stage-4-plan.md](stage-4-plan.md), and the decisions are in [ADR-0014](../adr/0014-governance-kernel.md).

- **Authorization.**
  - The roadmap's 12 roles, assigned per entity or at `GLOBAL`, and permissions as code.
  - A principal sees an entity's data only through an assignment in that entity. `GLOBAL` covers data sources, access administration and the whole audit trail, and never grants data visibility.
  - Changing a multi-entity object needs the role in all of its entities, and reading it needs a role in any one of them. An invisible object is a 404; a visible one without the permission is a 403.
  - Every operation declares its access in the OpenAPI document, and an undeclared operation fails a test.
- **Separation of duties.** These are blocked and audited:
  - approving your own configuration change;
  - activating your own rule;
  - approving a version you edited;
  - granting yourself a role.

  The configuration table's constraints repeat the author rules, as a second guard behind the app.
- **The audit chain.**
  - An append-only, hash-chained log in Postgres.
  - Every change a user makes is written in the same transaction as the change, and every refused privileged attempt in its own.
  - `python -m close_app.audit verify` re-derives every hash from what Postgres returns.
- **Configuration versions.**
  - Data sources and match sets go through draft, submitted, approved, active and retired (and reject), under the roadmap's paths.
  - They are validated through `close_contracts`, including a typecheck of every match-set expression, which moved there so the app and the engine share it.
  - A match set needs its data sources to exist before submission and to be active before activation.
- **HTTP conventions.**
  - Every POST takes an `Idempotency-Key`, scoped to its concrete path and looked up before `If-Match` is checked.
  - Versioned resources carry an `ETag`: 428 without `If-Match`, 412 when stale.
  - Errors have stable problem types.

## Found and fixed while building and testing

1. **The smoke-run cancel endpoint** (Stage 1a) let any signed-in user cancel any run. It now needs `smoke.run`, takes an `Idempotency-Key`, and is audited.
2. **The route inventory passed without checking anything at first.** This FastAPI version keeps included routers as objects instead of flattening their routes into `app.routes`, so the check saw only the four documentation routes. It now reads the published OpenAPI document, and that version found the cancel gap above.
3. **The authorization matrix was proven able to fail.** With the auditor role given author rights for one run, the matrix reported the unexpected allows. With the policy restored, it passes.
4. **A test expectation was wrong.** Through the stack, alice's attempt to approve her own match set is refused with a 403 (she holds no approver role), which comes before the SoD check's 409. The test now expects the 403; the SoD scenarios themselves are proven in the app suite.

## Gate runs

The first full gate run passed 57 of 60 criteria.
- **Two failures were mistakes in my own criteria.** The route-inventory file has five tests, not the six I required. The audit-chain criterion also counted the intended strict xfail as a failure.
- **One failure was a real regression in a Stage 1a test.** The readiness test hard-coded schema head `0001`. Its cleanup then wrote `0001` back into the stack's `alembic_version` while the schema was at `0002`, which would have made the next `make up` try to re-apply `0002`. I repaired the stack's marker. The test now records the head it finds and restores exactly that.

The second run passed 60 of 60. A review of what the gate actually proved then found two promises from the plan with no test behind them:
- **Audit writes.** "Every state change writes an audit event" was only half checked. The replay test proved a replay writes nothing, but not that the first call writes its event. The wording "exactly one" was also wrong, because an activation that retires the active version writes two.
- **The every-entity rule.** Nothing tested it: every test used a US01-only match set, although 8 of the 12 template match sets span several entities.

Both now have tests and their own criteria (62 in all), and the third run passed. You then decided two rules at the sign-off:
- reading a multi-entity match set needs a role in any one of its entities;
- a `GLOBAL` auditor sees every audit event.

They are implemented and tested. The fourth run passed 61 of 62. The Mac slept during its end-to-end suite: the step took 13 minutes of test time but finished almost six hours later, right after a wake in the power log. The two scheduler tests that measure wall-clock cadence then counted too few ticks (99 of at least 100; every earlier run counted 108 to 110), while their exactly-once checks held. Every gate target now keeps the Mac awake with macOS's built-in `caffeinate`. This report is the fifth run.

## Where the build differs from the plan

1. **Identity.** There is no dev JWT issuer and no Keycloak profile. Identity comes from the Databricks Apps proxy (ADR-0006), and D12 is amended in the roadmap changelog. Machine clients (`/api-clients`) wait for Stage 10, where service-principal identity under Apps can be checked on a real workspace.
2. **The query-template registry is superseded** by typed query specs (ADR-0009). A route-inventory test reviews every authenticated read in its place.
3. **The outbox and relay** were built and gated in Stage 1a. They are covered here by regression.
4. **Configuration versioning** covers data sources and match sets. The accounting policy follows in Stage 6; the table already accepts that kind.
5. **The remaining SoD scenarios**, approving your own manual match (Stage 5) and preparing and posting the same journal (Stage 7), use the same mechanism when those objects exist.
6. **A known limit of the audit chain.** A database owner who rewrites the tail and recomputes every hash is not detected. A strict xfail test pins it until Stage 11 adds an external checkpoint of the chain head.

## Needs your decision

1. **Please confirm the amendment to D12 and the two superseded items** (the JWT issuer and Keycloak, and the query-template registry), as recorded in ADR-0014.
2. **Still open from earlier stages:**
   - the Spark rule compiler, for the rest of Stage 3;
   - the policy sheet, before Stage 6;
   - the Spark Connect restart defect, before Stage 10.

## Carried forward

- **Stage 5** builds its matching APIs on this kernel. It adds deeper configuration validation and `:simulate` before the v1 API freeze, and the manual-match SoD scenario.
- **The dev users' roles** are local only:
  - alice authors configurations;
  - dave approves them and administers access;
  - bob audits EU01;
  - carol has none.

  Each still sees exactly the entities they saw in Stage 1.
