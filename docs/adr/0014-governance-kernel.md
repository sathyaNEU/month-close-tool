# ADR-0014: the governance kernel

Status: proposed with Stage 4. Date: 2026-09-28. Implements roadmap Stage 4 and engineering rule 2. It amends D12 and replaces two Stage 4 items, as described below.

## Context

The roadmap's Stage 4 was written for a platform with its own identity provider: a dev JWT issuer and Keycloak. It also assumed SQL query templates. ADR-0006 moved the platform to a Databricks App behind the Apps proxy, which authenticates users before a request reaches us. ADR-0009 replaced query templates with typed query specs. Stage 1a already built the outbox, `Idempotency-Key` storage and problem+json errors. Stage 2a built the maker-checker envelope as a contract.

Stage 4 therefore adds what is still missing, before any business API exists:
- authorization;
- separation of duties;
- a tamper-evident audit log;
- the configuration life cycle in Postgres;
- one set of HTTP conventions for every write.

## Decision

**Identity stays with the proxy** (ADR-0006). The principal is `X-Forwarded-User` as in 1a. There is no JWT issuer and no Keycloak. How Databricks service principals appear to the app can't be tested locally, so machine clients (the roadmap's `/api-clients`) wait for Stage 10.

**Authorization.**
- **Roles and scopes.** The roadmap's role catalog, with `journal_approver_l1` to `l3`. Assignments are stored in `role_assignments`, each at an entity or at `GLOBAL`.
- **Visibility.** A principal sees an entity's data only through an assignment in that entity. `principal_entities` is now a view over the assignments, so there is one source of truth. `GLOBAL` authorizes global objects (data sources, access administration) and the whole audit trail, and never grants data visibility.
- **Permissions** are policy as code (`close_app.authz.POLICY`), versioned with the app.
- **Entity objects.** Changing an entity object needs the role in every entity of the object, and reading it needs the read action in any one of them. You decided this at the sign-off, because 8 of the 12 template match sets span several entities. An object in none of the caller's entities is a 404; a visible object without the permission is a 403.
- **Audit events.** `audit.read` at `GLOBAL` shows every event, because checking the chain end to end needs them all (also decided at the sign-off). `audit.read` in an entity shows the events about that entity.
- **Declarations.** Every operation declares its access in the OpenAPI document (`x-close-access`): `public`, `authenticated`, or an action. A test fails for any undeclared operation.
- **Authenticated reads** keep their Stage 1 behaviour: they return only the caller's entities, and a principal without assignments gets empty results, not a 403.
- **The query-template registry is superseded.** Its purpose, that no read escapes the entity filter, is served by typed query specs, whose engine always adds the caller's entity filter (ADR-0009). A test reviews every authenticated read.

**Separation of duties** is checked on the object at the moment of the action.
- A version's authors (creator and every editor) cannot approve or activate it. The table's check constraints repeat the rule, so it holds even against a bug in the app.
- Nobody can grant a role to themselves.
- A blocked attempt is a 409 of type `sod-violation`, and it is audited.
- Approving your own manual match (Stage 5) and preparing and posting the same journal (Stage 7) will use the same mechanism.

**The audit chain.**
- **Storage.** `audit_events` is append-only for the app role, and a single head row holds the last position and hash. Every append locks the head as the transaction's last lock, so positions are contiguous and never fork, and appends never deadlock with leases, runs or the outbox.
- **Hashing.** The hash is SHA-256 over the previous hash and the RFC 8785 JSON of the event, timestamp in one UTC format. Payloads are limited to values that round-trip through `jsonb` exactly, so `python -m close_app.audit verify` re-derives every hash from what Postgres returns.
- **What is recorded.** Every change a user makes is recorded in the same transaction as the change: configuration versions, role grants and revocations, smoke runs and cancellations. A refused privileged attempt is recorded in its own transaction, because the request's transaction rolls back.
- **Residual risk.** A database owner who rewrites the tail and recomputes the chain is not detected. A strict xfail test pins this until an external checkpoint of the head exists (Stage 11, for example writing the head hash to the lake or to object storage with retention).

**Configuration versions.**
- **Storage and life cycle.** `config_versions` holds data sources and match sets through `draft → submitted → approved → active → retired`, and `reject` returns a submitted version to draft.
- **Rules:**
  - At most one version is in progress and at most one is active.
  - Activation retires the previous active version at the new `effective_from`, which must be later.
  - A match set can be submitted only when its data sources have versions, and activated only when they are active.
- **Validation** runs through `close_contracts`, which the app ships. The model shape, `bind()` for data sources, and `close_contracts.config.validate.match_set_issues`, which typechecks every match-set expression and operand type. The engine's reference matcher uses the same item schema.
- **Paths.** The API uses the roadmap's resource paths (`/data-sources`, `/match-sets`, `…/versions/{v}:submit|approve|reject|activate|retire`). Stage 5 adds `:simulate` and deeper validation before the v1 freeze.

**HTTP conventions.**
- **Idempotency.** Every POST requires `Idempotency-Key`, scoped to the principal, the method and the concrete path. The lookup happens before `If-Match` is checked, so a replay after a later change returns the stored response.
- **ETags.** Versioned resources carry an `ETag` built from a row version, so it changes on every transition. An edit or transition needs `If-Match`: 428 without it, 412 when stale.
- **Problem types** are stable URNs (`urn:close:problem:forbidden`, `…:sod-violation`, and so on).

**A 1a gap closed.** The smoke-run cancel endpoint accepted any signed-in user. It now needs `smoke.run`, takes an `Idempotency-Key`, and is audited.

## Consequences

- Migration `0002` moves the Stage 1 entity grants into `match_preparer` assignments, which keeps what those users could do, and its downgrade moves them back. The dev seed gives the four dev users roles without changing what each can see.
- New endpoints must declare their access and pass the route inventory. A new role or permission must be added to the hand-written matrix expectations, or the gate fails. That review step is deliberate.
- D12 is amended in the roadmap changelog: identity comes from the Databricks Apps proxy, not OIDC JWTs, and RBAC, entity scopes and SoD live in the app as planned.
- Amended by [ADR-0015](0015-journaling-configuration-and-reference-engine.md) in Stage 6:
  - a configuration that an active one uses can't be retired, including a data source that an active match set uses (a new version replaces it instead);
  - a match set's references are still checked on submit and activate as before.
