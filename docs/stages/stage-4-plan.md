# Stage 4 plan: the governance kernel

The roadmap specifies Stage 4 in [ROADMAP.md](../plan/ROADMAP.md) (Stage 4, the Backend API conventions, and engineering rule 2). You chose to start it before Stage 3 is finished, because it doesn't depend on the matcher (2026-09-28). This note re-specifies it for the platform as it is now: a Databricks App behind the Apps proxy (ADR-0006), with the control plane from Stage 1a. It also records the decisions the roadmap leaves open. There is one gate, `make gate-4`, and no new third-party package or container.

## What changes from the roadmap's list

| Roadmap item | Stage 4 |
|---|---|
| Auth: a dev JWT issuer and a Keycloak `+auth` profile | **Superseded by ADR-0006.** Identity comes from the Databricks Apps proxy headers, which `devproxy` stands in for locally (built in 1a). How Databricks service principals appear to the app can't be verified locally, so machine clients wait for Stage 10. D12 is amended in the changelog. |
| Roles, entity scopes and SoD policies | **Built here** (below). |
| The hash-chained audit log, with a verifier CLI | **Built here** (below). |
| The transactional outbox and relay | **Already built in 1a**, and proved exactly-once under relay crashes by the 1a suite, which runs as regression. |
| Maker-checker config versioning | **Built here**, for data sources and match sets, under the roadmap's resource paths. Stage 5 adds the deeper validation and `:simulate`. |
| Idempotency keys, ETags and problem+json errors | Idempotency keys and problem+json exist from 1a. They are **generalized here** to every POST, together with `ETag` and `If-Match`. |
| A registry of query templates that rejects any template without `:entity_ids` | **Superseded by ADR-0009.** Reads are typed query specs, and the query engine always adds the caller's entity filter. Its equivalent here is a route-inventory test proving every read route goes through the entity-filtered path or checks entity visibility itself. |

## Authorization

- **Roles.** The roadmap's catalog:
  - `platform_admin`, `config_author`, `config_approver`;
  - `match_preparer`, `match_approver`;
  - `journal_preparer`, `journal_approver_l1` to `journal_approver_l3`;
  - `controller`, `auditor`, `integration_client`.
- **Assignments** live in Postgres: a principal, a role, and a scope.
  - **Scope.** The scope is either one entity or `GLOBAL`. `GLOBAL` covers global objects (data sources, access administration, audit events that carry no entity). It never grants data visibility, so an administrator sees no entity's data without an entity assignment.
  - **Visibility.** A principal's visible entities are the entities of their active assignments. `principal_entities` becomes a view over the assignments, so there is one source of truth.
- **Permissions** are policy as code: a fixed map from role to actions, versioned with the app. The route inventory ties every `/api/v1` route to an action, or to an explicit public or authenticated-read declaration. A route with neither fails a test (deny by default).
- **Entity rules:**
  - An entity action needs the role in the object's entity.
  - A match set spanning several entities needs the role in **all** of them to be changed, and in **any one** of them to be read (amended at the sign-off).
  - A data source has no entity, so authoring or approving one needs the role at `GLOBAL` scope.
  - `audit.read` at `GLOBAL` shows every audit event, and in an entity the events about that entity (amended at the sign-off).
- **Deny responses.** An object in no visible entity is a 404, so its existence isn't revealed. A visible object without the permission is a 403.
- **Authenticated reads** keep their Stage 1 behaviour: `whoami` and the smoke result reads need only a signed-in principal, and return only visible entities. A user with no grants still gets 0 rows (1a criterion), not a 403.

## Separation of duties (object level)

Blocked in Stage 4:
1. Approving a configuration version you authored. The authors are the creator and every editor of the version.
2. Activating a configuration version you authored. The approver may activate.
3. Granting a role to yourself.

The roadmap's other SoD blocks use the same mechanism when their objects exist: approving your own manual match (Stage 5), and preparing and posting the same journal (Stage 7). A blocked action is a 409 problem of type `sod-violation`, and it is audited.

## Audit chain

- **Storage.** An append-only table, `audit_events`, plus a single head row that holds `(last_seq, last_hash)`. The app role may insert and select events and update only the head. It can never update, delete or truncate an event.
- **Appending.**
  - Every append locks the head as the **last** lock of its transaction, after any idempotency, lease, run or outbox rows. It then numbers the event `last_seq + 1`, so positions are contiguous even when other transactions roll back.
  - The event is stamped with `clock_timestamp()` after the lock and in one UTC format. The verifier checks positions and hashes, never time order.
- **Hashing.** The hash is SHA-256 of the previous hash and the RFC 8785 JSON of the event as Postgres returns it. Payloads are limited to what round-trips exactly through `jsonb`: strings without NUL, JSON-safe integers, booleans, null, lists and objects.
- **What is audited.**
  - Every state change a user makes: configuration versions (create, edit, submit, approve, reject, activate, retire), role grants and revocations, and smoke-run starts. Each is recorded in the transaction that makes the change.
  - Denied attempts at a privileged action (403 and SoD) are also recorded, in their own transaction, because the request itself rolls back.
- **Verifier.** `python -m close_app.audit verify` walks the chain and reports the first broken position. It is tested against edits, deletions, reordering and a truncated tail, made as the migrator role (the realistic threat).
- **Residual risk.** A database owner who rewrites the tail and recomputes every hash is not detected. It is pinned by a strict xfail test until an external checkpoint of the chain head exists (Stage 11).

## Configuration versioning (maker-checker)

- **Storage.** A `config_versions` table keyed by (kind, config id, version) holds:
  - the status, from ADR-0011's `ConfigStatus`, and the effective dates;
  - the specification as `jsonb` and its RFC 8785 content hash;
  - the entities, and the authors;
  - who submitted, approved, activated and retired it, and when;
  - a `row_version`, which the `ETag` is built from.
- **Transitions.** `draft → submitted → approved → active → retired`, plus reject, which returns `submitted` to `draft`.
- **Editing.** Only a draft can be edited, with `If-Match`.
- **Activation.** A config has at most one active version. Activating a new one retires the old, whose `effective_to` becomes the new version's `effective_from`.
- **Validation** runs through `close_contracts` only, because the app doesn't ship the engine:
  - the pydantic models;
  - `bind()` for data sources;
  - a typecheck of every match-set expression against the item fields, which moves into `close_contracts` so the engine and the app share it.
- **References between configurations.** A match set can be submitted only when every data source it names has a version, and activated only when each has an active one.
- **API**, under the roadmap's paths:
  - `/api/v1/data-sources` and `/api/v1/match-sets`;
  - `…/{id}/versions` and `…/{id}/versions/{v}`;
  - `…/{id}/versions/{v}:submit|approve|reject|activate|retire`.

## HTTP conventions

- **`Idempotency-Key`** is required on every POST. It is scoped to the concrete path (ids included) and the principal, and it is checked **before** `If-Match`, so a replay after a later change still returns the stored response. The route inventory checks every POST requires it.
- **`ETag`** is returned on every versioned resource and changes on any transition. `If-Match` is required to edit or transition a version: 428 when it is missing, 412 when it is stale.
- **Problem types.** Errors carry stable problem `type`s: `forbidden`, `not-found`, `sod-violation`, `precondition-failed`, `precondition-required`, `validation-failed` and `conflict`.

## Gate 4 (pass/fail)

1. **Authorization matrix.** Every role × every route × {an entity in scope, an entity out of scope, `GLOBAL`} has 0 unexpected allows and 0 unexpected denies. The expectations come from an independent hand-written fixture, not from the policy the code enforces.
2. **Route inventory.** Every route is declared public, authenticated-read or an action. Every POST requires `Idempotency-Key`, and every read of entity data is entity-filtered.
3. **SoD.** Every Stage 4 scenario is blocked and audited.
4. **Audit.**
   - Every state change writes its audit events in its own transaction: one per change, and two when an activation retires the active version. A failure after the change leaves neither.
   - Concurrent appends leave no forks and no gaps.
   - Every tamper (edit, delete, reorder, truncated tail) is detected, and the recomputed-chain residual is a strict xfail.
5. **Idempotency and ETags.**
   - Replays of every POST return byte-identical responses, and concurrent duplicates have one effect.
   - A replay after a later change returns the stored response.
   - A stale `If-Match` gets 412, and a missing one 428.
6. **Maker-checker end to end.** A data source and a match set go from draft to active through the API, with every transition audited, and the stack's audit chain verifies.
7. **Regression.** Every Stage 3 reference-slice step, including every 2b, 2a, 1b and 1a check.
8. **ADR-0014** is written, and the roadmap changelog amends D12.

## Build order

1. Migration `0002`:
   - role assignments, with the existing entity grants moved in and deny-by-default kept;
   - `principal_entities` as a view;
   - the audit tables and their grants;
   - `config_versions`;
   - a real downgrade.
2. The audit chain and its verifier.
3. The policy, the authorization dependency and the route inventory, with the dev seed and the existing routes brought under it.
4. The configuration-versioning service and its routes.
5. Idempotency on every POST, and `ETag` and `If-Match`.
6. The matrix, SoD, audit and tamper suites.
7. `make gate-4`, the summary, the gate run and the report.
