# Marketplace close platform: BlackLine-grade matching and journaling on Databricks, orchestrated from Kubernetes

Status: approved on 2026-09-26. Changes after approval are recorded in the Changelog at the end, and the decisions table marks amended rows, so the approved text stays visible.

> The version facts in this plan were checked against primary sources on 2026-09-26 (Databricks release notes, Apache, PyPI, GitHub, Docker Hub), and Stage 0 checks every pin again before locking it. The draft was also reviewed for correctness at scale, auditability and rework risk, and every must-fix finding from that review is built into the plan below.

## Context

The org runs a GOAT/StockX-style resale marketplace, where the buyer pays the platform, the seller ships to an authentication center, and the platform authenticates the item, ships it to the buyer and then pays the seller net of fees. Today the books are closed in BlackLine, and we are building an in-house, enterprise-grade month-close platform with BlackLine-level **Transaction Matching** and **Journal Entry**, tuned to marketplace money flows (orders, PSP captures and payouts, seller payouts, refunds, chargebacks, shipping labels, sales tax, store credit).

You set these constraints, and they are fixed:
- Source data always lands in **Databricks landing tables**, and the results go back to **Databricks**, so matching and journaling run as **PySpark/SQL jobs** on Databricks.
- A **pure backend application** on Kubernetes orchestrates everything through an API, and there is no UI work yet, but this plan still covers the frontend, CI/CD, infrastructure and automation.
- The org is on **AWS + Databricks**, but local development runs entirely on **Docker + Kubernetes on this Mac**, so no cloud is needed to bring the application up.
- We build **stage by stage**, and each stage is tested thoroughly and signed off by you before the next one starts, so the code does not get reworked again and again.

Your answers were **Databricks latest LTS + Unity Catalog**, **Temporal** and **GitHub Actions**, with the toolchain installed via Homebrew. The one change I made is the Docker VM, which I set to 12 GB rather than 14–16 GB, so the Spark tests that run on the Mac itself still have headroom (see D13).

The machine is an Apple M5 Pro (arm64) with 18 cores and **24 GB RAM**. Docker 29.4 is installed but its daemon is not running, kubectl 1.34 and Terraform 1.14 are present, and there is no JDK, kind, helm or uv. Host Python is 3.14, which is too new for Databricks parity, so the project uses a uv-managed Python 3.12.

For reference, `reports/BlackLine capabilities and API deep dive.md` covers BlackLine's object model, API patterns and gaps, and it also covers the audit standards and the Oracle ARCS semantics that we borrow where BlackLine is not public.

## Stages at a glance

Each stage ends with a test gate, a written report with the real command output in it, and your sign-off, and only then does the next stage start.

| # | Stage | What you get | Headline gate |
|---|---|---|---|
| 0 | Toolchain, scaffold, host parity | Tools installed, monorepo, contracts core, proven feature allowlist for Spark 4.1 / Delta 4.4 | Every allowlisted feature passes 3/3 on MinIO; notebooks run as scripts |
| 1 | Local k8s platform | kind cluster with Postgres, MinIO, Spark Connect, Temporal, API and worker; end-to-end smoke pipeline | Smoke passes 10/10; memory ≤ 9.5 GB; RBAC matrix holds |
| 2 | Domain, contracts, synthetic data | Marketplace domain spec, policy sheet for your controller, frozen schemas and DSL, seeded data generator with answer key | Deterministic seeds; every break type present; 0 schema drift |
| 3 | Matching engine | Spark matching engine plus a reference matcher; templates 1, 2, 3, 6, 9, 10 | 0 false auto-matches; recall ≥ 99% on keyed rules; identical digests across reruns |
| 4 | Governance kernel | Auth, entity scopes, SoD, hash-chained audit, outbox, maker-checker config | 0 unexpected allows; tampering detected |
| 5 | Matching control plane | Matching API v1: config, runs, results, manual commands, exceptions, webhooks; templates 4, 5, 7, 11 | Contract tests pass; no double-matches under concurrency; e2e on kind |
| 6 | Journaling engine | Accounting events, posting rules, FX, suspense, GL rollups in Databricks | Journals equal the independent oracle; accounting invariants hold |
| 7 | Journal workflow | Templates, manual and recurring journals, approvals, batches, reversals, posting | Every state transition and SoD rule tested |
| 8 | Close management | Account reconciliations, trial balance, templates 8 and 12, flux, close checklist | Roll-forward ties |
| 9 | Frontend | Match workbench, journals, approvals, GL explorer | Playwright suite passes; WCAG 2.1 AA |
| 10 | AWS + Databricks, CI/CD | Terraform, GitHub Actions, Argo CD, bundle deploys | Nightly parity suite green on Databricks |
| 11 | Hardening | Pen-test fixes, DR drills, ITGC evidence | Recovery meets RPO 2 h / RTO 24 h |
| 12 | Parallel run and cutover | 2+ closes run alongside BlackLine | Variance report signed off |

## Decisions (defaults; any can be overridden at a stage gate)

| # | Area | Decision |
|---|---|---|
| D1 | Databricks target | **DBR 18 LTS** (Spark 4.1.0, Python 3.12.3, JDK 21, Scala 2.13; supported until Jun 2029) + Unity Catalog. Catalogs `close_dev`, `close_stg`, `close_prod`. |
| D2 | Local data plane | **pyspark 4.1.3 + delta-spark 4.4.0** (`io.delta:delta-spark_4.1_2.13:4.4.0`), with MinIO in place of S3. Code never names tables directly: it goes through a `TableResolver` and `IDENTIFIER(:t)`. The local default is **path-based Delta tables on MinIO**. **Unity Catalog OSS 0.6.0** is an opt-in `+uc` profile that proves three-level naming. Spike S1 confirms this setup. (amended 2026-09-26 and 2026-09-27, see Changelog) |
| D3 | Job code shape | Engine logic lives in the wheel `close_engine`. Each job is a `main(argv)` entrypoint: `python_wheel_task` on Databricks, `python -m` / spark-submit locally. Each job also gets a **thin Databricks notebook** so your team can run and inspect it in the workspace. **SQL files** hold Delta migrations, data checks and read-query templates. Declarative pipelines are out of scope, because they would be a second execution model to test. |
| D4 | Language and libraries | **Python 3.12 everywhere**. pandas 2.2.3, pyarrow 21.0.0 and numpy 2.1.3 match DBR 18. FastAPI, Pydantic v2, SQLAlchemy 2 + Alembic, all in a uv workspace. |
| D5 | Orchestration | **Temporal**: server 1.32, SDK `temporalio` 1.33. Locally it runs as the single-process dev server (SQLite on a volume), which saves about 1 GB. An opt-in `+temporal-pg` profile runs Helm chart 1.7 on Postgres for parity. Production (Temporal Cloud vs EKS + Aurora, including `numHistoryShards`) is an ADR in Stage 10. (amended 2026-09-27, see Changelog) |
| D6 | Systems of record | **Postgres** holds config versions and approvals, periods (authoritative), commands, run status, the exception workflow, the audit hash chain, the outbox and idempotency keys. **Delta** holds items and their versions, matches, adjustment requests, accounting events, journal lines, rollups and run manifests. (amended 2026-09-27, see Changelog) |
| D7 | Compute adapter | A `ComputeBackend` interface with three implementations: a k8s Job locally, the Databricks Jobs API in prod, and in-process for tests. `submit` is idempotent on `(run_id, attempt)`, and `ensure_terminal` runs before any hand-off. (amended 2026-09-27, see Changelog) |
| D8 | Read path | A `QueryEngine` that runs only allowlisted SQL templates with named parameters and `IDENTIFIER(:t_*)`. Every template takes `:entity_ids`, bound from the caller's identity. Locally it runs against Spark Connect (the API uses `pyspark-client`, so no JVM); in prod, against a Databricks SQL warehouse. Tests use Fake and LocalSpark engines. (amended 2026-09-26, see Changelog) |
| D9 | Money | Amounts are stored as **BIGINT minor units** with an ISO-4217 code and a currency-exponent table. FX conversion is `CAST(amt AS DECIMAL(18,0)) × rate DECIMAL(19,12)`, which gives an exact DECIMAL(38,12), then `bround` half-even. Each event gets its own rounding line, and each line records the rate type, date and version. |
| D10 | Rules | A typed JSON DSL is compiled to an IR, then to Spark Columns, **never to SQL strings**. Data-source column mappings use the same expression tree. Regex is limited to an RE2-validated subset. |
| D11 | IDs and time | Engine rows use 128-bit, length-prefixed SHA-256 IDs, with ≥50 test vectors shared between Python and Spark. App entities use UUIDv7 (`uuid-utils`). Everything is stored in UTC, and accounting dates follow each entity's timezone. |
| D12 | Access control | OIDC JWTs: a dev issuer in `core`, Keycloak in the `+auth` profile, your IdP in prod. RBAC, entity scoping and SoD arrive in **Stage 4**, before any matching API is built. (amended 2026-09-27, see Changelog) |
| D13 | Local k8s | **kind v0.33, single node** (Kubernetes v1.36.4 pinned by digest to match EKS), **Helm 4.3**, a local registry and metrics-server. Services are exposed with NodePort + `extraPortMappings`, with no ingress. **Docker Desktop: 12 GB, 8 CPU, 120 GB disk.** kubectl is upgraded via Homebrew (1.34 → 1.36+) to keep version skew within one minor. (amended 2026-09-27, see Changelog) |
| D14 | CI/CD | GitHub Actions (actions pinned by SHA, SBOM, signed images), Argo CD, and Databricks bundle deploy via Databricks CLI v1.18 (bundles were renamed "Declarative Automation Bundles"). `make ci` mirrors CI locally, and the kind smoke test runs nightly on a larger runner. (amended 2026-09-27, see Changelog) |
| D15 | IaC | Terraform with the AWS and Databricks providers. The prod app DB is **Aurora PostgreSQL or Databricks Lakebase** (GA on AWS, PG17), decided by ADR in Stage 10. Locally we run `postgres:17.11`. (amended 2026-09-27, see Changelog) |
| D16 | Frontend | React + TypeScript + Vite, TanStack Query, a data grid, an OpenAPI-generated client and Playwright. |
| D17 | Observability | OpenTelemetry throughout. Locally it's an opt-in `+obs` profile; in prod, your org's stack. |
| D18 | Images | Our own multi-arch images: `close-spark` is Python 3.12, JRE 21, pyspark/delta and prefetched S3A/UC jars. The rest use official images (`postgres:17.11`, MinIO, `unitycatalog/unitycatalog:v0.6.0`, `temporalio/temporal`). **No Bitnami**: its Spark image is discontinued and its Postgres image only ships an unpinned `latest`. |
| D19 | **GL book of record (please confirm by the Stage 5 gate)** | Journals have two states. **`posted`** means committed to the Databricks `ledger` GL tables, your stated destination, and needs no ERP. **`erp_confirmed`** is optional: it is set when a downstream ERP feed writes back a document ID through `POST /posting/status`. D19 decides one thing. **Default: an ERP remains the legal book of record**, so Stage 8 adds a Delta-to-ERP trial-balance tie-out once an ERP extract is available. If instead **Databricks is the GL of record**, Stage 6 adds opening balances, the trial-balance roll-forward and year-end close. |

## Architecture

```
            ┌────────────────── Kubernetes (kind locally, EKS in prod) ──────────────────┐
 clients ──▶│ close-api (FastAPI /api/v1) ──txn──▶ Postgres: config·periods·commands·     │
 (UI later) │     │ reads via QueryEngine           audit chain·outbox·exception queue  │
            │     │                                   │ outbox relay                      │
            │     │                                   ▼                                   │
            │     │                       Temporal ◀── close-worker (workflows+activities)│
            │     │                                   │ submit/poll: ComputeBackend       │
            └─────┼───────────────────────────────────┼───────────────────────────────────┘
                  ▼                                   ▼
   Spark Connect (local) / SQL warehouse (prod)   k8s Spark Job (local) / Databricks Job (prod)
                  └────────────▶ Delta tables: MinIO paths (local) / S3 + Unity Catalog (prod) ◀── landing tables
```

**How Postgres and Delta stay consistent (the outbox pattern):**
1. In one Postgres transaction, the API writes the command or config change, its audit event, and an outbox row.
2. A relay reads the outbox and starts or signals Temporal, passing IDs only.
3. The writer workflow snapshots the config as content-addressed JSON (on MinIO locally, and on a UC Volume with S3 Object Lock in prod), and then it launches the job.
4. The job validates against Delta, commits the tables in a fixed order and writes its **run manifest last**, and readers and downstream jobs only see runs that have a committed manifest.
5. An activity reads the manifest and records each outcome in Postgres (`applied`, or `rejected` with a reason), and a nightly control reconciles the Postgres commands against the `command_id`s recorded in Delta.

**Catalog layout** (the catalog is `close_<env>`, and landing tables are referenced by configured fully-qualified names because another team owns them):

| Schema | Tables | Writer |
|---|---|---|
| `landing` (local only) | orders, order events, PSP balance transactions and payouts, bank statement lines, seller payouts, payout-provider records, carrier invoice lines, tax documents, stored-value ledger | synthetic generator (prod: your ingestion) |
| `ref` | `currencies`, `fx_rates`, `calendars` (with business-day index), `periods` (mirrored with `status_version`), `accounts`, `dimensions`, `account_mappings` | ref-sync job |
| `match` (partitioned by `match_set_id`) | `items`, `item_versions`*, `item_state_log`*, `groups`, `group_items`*, `adjustment_requests`* | that match set's writer only |
| `ledger` | `accounting_events`*, `subledger_lines`*, `journals`, `journal_lines`*, `gl_rollup_lines`, `adjustment_consumption`*, `gl_outbox`, `balances` | the entity's ledger writer only |
| `ops` | `run_manifests`*, `schema_migrations`, `dq_results`, `scratch_*` (per run, disposable) | every job |

\* append-only (`delta.appendOnly=true`)

## Engineering rules (enforced by lint, tests and gates, from Stage 0)

1. **Contracts first.** Canonical schemas, config models (Pydantic → JSON Schema) and the OpenAPI spec are versioned and frozen at each gate, so a breaking change needs a new version.
2. **Config under change control (maker-checker).** Match sets, rules, data-source mappings, journal templates, posting rules and approval matrices go through draft → submitted → approved → active, with effective dates.
   - The author cannot approve their own change.
   - Every change writes an audit event and a content hash (RFC 8785 JSON).
   - Under ITGCs, a rule change counts as a program change.
3. **Single writer per match set.**
   - `MatchSetWriterWorkflow(match_set_id)` is the only writer, and it serializes scheduled runs and manual-command batches.
   - A new compute job starts only after `ensure_terminal` confirms the previous one has finished, and every job has a hard timeout.
   - Every MERGE names the literal `match_set_id` partition.
   - Tables are committed in this order: `item_versions` → `groups` → `group_items` → `adjustment_requests` → `item_state_log` → `items` → manifest.
   - Delta write conflicts are retried a bounded number of times, which is safe because the IDs are deterministic. Locally only one compute job runs at a time, because OSS Delta on S3 is only safe with one driver per table.
   - The ledger follows the same pattern, with one writer per entity.
4. **Determinism contract.** Re-running with the same inputs and config must produce the same output digest.
   - **Pinned session.** Each job asserts and records `session.timeZone=UTC`, `ansi.enabled=true`, `decimalOperations.allowPrecisionLoss=false` and schema auto-merge off.
   - **Total ordering.** Every window and rank goes through a `total_order()` helper that ends with the item IDs.
   - **Work budgets, not time budgets**, are used for any bounded search.
   - **Pinned inputs.** Input table versions are written once per `run_id`.
   - **Order-independent digests.** A per-table `output_digest` is a SHA-256 over sorted row hashes in 256 buckets, and the Delta files themselves never compare byte-for-byte.
   - **Banned APIs.** An AST lint in pre-commit and CI rejects:
     - `monotonically_increasing_id`, `rand`, `uuid()` and `now`/`current_timestamp`
     - `dropDuplicates`, and `first`/`last`/`max_by` without a total order
     - unsorted `collect_list`
     - integer `/` (use `div`) and DOUBLE in any stored or ranked column
     - `input_file_name`
     - `sparkContext`, `.rdd`, `_jvm`, `cache` and `checkpoint` (intermediates are materialized to `ops.scratch_*` tables instead, so the code also runs on Spark Connect, serverless and standard access mode)
5. **Append-only system-of-record tables.**
   - Idempotency means insert-if-absent, and corrections are made by reversal, so nothing is deleted.
   - Replace-by-`run_id` applies to scratch tables only.
   - In prod, only the job service principal can write `match` and `ledger` (people get SELECT), and a detective check compares the `DESCRIBE HISTORY` writers against that principal.
   - Batch digests are anchored in the Postgres audit hash chain.
   - Each manifest records `engine_version`, `git_sha` and `image_digest`, which is the program-change baseline under AS 2201.
6. **Completeness control totals (AS 1105).**
   - Normalization checks `read = inserted + unchanged + corrected + excluded + rejected` per source and currency, both by row count and by signed amount.
   - Journaling checks `events = posted + suspended`.
   - If a check fails, the run does not commit.
7. **Portable Spark and Delta only.** We use only the allowlist proven in Spike S2, and anything Databricks-only sits behind an adapter.
8. **Delta schema migrations.** Numbered `V###__*.sql` files are applied by a `ddl_apply` job and recorded in `ops.schema_migrations`, and schema evolution is never allowed on system-of-record tables.

## Marketplace domain model (configurable, never hard-coded)

These are public facts from the research, which we use as design inputs, but they are not claims about anyone's internal accounting.
- **The money-owed trigger varies by channel, even within one company.** StockX's core marketplace pays after verification, Verified Sellers are paid on the ship date and Listings are paid after carrier-confirmed delivery, and GOAT credits sellers after authentication.
- **Seller fees are layered.** There is a tiered transaction fee by seller level, a processing fee and regional minimums, and penalties are netted from payouts or charged to the seller's card.
- **Refunds often go to store credit or a wallet.** Non-US seller payouts often go through Hyperwallet.
- **What comparable SEC filers do** (RealReal, ThredUp, Etsy, eBay, Poshmark):
  - They report consignment revenue net, as an agent, and owned inventory gross.
  - Sales tax is excluded from revenue as a facilitator pass-through.
  - Incentives are contra-revenue, with any excess going to marketing.
  - Money owed to sellers sits in a payable / customer-funds liability.
  - Chargebacks are covered by a reserve.

**Programs and policy switches.** Each program is a channel (verified marketplace, direct-ship, consignment/stored inventory, or listings), and each one carries its own settings, which the controller signs off in Stage 2:

| Switch | Options |
|---|---|
| Revenue / seller-payable trigger | payment · verification passed · shipped · delivered · return window closed |
| Program model | agent (net commission and fees) · owned inventory (gross + COGS) |
| Shipping | net as agent · gross |
| Incentives | contra-revenue · contra up to cumulative revenue then marketing · marketing |
| Presentation | PSP receivable as cash-equivalent or A/R · processing fees in COGS or SG&A · chargebacks via reserve or a transaction-loss line |

**Accounting event catalog.** Posting rules are configuration.
- `PAYMENT_CAPTURED` and `CAPTURE_ADJUSTED`.
- `VERIFICATION_PASSED` and `VERIFICATION_FAILED`.
- `SHIPPED_TO_BUYER`, `DELIVERY_CONFIRMED` and `RETURN_WINDOW_CLOSED`.
- `ORDER_CANCELLED` and `REFUND_ISSUED` (cash or store credit).
- `INCENTIVE_APPLIED`.
- `SELLER_CHARGE_ASSESSED`.
- `SELLER_PAYOUT_INITIATED` and `SELLER_PAYOUT_RESULT` (paid, failed, returned or unclaimed).
- `PSP_SETTLEMENT_RECEIVED` and `PSP_FEE_BILLED`.
- `DISPUTE_EVENT` (opened, fee, won, lost, late win).
- `CARRIER_CHARGE_BILLED`.
- `TAX_COMMITTED` and `TAX_REMITTED`.
- `STORED_VALUE_EVENT` (issued, redeemed, expired, breakage, escheat).
- `FX_REALIZED` and `FX_REVALUED`.

**Match-set templates.** Keys come from real PSP, bank and carrier formats.

| # | Template | Kind | Keys and tolerances |
|---|---|---|---|
| 1 | Order ↔ PSP capture | 1:N | order ID ↔ Stripe PaymentIntent metadata / Adyen merchant ref / PayPal invoice ID / Braintree order ID; exact amount in presentment currency |
| 2 | PSP transactions ↔ PSP payout | N:1 | Stripe `automatic_payout_id` / Adyen batch number / Braintree transfer ID; Σ net = payout |
| 3 | PSP payout ↔ bank credit | 1:1 | Stripe `trace_id` / Adyen `TX…XT` ↔ BAI2 16-record refs and text, camt.053 `AcctSvcrRef`/`EndToEndId`/`RmtInf`, MT940 `:61:`/`:86:`; value-date window in business days |
| 4 | Refund chain | chain | refund event ↔ PSP refund / `partial_capture_reversal` ↔ seller-payable adjustment or transfer reversal |
| 5 | Dispute lifecycle | chain | dispute ID ↔ debit + fee ↔ win / loss / late win ↔ reserve ↔ seller recovery |
| 6 | Seller payable ↔ payout item | 1:1 / N:1 | Stripe `transfer_group` / Hyperwallet `clientPaymentId` ↔ `pmt-` / PayPal `sender_item_id`; status-aware |
| 7 | Payout-provider funding ↔ bank debit | 1:1 | funding transfer ↔ bank debit, within a date window |
| 8 | Stored-value roll-forward | balance | issued − redeemed − expired − breakage = liability |
| 9 | Shipping label ↔ carrier invoice | 1:N | tracking number + ship date, by leg; quoted vs billed tolerance; weight and address corrections |
| 10 | Tax collected ↔ tax engine ↔ remittance | 1:1 / N:1 | order ↔ AvaTax document code / `totalTax` (committed documents only) |
| 11 | PSP fee accrual ↔ fee invoice | N:1 | Braintree gross fees / Adyen `InvoiceDeduction` / Stripe fees |
| 12 | Clearing-account zero-out | balance | GL PSP clearing ↔ PSP ending balance; seller-payable subledger ↔ GL control |

**Injected break types**, which the engine must surface:
- Timing (payout cycle, value date, month-end).
- Partial captures and partial refunds.
- Split tender and store credit.
- FX differences.
- Fees netted vs invoiced separately.
- Chargebacks and late wins.
- Duplicates.
- Rounding and format (BAI2 has no decimal point, and MT940 uses a decimal comma).
- A refund issued after the seller was paid (clawback).
- Unclaimed or returned payouts.

## Engine design

### Transaction Matching (BlackLine concepts → ours)

| BlackLine | Ours |
|---|---|
| Import Definition + Data Source | `DataSource`: landing binding, typed column mapping (DSL expressions), source identity key, sign convention. Its output is `match.items` plus `item_versions`, with lineage (table, Delta version, key, row hash). |
| Match Set (entity, account, team, preparer, approver; groups) | `MatchSet`: 1..N sources with roles A/B/C, scope filters, owners, schedule and a group hierarchy. Types: `matching`, `intercompany`. |
| Pass Rules (ordered; automatic vs suggested) | `PassRule`: an ordered waterfall. Kind is `1:1`, `1:M`, `M:1`, `M:M`, `self_net` or `chain`, with conditions, tolerances, outcome `auto`/`suggested`, an ambiguity policy and limits. Oracle ARCS semantics fill the gaps where BlackLine isn't public. |
| `MatchType` | Group `match_type`: `automatic`, `suggested`, `manual`, `partial`. Group `status`: `active`, `pending`, `confirmed`, `rejected`, `superseded`, `unmatched`, `broken`. |
| In-Transit Definitions | `timing_rules`, which classify residual items as in-transit with an expected clear date |
| Reason codes → items → adjusting JEs | Exceptions carry reason codes. A write-off rule emits event-sourced `adjustment_requests` (create/cancel), which journaling consumes exactly once. |

**How one pass runs (Spark):**
1. **Pool.** The pool is the open items in the match set, with no age cut-off, and items that have a pending suggestion are eligible for auto rules only.
2. **Blocking and candidate limits.**
   - Keyed rules use an equi-join, date windows explode into business-day offsets, and keyless amount rules use (currency, day, amount-bucket ±1) blocks.
   - The validator rejects any `M:M` rule without an exact key, and any rule that cannot block.
   - An item with more than `max_candidates_per_item` candidates, or a block with more than `max_block_pairs` pairs, becomes `too_many_candidates` and is never auto-matched.
3. **Conditions** compile to Spark Columns:
   - Text: equals, normalized-equals, `contains` (non-empty needle of at least N characters), starts/ends-with, and RE2-subset regex-extract-equals. Fuzzy uses built-in Levenshtein or a Jaro-Winkler UDF, is quantized to integer basis points, and runs only after blocking.
   - Dates: calendar or business-day windows, which can be asymmetric.
   - Amounts: exact, absolute, percent (bps), or percent with a cap, all in integer arithmetic.
   - Sign: same, opposite or absolute.
   - Currency: equal, or FX-converted with a tolerance.
4. **Ambiguity** is decided once, on the candidate graph, before assignment.
   - `unique_only` is the default for auto rules, and it needs each side to have exactly one candidate, just like Oracle's "No Ambiguous".
   - `best_unique` accepts a pair only when it is strictly the best for both sides.
   - `greedy_total_order` runs iterative mutual-best selection for at most 16 rounds, then a driver-side sort-and-scan if 2M or fewer pairs remain, and otherwise the items get `assignment_limit_reached`. Groups that needed the ID tie-break are flagged `ambiguous` and downgraded to suggested, unless an approved rule sets `allow_ambiguous_auto`.
5. **Kinds.**
   - `1:M` / `M:1` aggregate by group key, and the bounded subset-sum uses meet-in-the-middle, with at most 24 candidates per block and a work budget of 2^20 evaluations. Exactly one solution means auto, two or more means we suggest the lexicographically smallest and flag it ambiguous, and an exhausted budget means `search_limit_reached`.
   - `M:M` means equal totals per exact key.
   - `self_net` means the group sums to zero within tolerance.
   - `chain` runs as staged passes over composite items, with no transitive grouping.
6. **State rules.**
   - An item has at most one active group **or** one pending suggestion, never both.
   - Auto rules supersede suggestions, and confirming a superseded suggestion returns 409.
   - Group IDs include the run or command ID.
   - A **corrected source row** creates a new item version, its active group becomes `broken` (`SOURCE_CORRECTED`), the other items reopen, and the group's adjustment request is cancelled.
   - Deleted rows become `voided`.
   - Deduplication uses source identity, never content, because duplicates are a break we must surface.
7. **Outputs.** Groups, group items, adjustment requests and timing classifications are committed in the fixed order, followed by the manifest (control totals, per-rule metrics, invariants and digests).

### Journaling (BlackLine concepts → ours)

| BlackLine | Ours |
|---|---|
| Journal Template: dynamic header and line columns with `required`, `readOnly`, `defaultValue`, `balanceBy`, `validateField`, `restrictNegativeValues`… | `JournalTemplate` with typed columns carrying the same validation attributes. Journals store `dynamic_data` against the template version. |
| Journal Masters (recurring, `frequencyCode`, `canAutoCertify`) | `RecurringJournal`, run by Temporal Schedules. Auto-certify is allowed only when policy says so. |
| Automated Journal Definitions (type Matching / Items / ICH) | `AutoJournalDefinition`, sourced from match adjustments, exception write-offs or accounting events |
| Journal Batching (atomic) | `JournalBatch`: posts all or none |
| Certification (`certify`, `validate`, `decertify`, `reject`) | `draft → submitted → approved(L1..Ln) → posted` (committed to the Databricks GL tables) `→ erp_confirmed` (optional), plus `rejected`, `recalled` and `reversed`. SoD is enforced, and a risk score based on AS 2401.61 drives routing. |
| `scheduleupdatepostingstatus` | `POST /api/v1/posting/status` (`documentId`, `statusCode`, `statusMessage`, `erpDocUrl`), which sets `erp_confirmed` when an ERP feed exists |

**Subledger engine (Spark):**
1. **Events.** Landing lifecycle rows become `accounting_events`, keyed by `(event_id, event_version)` with amounts in minor units. A correction reverses the old lines and rebooks new ones, and a late event for a closed period goes to the first open period and is flagged `is_prior_period_adjustment` with its `original_period`.
2. **Posting rules** are versioned DSL, effective-dated by accounting date, and they produce lines with a Dr/Cr side, an account (a literal, or derived from `account_mappings` by attributes), an amount expression, dimensions and a description.
3. **Validation.** Each event must balance per currency, accounts must be active and allowed, dimensions must be complete, and the period must be open. Period status is fenced, which means the run pins `ref.periods.status_version` at start and checks it again at commit.
   - **An imbalance caused by configuration fails that event type outright.** Only data problems go to suspense, and they record the intended account.
   - A circuit breaker trips when suspense exceeds 0.5% of volume, and any suspense balance blocks soft close.
4. **FX.** Conversion follows D9, and realized FX is booked on cross-currency matches. Foreign-currency PSP and seller balances are revalued at period end, but translation to reporting currency (CTA) stays in consolidation.
5. **Rollup.** Lines are summarized by entity, day or period, account, dimension set and currency into `gl_rollup_lines`, and a deterministic `subledger_lines.rollup_line_id` gives drill-down from the GL line to the subledger line, the event and the landing row. The rollups then go to `gl_outbox`.
6. **Adjustment consumption.** Journaling appends to `ledger.adjustment_consumption`, so each adjustment request is booked exactly once and every table keeps one writer.

## Backend API (`/api/v1`, OpenAPI 3.1, contract-first)

The API follows these conventions:
- Cursor pagination.
- `Idempotency-Key` on every POST.
- `ETag` / `If-Match` for optimistic concurrency.
- RFC 9457 `problem+json` errors.
- `202` plus an operation resource for long-running work.
- Request IDs, OTel traces and rate limits.
- **Read APIs and webhooks, which BlackLine lacks.**

| Area | Resources |
|---|---|
| Reference | `/entities`, `/periods` (open / soft-close / close / lock), `/calendars`, `/currencies`, `/fx-rates`, `/accounts`, `/dimensions`, `/account-mappings` |
| Matching config (maker-checker) | `/data-sources`, `/match-sets`, `/match-sets/{id}/rules`, `/…/versions/{v}:submit\|approve\|activate`, `/rules:validate`, `/match-sets/{id}:simulate` (dry run, no commit) |
| Matching runs | `POST /match-sets/{id}/runs` → 202 with `run_id`; `GET /runs/{id}` (status, per-rule metrics, match rate, control totals) |
| Matching results | `/match-sets/{id}/items` (filters, subtotals, saved views), `/match-groups/{id}`, `/match-sets/{id}/groups?status=suggested`, `POST /match-groups/{id}:confirm\|reject\|unmatch`, `POST /match-sets/{id}/manual-matches` (with a difference and a reason code) |
| Bulk commands | Select-by-filter: a pinned template + parameters + the expected count and amount total, which the job re-checks before applying |
| Exceptions | `/exceptions` (assign, reason code, comment; served from a Postgres projection), `POST /exceptions/{id}:write-off` → adjustment request |
| Journaling config (maker-checker) | `/journal-templates` (+columns), `/posting-rules`, `/event-types`, `/recurring-journals`, `/auto-journal-definitions`, `/approval-matrices`, `/sod-policies`, `/programs` (policy switches) |
| Journals | `/journals` (CRUD, CSV/XLSX import), `/journals/{id}/lines`, `:submit\|approve\|reject\|recall\|post\|reverse`, `/journal-batches`, `/journals/{id}/attachments` (presigned upload) |
| Journaling runs and GL | `POST /journaling/runs`, `/gl/lines`, `/gl/trial-balance`, `/gl/drilldown/{line_id}` |
| Posting | `/posting/outbox`, `POST /posting/status` |
| Governance | `/audit-events` (hash-chained, exportable), `/users`, `/roles`, `/api-clients` (client-credentials, unlike BlackLine's password grant), `/webhooks` |

**Roles:** `platform_admin`, `config_author`, `config_approver`, `match_preparer`, `match_approver`, `journal_preparer`, `journal_approver_L1..Ln`, `controller`, `auditor` (read-only) and `integration_client`, and every role is scoped by entity.

**SoD blocks:**
- Approving your own work.
- Approving your own config change.
- A rule author activating their own rule.
- Preparing and posting the same journal.

**Before the v1 freeze** (Stage 5), I will check the API shape against low-fidelity workbench wireframes, so the frontend does not force any API rework later.

## Orchestration (Temporal)

| Workflow | Purpose |
|---|---|
| `MatchSetWriterWorkflow(match_set)` | The only writer for a match set, started with signal-with-start. It serializes runs and command batches, continues-as-new every 200 operations, and makes each compute job one heartbeating activity. |
| `JournalingRunWorkflow(entity, window)` | Holds the entity ledger lease. Runs events → posting → validation → rollup → outbox. |
| `JournalApprovalWorkflow(journal)` | Waits for approval signals per level, with reminder and escalation timers, checks SoD, then posts. |
| `OutboxRelay` | Delivers Postgres outbox rows to Temporal and to webhooks, at least once, deduplicated by ID. |
| Schedules (overlap = `SKIP`) | Intraday and daily matching, daily journaling, recurring journals, auto-reversals on period open, ref-data sync |
| `PeriodCloseWorkflow` (Stage 8) | Close tasks with dependencies |

**Temporal rules:**
- Payloads carry references only (limits are 2 MB per payload and about 50k history events).
- Each heartbeat carries the external job ID, so a retry re-attaches to the running job instead of resubmitting it.
- Databricks `run_now` gets `idempotency_token = {run_id}-{attempt}`.
- k8s Jobs are named `{kind}-{run_id[:12]}-a{attempt}` with `backoffLimit: 0`, so retries belong to Temporal alone.
- Workflow modules never import pyspark.
- Changes to long-lived workflows use `workflow.patched()`, and recorded histories are replayed in CI.

## Local platform (kind)

- **Namespaces:** `close-system` (api, worker, outbox relay), `close-data` (minio, spark-connect, spark job pods, optional UC), `close-infra` (postgres, temporal, optional keycloak) and `observability` (optional).
- **Helm:** an umbrella chart `deploy/helm/close-platform` with `values-local.yaml` (later `values-eks.yaml`), and secrets are generated at bootstrap into gitignored files and mounted as files.
- **Makefile targets:** `bootstrap`, `cluster-up`, `images`, `deploy`, `seed PROFILE=small`, `smoke`, `test-unit`, `test-spark`, `test-int`, `test-e2e`, `ci`, `gate-N`, `down`.
- **Profiles:** `core` (default), plus the opt-in `+uc`, `+auth`, `+obs` and `+temporal-pg`.

**Memory budget** (the Docker VM has 12 GB):

| Component | Limit (GB) |
|---|---|
| kind node + metrics-server | 1.2 |
| Postgres + MinIO | 1.0 |
| Temporal dev server | 0.3 |
| Spark Connect (1.25 GB heap) | 2.0 |
| One Spark job pod (2.5 GB heap + pandas workers, `local[4]`) | 4.0 |
| API + worker + relay | 0.7 |
| **`core` total** | **≈ 9.2** |

On top of that, the opt-in profiles add roughly `+uc` 0.75, `+auth` 0.75, `+obs` 1.5–2 and `+temporal-pg` 0.9.

**Job security:**
- The worker's Role in `close-data` allows only jobs (create, get, list, watch, delete), pods (get, list, watch) and `pods/log` (get), so there is no exec and no access to secrets, and a `kubectl auth can-i` matrix asserts this.
- Job pods run the `restricted` Pod Security level, with non-root, a read-only root filesystem, `automountServiceAccountToken: false` and an image pinned by digest.
- Job parameters are IDs only, validated per job kind.
- Spark Connect uses a **read-only** MinIO user, which mirrors the SELECT-only prod principal.
- A NetworkPolicy denies everything by default, and Calico is added if kind's default CNI does not enforce it.

## Testing strategy (tiers every stage uses)

1. **Unit (host):** the DSL typecheck and IR, money and rounding vectors, hash-ID vectors, SoD and approval routing, and API handlers against fakes.
2. **Spark component (host, uv Python + Homebrew JDK 21):** every rule kind and tolerance, the SQL files, golden datasets, and **every Databricks notebook run as a script** through the widget stub.
3. **Reference and differential testing:**
   - A **pure-Python reference matcher** implements the same IR, engine output must equal reference output, and every reference auto-match must be a generator truth pair.
   - Compiled Spark conditions and the Python interpreter must agree on random cases.
4. **Property tests (Hypothesis):** these invariants must hold:
   - Each item has at most one active group or one pending suggestion.
   - Group differences are the sums of their members.
   - Control totals balance.
   - Events balance per currency.
   - Rollup equals detail.
   - Nothing posts to a closed period.
   - Output digests are the same across reruns, partition counts and input orderings.
5. **Integration (compose / testcontainers):** API + Postgres + Temporal test server + MinIO + Spark Connect, with contract tests (Schemathesis), migrations up and down, and an authorization matrix.
6. **End-to-end on kind:** seed landing → match through the API → manual commands → adjustment requests → journaling → approvals → rollup → trial balance, asserted against the generator's answer key.
7. **Fault injection:** kill the job or the worker after each table in the commit order, and the retry must end with the same digest.
8. **Performance:** a fixed `medium` profile (about 1M items) runs locally, while 10M+ items run on Databricks only, and every gate after the first fails on a regression over 25%.
9. **Security:** ruff/bandit, pip-audit, trivy, checkov, gitleaks and the banned-API lint.

**Stage gate artifact:** `docs/stages/stage-N-report.md`, which contains the real command output, the frozen contract versions and any open issues. **I stop at each gate for your sign-off.**

## Repository layout (monorepo)

```
financial-month-close-tool/
  docs/{plan/ROADMAP.md, adr/, domain/, stages/, runbooks/}
  libs/contracts/src/close_contracts/   # NO pyspark: ids, canonical_json, money, calendar, enums, tables,
                                        # resolver, commands, manifest, dsl/{ast,functions,typecheck,ir,interpret},
                                        # config/{envelope,data_source,match_set,…}, canonical/{items,landing/*}, schemas/, test_vectors/
  libs/platform/src/close_platform/     # compute/{base,k8s,databricks,inprocess}, query/{base,templates,spark_connect,
                                        # databricks_sql,local_spark,fake}, evidence  (used by BOTH api and worker)
  engine/src/close_engine/              # runctx, session, io/{commit,pins,scratch,digest}, sparkfns/{ids,money,calendar,text},
                                        # normalize/{bind,incremental,controls}, matching/{lower,blocking,candidates,ambiguity,
                                        # assign,aggregate,subset,self_net,chain,waterfall,outcomes,commands,invariants,metrics},
                                        # jobs/{ddl_apply,normalize,match_run,apply_commands,smoke}.py  (main(argv))
  engine/reference/                     # pure-Python reference matcher (and, in Stage 6, the journal oracle)
  engine/notebooks/                     # thin Databricks notebooks per job
  engine/sql/{ddl/V001__ops.sql…, queries/, checks/}
  engine/databricks.yml                 # bundle: jobs + targets dev/stg/prod
  engine/tests/{unit,spark,property,golden,faults}/
  datagen/src/close_datagen/            # world, flows/, breaks, truth, profiles, writer
  services/api/src/close_api/           # FastAPI app, routers, services, repositories, authz, audit, db/migrations
  services/worker/src/close_worker/     # workflows, activities, outbox relay
  tools/{lint_spark_bans.py, versions.lock}
  deploy/{docker/, helm/close-platform/, kind/cluster.yaml}
  infra/terraform/{aws/, databricks/}   # Stage 10
  frontend/                             # Stage 9
  .github/workflows/  Makefile  pyproject.toml (uv workspace)  .pre-commit-config.yaml
```
`reports/` and `research_notes/` stay as they are.

## Stages

Stages 0–3 are specified in detail, but Stages 4–12 are a roadmap, and each of them gets a detailed spec at the gate before it. Every stage ends with `make gate-N`, a report and your sign-off.

### Stage 0: Toolchain, scaffold, host-side parity
1. **Install:**
   - `brew install kind helm uv openjdk@21 kubernetes-cli databricks/tap/databricks`.
   - `uv python install 3.12`, and set `JAVA_HOME` to Homebrew's openjdk@21, because `/usr/bin/java` is only a stub.
   - Start Docker Desktop at 12 GB, 8 CPUs and 120 GB+ disk.
   - `git init`, and nothing is committed unless you ask.
2. **Scaffold:**
   - The monorepo and the uv workspace, with ruff, `mypy --strict`, pytest + Hypothesis and pre-commit.
   - `tools/versions.lock` and the banned-API lint.
   - The Makefile, an ADR template, and `docs/plan/ROADMAP.md` (this plan, in your writing voice).
   - `compose.test.yaml` with MinIO and Postgres.
3. **Contracts core:** hash IDs with shared test vectors, canonical JSON (RFC 8785), money and rounding, the business-day calendar and the `TableResolver`.
4. **Spike S2, parity.** On MinIO, prove the portable-feature allowlist of at least 14 features:
   - MERGE with insert, update, delete and not-matched-by-source.
   - An insert-only MERGE on an append-only table, where UPDATE is rejected.
   - An error on an unexpected column when auto-merge is off.
   - `VERSION AS OF` and CDF.
   - CHECK constraint violations.
   - Deletion vectors and row tracking.
   - `IDENTIFIER` with named parameters, including path identifiers.
   - `applyInPandas` under ANSI mode.
   - Exact `DECIMAL(18,0)×DECIMAL(19,12)`.
   - `bround` half-even vectors.
5. **Spike S4, local half.**
   - A wheel entrypoint gives the same output digest whether it runs through spark-submit or `python -m`.
   - **Every Databricks notebook file also runs locally as a script.** Source-format notebooks are plain `.py`, and a small widget stub (about 20 lines, test-only) feeds `main(argv)`. This test runs at every later gate, so the notebooks are proven to work long before a Databricks workspace exists.

**Gate 0:**
- Running `make bootstrap` a second time changes nothing, and installed versions match `versions.lock`.
- ruff and mypy report 0 errors, unit tests are 100% green, and contracts coverage is ≥ 90%.
- The banned-API lint catches a seeded violation.
- At least 50 hash-ID vectors are identical in Python and Spark.
- S2: every allowlisted feature passes 3 of 3 runs on MinIO.
- S4: the digests are equal, and every notebook runs as a script through the widget stub.
- ADRs for S2 and S4 are written.

### Stage 1: Local Kubernetes platform, spikes, skeleton services
1. **Cluster:** kind (single node, v1.36.4 by digest), a local registry, metrics-server and namespaces.
2. **Spike S1, catalog.** The path registry is the default, and we also run the `+uc` profile (UC OSS 0.6.0 on Postgres) to check three-level names, MinIO credentials, 24-hour token expiry and external tables. We do not use views, because `CREATE VIEW` needs Spark 4.2 on UC OSS.
3. **Helm charts:** postgres, minio, spark-connect, temporal (dev server), api, worker and relay, plus the optional profiles.
4. **Spike S3, read path.** Spark Connect plus the QueryEngine templates, with a latency baseline.
5. **Spike S5, Temporal.** The worker runs in kind, and a replay test runs in CI.
6. **Spike S6, ComputeBackend.** k8s Job submit, find, status, logs, cancel and `ensure_terminal`, run under an RBAC matrix.
7. **Skeleton services:**
   - The API: health and readiness, OpenAPI, settings, JSON logs, OTel and an Alembic baseline.
   - The worker: `HelloPipelineWorkflow`, which runs a Spark job that writes Delta, then reads the result back through the QueryEngine and returns it via the API.
8. **CI:** `.github/workflows` covering lint, types, unit, Spark, image build, compose integration, security scans and a nightly kind smoke run, and `make ci` mirrors it locally.

**Gate 1:**
- A cold `cluster-up deploy` takes ≤ 15 min and a warm one ≤ 4 min, and after 3 down/up cycles every pod is Ready.
- `make smoke` passes 10 of 10 times, each ≤ 120 s.
- In 3 trials, killing the worker mid-smoke leaves exactly one Job per attempt and the same digest.
- Cancel deletes the Job within 30 s.
- Peak total pod memory (sampled every 5 s) stays ≤ 9.5 GB, with 0 OOM kills and 0 restarts.
- Read-path p95 is ≤ 1.5 s for 3 templates over 100 calls.
- The can-i matrix denies exec, secrets and cross-namespace access.
- trivy, pip-audit and bandit report 0 unwaived critical or high findings, and gitleaks finds 0 secrets.
- ADRs for S1, S3, S5 and S6 are written.

### Stage 2: Domain, contracts and synthetic marketplace data
1. **Domain spec** (`docs/domain/`):
   - Lifecycle and money flows, the glossary and the BlackLine concept map.
   - An **accounting-policy sheet for your controller to sign off**, covering the switches above plus failed-authentication penalties, chargebacks, shipping and gift cards.
2. **Contracts:**
   - Expected landing schemas, each behind a mapping so your real tables can plug in.
   - Canonical item and event schemas, and the matching and ledger output schemas.
   - The config envelope and the rule-DSL grammar, including typecheck and IR (no Spark), plus the JSON Schemas.
   - The Delta DDL as `V###` migrations.
3. **Generator:** it is deterministic and seeded, and it covers:
   - Programs.
   - US, EU and UK entities in USD, EUR and GBP.
   - Sellers with fee tiers.
   - The order lifecycle, firing events on each program's trigger.
   - Stripe-shaped and Adyen-shaped PSP data.
   - Daily seller payout cycles through Hyperwallet-like and PayPal Payouts-like providers.
   - Bank rows derived from BAI2 and camt.053, FedEx-like carrier lines, AvaTax-like tax documents and a stored-value ledger.
   - Every break type, injected at configurable rates.
   - An **answer key** with the expected groups and outcomes per template, where genuinely ambiguous cases (same amount and date, no reference) are labelled `suggested` or `open`.
4. **Profiles:** `tiny` (unit), `small` (e2e, about 10k orders) and `medium` (about 1M items, for performance), while `large` runs on Databricks only.

**Gate 2:**
- `ddl_apply` on an empty catalog takes ≤ 60 s, and a rerun applies 0 migrations.
- `DESCRIBE` matches the contract schemas for 100% of tables.
- JSON Schema snapshots show 0 drift.
- Round-trip tests pass for 1,000 examples per model.
- Seeds {11, 23, 47} produce identical landing digests across 3 runs, and with `local[1]` vs `local[4]`.
- `small` generates in ≤ 3 min and `medium` in ≤ 25 min.
- Each break type appears ≥ 25 times in `small`, within ±10% of its configured rate.
- The answer key passes 100% of its self-consistency checks.
- Landing control totals equal the generator manifest.
- The policy sheet has been sent, and its sign-off gates **Stage 6**, not Stage 3.

### Stage 3: Matching engine (data plane)
1. **Normalize job:**
   - Bind data sources to landing tables.
   - Process incrementally, using CDF when the landing owner enables it, and otherwise a declared incremental column with a lookback, plus a weekly full compare on key and row hash.
   - Write item versions and corrections.
   - Enforce the control totals.
2. **Rule lowering:** IR → Spark Columns, blocking, candidate limits, ambiguity policies and assignment.
3. **Match kinds:**
   - `1:M` / `M:1` with bounded subset-sum.
   - `M:M` with equal totals.
   - `self_net` and `chain`.
   - Timing rules.
4. **Waterfall and state machine:**
   - The waterfall and the state machine, including suggestion supersession and broken groups.
   - Adjustment requests (create and cancel).
   - The `apply_commands` job for manual match, unmatch, confirm and reject.
   - Commit order, manifest, digests and metrics.
5. **The reference matcher** (pure Python, over the same IR).
6. **Templates 1, 2, 3, 6, 9 and 10** as configurations, each with golden tests.

**Gate 3.** Every template runs on seeds {11, 23, 47} (`small`) and seed 11 (`medium`).
- **Correctness:**
  - Engine groups exactly equal reference groups.
  - **0 false auto-matches.**
  - Auto-match recall on clean truth pairs is ≥ 99.0% for keyed rules and ≥ 95.0% for keyless ones, and both targets are adjustable per template.
  - 100% of injected breaks reach their expected outcome.
- **Determinism:** digests are identical across 3 reruns, shuffle partitions of 1, 8 and 200, `local[1]` vs `local[4]`, shuffled input order, and in-process vs k8s Job.
- **Crash-safety:** killing the job at ≥ 8 points (after each table, mid-iteration, mid-`apply_commands`) always ends with the clean-run digest.
- **Property tests:**
  - Spark: 200 examples in CI and 2,000 nightly.
  - Reference matcher: 10,000 examples.
  - 0 failures.
- **Differential test:** Spark and the Python interpreter agree on 5,000 random rule/row cases.
- **Fuzzing:**
  - 10,000 invalid rules are rejected with structured errors and 0 crashes.
  - A corpus of ≥ 20 ReDoS patterns is rejected.
  - The compiler generates no SQL strings.
- **Command sequences:** random sequences interleaved with runs (20 in CI, 100 nightly) keep every invariant.
- **Performance:** on `medium`, templates 2 and 3 complete in ≤ 15 min in one pod (4 cores, 4 GB), and the per-rule baseline is recorded.
- **Coverage:** engine lines ≥ 85%, and branches in the typecheck and compiler ≥ 90%.

### Stage 4: Governance kernel (before any business API)
- Auth: a dev JWT issuer plus the Keycloak `+auth` profile.
- Roles, entity scopes and SoD policies.
- The hash-chained audit log, with a verifier CLI.
- The transactional outbox and relay.
- Maker-checker config versioning.
- Idempotency keys, ETags and problem+json errors.
- A registry of query templates, which rejects any template without `:entity_ids`.

**Gate 4:**
- An authorization matrix (role × endpoint × entity) with 0 unexpected allows.
- Every SoD scenario is blocked.
- An audit-chain tamper test is detected.
- The outbox delivers exactly once under relay crashes.
- Idempotent replays return the same responses.

### Stage 5: Matching control plane
- Config APIs with validation and simulation.
- `MatchSetWriterWorkflow`, the run APIs and the read APIs (items, groups, subtotals, saved views).
- Manual commands, including select-by-filter.
- Exception workflow and write-offs, webhooks and schedules.
- **Templates 4, 5, 7 and 11.**
- A wireframe review of the API shape, then the **matching API v1 freeze**.
- **D19 confirmed.**

**Gate 5:**
- Schemathesis contract tests pass.
- Concurrency: commands arriving during a run never double-match.
- Failure injection passes.
- End-to-end runs of every template on kind pass.
- 100% of state changes produce an audit event.

### Stage 6: Journaling engine
- Events with versions and corrections.
- Posting rules and account derivation.
- Validation and suspense policy.
- FX with realized and revaluation entries.
- Period fencing and prior-period adjustments.
- Rollups with `rollup_line_id`, and `gl_outbox`.
- Adjustment consumption.
- The **journal oracle**, built from the signed-off policy sheet.

This stage needs the signed-off policy sheet, your chart of accounts and your dimensions.

**Gate 6:**
- Journals equal the oracle for every combination in the policy-switch matrix.
- Accounting invariants hold.
- Corrections and cross-period reruns are deterministic and idempotent.
- The suspense circuit breaker trips when it should.
- A performance baseline is recorded.

### Stage 7: Journal workflow and API
- Templates with dynamic columns.
- Manual journals, with CSV/XLSX import.
- Multi-level approvals with thresholds, delegation and reminders.
- A risk score (AS 2401.61) that feeds approval routing.
- Atomic batches, recurring journals and auto-reversals.
- Attachments.
- Posting to the Databricks GL tables (`posted`), plus the optional ERP write-back (`erp_confirmed`), exercised with a stub ERP feed.
- Period open, close and lock.
- Automated journal definitions.

**Gate 7:**
- Every legal and illegal state transition is tested.
- SoD, routing and period-lock tests pass.
- Posting to Delta is idempotent.
- The stub-ERP write-back round-trip works.
- End-to-end runs on kind pass, and none of this needs a real ERP.

### Stage 8: Close management
- Account reconciliations with auto-certification rules, and reconciling items drawn from in-transit matches.
- **Templates 8 and 12.**
- Trial balance and roll-forward from the Databricks GL tables.
- If D19 keeps an ERP as book of record, a Delta-to-ERP trial-balance tie-out that runs against an ERP extract.
- Flux analysis.
- Close task checklist and `PeriodCloseWorkflow`.

### Stage 9: Frontend (React/TS)
- Pages:
  - dashboard
  - match workbench (side-by-side, bulk select, match with a difference, suggested queue)
  - exceptions
  - rule builder with simulation
  - journals grid and a template-driven editor
  - approvals inbox
  - period close
  - GL explorer with drill-down
  - audit viewer
  - admin
- Playwright tests and WCAG 2.1 AA.

### Stage 10: AWS and Databricks infrastructure, CI/CD
- **Terraform, AWS:** VPC, EKS, Aurora (or Lakebase, by ADR), S3, KMS, IAM (Pod Identity), ECR, ALB, Route53/ACM and Secrets Manager with External Secrets.
- **Terraform, Databricks:** UC catalogs, schemas and grants (only the job service principal writes `match` and `ledger`), plus service principals, the SQL warehouse, bundle targets and a UC Volume with Object Lock for config snapshots.
- Temporal prod ADR.
- **GitHub Actions:** OIDC to AWS, build, test, scan, sign and push, bundle validate and deploy, and migration jobs.
- **Argo CD:** dev → stg → prod, with manual approval for prod.
- The nightly DBR 18 parity suite on Databricks dev.

### Stage 11: Enterprise hardening and compliance evidence
- Threat model and penetration-test fixes.
- Backup and restore, and DR drills, with a target of at least BlackLine's floor (99.9% availability, 2 h RPO and 24 h RTO).
- Tokenized PII.
- Automated ITGC evidence: change logs, access reviews and the `DESCRIBE HISTORY` writer check.
- Auditor export packs.

### Stage 12: Scale, parallel run and cutover
- Production-volume performance and cost tuning on Databricks.
- BlackLine config harvest and import, with BlackLine field-name aliases on our config models.
- History extraction.
- A **parallel run against BlackLine for 2 or more closes**, with a variance report.
- UAT, the audit walkthrough, go-live and hypercare.

## Inputs needed from you, by stage

| Stage | Inputs |
|---|---|
| 2 | Landing-table schemas or samples. Which PSPs, payout providers, banks and statement formats, carriers and tax engine you use. Entities and currencies. Volumes (orders per day, peak). Controller review of the policy sheet. |
| 3 | Priority match sets and target match rates. Your BlackLine Match Set and pass-rule export (read-only admin calls in the report's checklist), plus screenshots of the rule conditions. |
| 5 | Confirmation of D19 (GL book of record) |
| 6–7 | Chart of accounts and dimensions, current JE templates and automated journal definitions, approval matrix, SoD rules, close calendar |
| 10 | AWS account and network standards, IdP, Databricks workspaces and UC naming, GitHub org |

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Local OSS Spark drifts from DBR 18 | Pinned versions, the S2 allowlist, and a nightly parity suite on Databricks dev from Stage 10 |
| OSS Delta on S3 is unsafe with multiple writers | A single-writer workflow, MERGEs scoped to one partition, and one job at a time locally |
| Nondeterminism | Banned-API lint, `total_order()`, pinned session config, work budgets, and digest tests |
| Combinatorial blow-up | Mandatory blocking, candidate limits, bounded subset-sum with explicit limit outcomes |
| Unclear accounting policy | Everything configurable, and the controller signs off before Stage 6 |
| Auditors don't accept a self-built tool | Maker-checker config, SoD, append-only tables, control totals, hash-chained audit, writer checks, manifests carrying git SHA and image digest |
| GL book of record left undecided | D19 has a default and must be confirmed at the Stage 5 gate |
| 24 GB RAM | 12 GB VM, single-node kind, opt-in profiles, `medium` locally and `large` only on Databricks |
| Scope creep and rework | Contracts frozen at every gate; later stages are specified only at the previous gate |

## Verification

- `make gate-N` runs lint, types, unit, Spark, reference and differential, property, integration, fault-injection and kind e2e tests for that stage, and then it writes `docs/stages/stage-N-report.md` with the real output.
- For Stage 0, that is `make bootstrap && make ci && make gate-0` on this Mac.
- For Stage 1, that is `make cluster-up deploy smoke gate-1`, plus a `curl` of `/api/v1/health` and the smoke endpoint, a `kubectl top` memory capture and the can-i matrix output.
- I stop at every gate for your review before starting the next stage.

## Appendix A: Key interfaces

```python
class TableResolver(Protocol):                       # close_contracts.resolver
    def table(self, t: LogicalTable) -> str: ...     # value bound to IDENTIFIER(:t)
    def landing(self, source_key: str) -> str: ...   # configured source-owned table
    def evidence_uri(self, *parts: str) -> str: ...  # MinIO path / UC Volume

@dataclass(frozen=True)                              # close_engine.runctx
class RunContext:
    run_id: str; attempt: int; job_kind: JobKind; env: Env
    match_set_id: str | None; entity_id: str | None; as_of: date
    started_at: datetime                 # from orchestrator; engine never calls now()
    config_uri: str; config_sha256: str; engine_version: str; git_sha: str; image_digest: str
    resolver: TableResolver
    @classmethod
    def from_argv(cls, argv: Sequence[str]) -> "RunContext": ...
    def session(self) -> SparkSession: ...                               # applies + asserts pinned conf
    def pins(self, spark: SparkSession) -> Mapping[LogicalTable, int]: ... # write-once per run_id
    def scratch(self, step: str) -> str: ...

class ComputeBackend(Protocol):                      # close_platform.compute.base
    async def submit(self, spec: JobSpec) -> JobHandle: ...     # idempotent on (run_id, attempt)
    async def find(self, run_id: str, attempt: int) -> JobHandle | None: ...
    async def status(self, h: JobHandle) -> JobStatus: ...      # PENDING|RUNNING|SUCCEEDED|FAILED|CANCELLED|UNKNOWN
    async def cancel(self, h: JobHandle) -> None: ...
    async def ensure_terminal(self, h: JobHandle, timeout: timedelta) -> JobStatus: ...

class QueryEngine(Protocol):                         # close_platform.query.base
    async def run(self, template_id: str, params: Mapping[str, ParamValue], *, principal: Principal,
                  page: PageRequest | None = None, timeout_s: float = 30.0) -> QueryResult: ...

def typecheck(rule: PassRule, sides: SideSchemas) -> list[Issue]: ...   # close_contracts.dsl (no Spark)
def to_ir(rule: PassRule, sides: SideSchemas) -> RuleIR: ...            # rule_hash = sha256(JCS(ir))
def lower(ir: RuleIR) -> CompiledRule: ...                              # close_engine: Columns only, never F.expr
```

## Appendix B: Core matching tables (all partitioned by `match_set_id`; full DDL is a Stage 2 deliverable)

| Table | Grain | Key columns |
|---|---|---|
| `match.items` (mutable current state) | item | `item_id` (H('itm',ms,ds,source_key)), `item_version`, `data_source_id`, `side`, `entity_id`, `currency`, `amount_minor`, `item_date`, `item_bd_index`, `ref_1..3`, `description_norm`, `attrs`, `state` (open/suggested/matched/written_off/voided), `state_seq` (ETag), `active_group_id`, `pending_suggestion_id`, `exception_code` |
| `match.item_versions`* | item version | `change_kind` (inserted/corrected/voided), `source_table`, `source_version`, `source_key`, `source_row_hash`, `content` struct, `data_source_sha`, `run_id` |
| `match.item_state_log`* | transition | `log_id`, `from_state`, `to_state`, `group_id`, `reason_code` (AUTO_RULE/SUGGESTED/MANUAL/UNMATCH/SOURCE_CORRECTED/SUPERSEDED/WRITE_OFF), `rule_id`, `rule_hash`, `command_id`, `actor`, `run_id` |
| `match.groups` | group | `group_id` (H('grp',run\|command,rule,sorted items)), `match_type`, `status`, `kind`, `rule_id/version/hash`, `pass_sequence`, `ambiguous`, `alternatives`, `sum_a_minor`, `sum_b_minor`, `difference_minor`, `within_tolerance`, `created_*`, `closed_*`, `approval_ref` |
| `match.group_items`* | membership | `group_id`, `item_id`, `item_version`, `side`, `currency`, `amount_minor` |
| `match.adjustment_requests`* | request event | `action` (create/cancel), `cancels_id`, `group_id`, `entity_id`, `currency`, `amount_minor`, `reason_code`, `account_hint`, `accounting_date`, `source` |
| `ops.run_manifests`* | run attempt | `status`, `engine_version`, `git_sha`, `image_digest`, `config_uri/sha256`, `session_conf`, `input_pins`, `output_versions`, `output_digests`, `control_totals`, `rule_metrics`, `invariants`, `error` |

## Appendix C: Rule DSL example (PSP payout ↔ bank credit)

```json
{ "dsl_version": "1.0", "match_set_id": "ms-psp-payout-bank-us01", "entity_id": "US01",
  "sides": { "A": {"data_source_id": "ds-psp-payouts"},
             "B": {"data_source_id": "ds-bank-lines",
                   "filter": {"op": "eq", "args": [{"field": "B.attrs.direction"}, {"lit": "CR"}]}} },
  "rules": [{
    "rule_id": "psp-bank-10", "sequence": 10, "kind": "1:1", "outcome": "auto",
    "ambiguity": {"policy": "unique_only", "on_ambiguous": "leave_open"},
    "conditions": [
      {"id": "ccy", "type": "equals", "left": {"field": "A.currency"}, "right": {"field": "B.currency"}},
      {"id": "amt", "type": "amount_tolerance", "sign": "same",
       "left": {"field": "A.amount_minor"}, "right": {"field": "B.amount_minor"},
       "tolerance": {"mode": "pct_with_cap", "pct_bps": 10, "cap_minor": 500}},
      {"id": "date", "type": "date_window", "unit": "business_days", "calendar": "US_FED",
       "roll": "following", "min": 0, "max": 3,
       "left": {"field": "A.item_date"}, "right": {"field": "B.item_date"}},
      {"id": "ref", "type": "contains", "min_needle_len": 8,
       "haystack": {"fn": "normalize_ref", "args": [{"field": "B.description_norm"}]},
       "needle":   {"fn": "normalize_ref", "args": [{"field": "A.ref_1"}]}} ],
    "rank_by": [{"metric": "abs_difference", "dir": "asc"}, {"metric": "bd_distance", "dir": "asc"}],
    "difference": {"when": "nonzero_within_tolerance", "emit": "adjustment_request",
                   "reason_code": "PSP_BANK_FEE", "account_hint": "bank_fees"},
    "limits": {"max_candidates_per_item": 50, "max_block_pairs": 100000} }],
  "timing_rules": [{"side": "A", "classify_as": "in_transit", "expected_clear_bd": 3}] }
```

**Rule `psp-bank-20`** (sequence 20) is stored as a complete, standalone rule, so each rule can be audited on its own. It differs from `psp-bank-10` in four ways:
- It has no `ref` condition.
- Its `outcome` is `suggested`.
- It uses `greedy_total_order` / `suggest_best`.
- Its reason code is `PSP_BANK_FEE_REVIEW`.

**How `psp-bank-10` compiles:**
- **Blocking.** It blocks on currency plus `A.bd + k = B.bd` for k ∈ 0..3, and amounts are bucketed by `amount_minor div 500`, with B exploded into buckets {b−1, b, b+1}.
- **Tolerance.** The test is `abs(B−A) <= least((abs(A)*10) div 10000, 500)`, using integer `div` only.
- **`contains`.** This becomes a non-null, ≥ 8-character `instr` test.
- **Rank.** `[abs(diff), B.bd−A.bd, A.item_id, B.item_id]`.
- **Missing reference.** Items with a null `ref_1` fall through to rule 20.

## Changelog

- **2026-09-26.** D2 was amended, and the local S3-compatible store is now SeaweedFS (`chrislusf/seaweedfs:4.47`) instead of MinIO, because the MinIO community edition repository was archived on 2026-04-25 and is now source-only, with no maintained images. The evidence and the decision are in [docs/adr/0003-local-object-store-seaweedfs.md](../adr/0003-local-object-store-seaweedfs.md).
- **2026-09-26.** The direction of D8 was amended, and the final design gets settled in Stage 1 spike S3.
  - OSS Delta Lake 4.1–4.4 drops SQL parameter markers when its SparkSession extension is loaded (upstream issue delta-io/delta#7640, with a fix proposed in PR #7747), so parameterised SQL templates cannot run locally.
  - The read path will use typed query specs compiled to DataFrame operations, with a mandatory entity-scope filter injected from the caller's identity.
  - The production backend (parameterised SQL on a SQL warehouse, or Databricks Connect with the same DataFrame backend) gets chosen in Stage 10, behind a differential test.
  - The details are in [docs/adr/0005-read-path-typed-query-specs.md](../adr/0005-read-path-typed-query-specs.md) and [docs/adr/0002-spark-delta-parity-allowlist.md](../adr/0002-spark-delta-parity-allowlist.md).
- **2026-09-26.** We added a new engineering rule, which is that the engine never binds SQL parameters. Data operations use the DataFrame and DeltaTable APIs, and `spark.sql` is allowed only in `close_engine/io/sql.py` (`run_ddl`), for DDL with resolver-rendered identifiers. Lint rules SB010 and SB011 enforce this, and the details are in [docs/adr/0002-spark-delta-parity-allowlist.md](../adr/0002-spark-delta-parity-allowlist.md).
- **2026-09-26.** The Stage 0 gate item "`IDENTIFIER` with named parameters, including path identifiers" moved from the allowlist to the deny list, with the evidence in ADR-0002. The allowlist still has 17 features, and all of them pass 3/3 on SeaweedFS.
- **2026-09-26.** The Databricks CLI install was deferred to Stage 10, because Homebrew 7 needs an explicit `brew trust` for the third-party databricks tap, and that step needs your go-ahead.
- **2026-09-26.** We recorded security waiver PYSEC-2026-113 (pyarrow 21.0.0, pinned to mirror DBR 18) in `tools/security-waivers.toml`, expiring on 2026-12-31, and the advisory states that the Python bindings are not vulnerable.
- **2026-09-27.** Because the platform will have 7 users, all on the accounting team, it now runs on Databricks instead of Kubernetes, and Temporal is dropped (you chose the Databricks App with Lakebase and dropping Temporal). The backend, and later the UI, runs as one Databricks App with two or more instances. Lakebase Postgres holds configuration, workflow state, audit, outbox and leases, the engine runs as Lakeflow jobs, and everything deploys with bundles from GitHub Actions. One writer per match set now comes from a Postgres lease with a fencing token checked at commit plus the Jobs API idempotency token, and local development uses Docker Compose (Postgres, SeaweedFS, Spark Connect and a local job runner) in place of kind. This amends D5, D6, D7, D12, D13, D14 and D15. Stage 1 is re-specified and goes to you for approval before we build it, Stages 10 and 11 change to match, and the details are in [docs/adr/0006-databricks-native-platform.md](../adr/0006-databricks-native-platform.md).
- **2026-09-27.** Stage 1a was built as re-specified, and four design details were settled while building it. First, the writer lease, fencing, outbox and scheduler are recorded in [docs/adr/0007-orchestration-without-temporal.md](../adr/0007-orchestration-without-temporal.md), which amends ADR-0006 so that periodic work runs under per-task claim rows instead of an advisory lock. Second, because Databricks does not document how long it remembers an `idempotency_token`, the relay now searches `runs/list` before any resubmission, and job submissions and cancellations are never dead-lettered, so a long Jobs API outage cannot strand the writer lease. Third, fencing tokens must never go backwards, so restoring the control-plane database from an older backup needs an operator to raise the lease tokens above the highest claim in the lake (the manual step is in ADR-0007, and Stage 10 turns it into an audited command). Fourth, the parity allowlist grew to 18 features with Delta commit `userMetadata` (ADR-0002), and the local Jobs API emulator is described in [docs/adr/0008-jobs-api-emulator-parity.md](../adr/0008-jobs-api-emulator-parity.md).
- **2026-09-27.** You signed off Stage 1a ([docs/stages/stage-1a-signoff.md](../stages/stage-1a-signoff.md)) and decided its two open questions: `httpx2` is added as a development dependency, and `kind`, `helm` and `kubectl` are removed from the toolchain files, because ADR-0006 dropped Kubernetes (nothing was uninstalled). Stage 1b is next, and where it stands is in [docs/stages/stage-1b-next-steps.md](../stages/stage-1b-next-steps.md).
- **2026-09-27.** Stage 1b started. You approved its new dependencies (`pyspark-client` 4.1.3 for the deployed app, the `connect` extra of pyspark for development, `sqlglot`, `zizmor`, the actionlint and trivy images, and Unity Catalog OSS 0.6.0 for the catalog spike), and you chose to raise Docker Desktop to 10 GB now instead of before Stage 3. Because the always-on Spark Connect server adds a second JVM, the Stage 1b memory criterion becomes 8.5 GiB (the Stage 1a criterion stays 6.5 GiB). pandas, pyarrow and numpy stay at the DBR 18 versions in the app as well, so the deployed app runs exactly what the tests run.
- **2026-09-27.** Stage 1b was built, and four details were settled while building it. First, the read path's final format is recorded in [docs/adr/0009-read-path-query-specs-final.md](../adr/0009-read-path-query-specs-final.md), which amends ADR-0005. Reads are typed QuerySpecs run over Spark Connect with the caller's entity filter added by the engine, every page reads the Delta version pinned on the first page, and the same semantics have a SQL renderer ready for a SQL warehouse. Second, spike S1 ran against Unity Catalog OSS 0.6.0 as its own Compose project (`make uc-spike`) instead of a `+uc` profile of the main stack, and all eight catalog operations work once the catalog has a managed storage location ([docs/adr/0010-unity-catalog-spike.md](../adr/0010-unity-catalog-spike.md)), so D2's catalog setup holds locally, while Databricks Unity Catalog is still confirmed in Stage 10. Third, the jar lock now leaves out every artifact pyspark ships in any version, because Spark loads its own copy first and the extra copies only carried their vulnerabilities into the image, and trivy gates `close-app` in full and `close-spark` for what we add to it (findings inside the Spark distribution's own jars are reported, not gated). Fourth, the `spark-connect` container limit is 2048m instead of 1792m, because the JVM came close to 1792m under the 1M-row benchmark. The trivy policy and its two waivers go to you with the Stage 1b report ([docs/stages/stage-1b-summary.md](../stages/stage-1b-summary.md)).
- **2026-09-27.** You signed off Stage 1b ([docs/stages/stage-1b-signoff.md](../stages/stage-1b-signoff.md)) after its gate passed 27 of 27 criteria. You accepted the trivy policy (`close-app` gated in full, `close-spark` gated for what we add to it, with the Spark distribution's own jars reported) and its two waivers, which expire on 2026-12-31. The thin memory margin of the local Spark Connect JVM (1,901 of 2,048 MiB at its peak, with no kill) becomes the first task of Stage 2, and Stage 2 starts when you give the go-ahead.
- **2026-09-27.** You approved the Stage 2 plan ([docs/stages/stage-2-plan.md](../stages/stage-2-plan.md)): Stage 2 is split into 2a (domain spec, configuration contracts and the rule DSL) and 2b (schemas, migrations and the generator), each with its own gate. Stage 2a is built. The DSL and configuration contracts are recorded in [docs/adr/0011-rule-dsl-and-configuration-contracts.md](../adr/0011-rule-dsl-and-configuration-contracts.md): integer literals are limited to the JSON-safe range so every configuration can be hashed under RFC 8785, the regex subset is stricter than RE2 so Java and Python read every accepted pattern the same way, and `close-contracts` now depends on Pydantic. The Spark Connect memory margin is fixed with two measured settings (peak about 1.85 to 1.65 GiB). One defect found during that work is open: the app does not recover when the Spark Connect server restarts (details in [docs/stages/stage-2a-summary.md](../stages/stage-2a-summary.md)).
- **2026-09-28.** You signed off Stage 2a ([docs/stages/stage-2a-signoff.md](../stages/stage-2a-signoff.md)). Its gate passed 35 of 35 criteria. Stage 2b (schemas, migrations and the seeded generator) starts now, beginning with the SeaweedFS memory margin and a spike of `medium`-scale generation.
- **2026-09-28.** You signed off Stage 2b ([docs/stages/stage-2b-signoff.md](../stages/stage-2b-signoff.md)). Its gate passed 45 of 46 criteria. The open one is whether the accounting-policy sheet has gone to the controller, which must be settled before Stage 6. Stage 3 (the matching engine) starts now.
- **2026-09-28.** In Stage 3, the Spark rule compiler could not be written in this session. You chose to build what doesn't need it first. That slice is built: the pure-Python reference matcher, templates 1, 2, 3, 6, 9 and 10 as 12 match sets, and golden tests against the answer key ([ADR-0013](../adr/0013-reference-matcher-semantics.md), `make gate-3-ref`).
  - **Results.** Every template scores 0 false automatic matches and 100% recall on `small` seeds 11, 23 and 47 and `medium` seed 11, with every injected break reaching its expected outcome.
  - **Corrections to Stage 2b.** Building it corrected two Stage 2b answer-key expectations (duplicate payables, carrier corrections) and a defect in the default carrier data source's entity mapping. These await your confirmation.
- **2026-09-28.** You signed off the Stage 3 reference slice ([docs/stages/stage-3-ref-signoff.md](../stages/stage-3-ref-signoff.md)). Its gate passed 51 of 51 criteria. The sign-off accepts:
  - the Stage 2b corrections above;
  - the rule that a matching set never groups items from different entities.

  **Change of order.** You chose to start **Stage 4 (the governance kernel) before finishing Stage 3**, because it doesn't depend on the matcher. Stage 3 stays open until the Spark rule compiler is written.
- **2026-09-28.** Stage 4 was re-specified for the Databricks App platform ([docs/stages/stage-4-plan.md](../stages/stage-4-plan.md), [ADR-0014](../adr/0014-governance-kernel.md)). **D12 is amended:**
  - identity comes from the Databricks Apps proxy headers, not from OIDC JWTs;
  - the dev JWT issuer and the Keycloak `+auth` profile are dropped;
  - machine clients wait for Stage 10;
  - roles, entity scopes and separation of duties live in the app as planned.

  **Also replaced:** the query-template registry, superseded by typed query specs whose engine always adds the entity filter (ADR-0009). A route-inventory test takes its place. The outbox was built in 1a and is covered by regression.
- **2026-09-29.** You signed off Stage 4 ([docs/stages/stage-4-signoff.md](../stages/stage-4-signoff.md)). Its gate passed 62 of 62 criteria.
  - **The sign-off accepts:**
    - the D12 amendment;
    - reading a multi-entity match set with a role in any one of its entities (changes still need all of them);
    - `GLOBAL` auditors seeing every audit event.
  - **Next, by your choice:** Stage 6's inputs, meaning the signed policy sheet, the chart of accounts and the dimensions ([docs/domain/stage-6-inputs.md](../domain/stage-6-inputs.md)). Stage 3 stays open until the Spark rule compiler is written.
- **2026-09-29.** At your request, Stage 6's first slice was built on **sample inputs**: example policy answers, a chart of accounts, dimensions, account mappings, event types and automated journal definitions, all labelled `sample_` ([docs/stages/stage-6-plan.md](../stages/stage-6-plan.md), approved automatically as you asked; [ADR-0015](../adr/0015-journaling-configuration-and-reference-engine.md); `make gate-6-ref`).
  - **Journaling rules are configuration** that the accounting team authors and approves through the API:
    - `/event-types`, `/auto-journal-definitions`, `/account-mappings`, `/accounts`, `/dimensions` and `/programs`;
    - migration 0003 widens the kind check.

    These journaling configuration APIs come forward from Stage 7. There is no separate `/posting-rules`: posting rules are a definition's line templates.
  - **The reference journaling engine** is pure Python, held to an independent oracle, the policy matrix and the balance invariants. The Spark journaling job waits for the rule compiler.
  - **A configuration that an active one uses can't be retired.** This also closes a Stage 4 gap for data sources used by active match sets.
  - **The generator's FX rates** now cover every date it emits.
  - **One question was added to the policy sheet:** the basis of the incentive cap.
  - **Nothing is confirmed.** The signed policy sheet, the chart of accounts and the dimensions are still needed before production.
  - **Its gate passed 73 of 73 criteria** in 64.8 minutes ([docs/stages/stage-6-ref-report.md](../stages/stage-6-ref-report.md)), and is waiting for your sign-off.
- **2026-09-30.** You signed off the Stage 6 first slice ([docs/stages/stage-6-ref-signoff.md](../stages/stage-6-ref-signoff.md)). Its gate passed 73 of 73 criteria.
  - **The sign-off accepts:**
    - ADR-0015 and the sample semantics;
    - the snapshot changes;
    - the retirement rule, which amends ADR-0014 (the lock for simultaneous changes comes with Stage 5's hardening);
    - the generator's longer FX rate horizon.
  - **Next, by your choice:** Stage 7, the journal workflow, built on sample inputs. The real Stage 6 inputs are still needed before production.
- **2026-09-30.** Stage 7 was planned on sample inputs ([docs/stages/stage-7-plan.md](../stages/stage-7-plan.md), approved automatically). It is split into two parts, each with its own gate and sign-off:
  - **7a, journals in the control plane:** templates, manual journals and CSV import, approvals with thresholds and an AS 2401.61 risk score, SoD and delegation, periods, batches, recurring journals, reversals, attachments and the auditors' evidence ([ADR-0016](../adr/0016-journal-workflow.md), `make gate-7a`).
  - **7b, posting and the general ledger.** It posts approved journals to the Databricks ledger and adds the stub ERP write-back. The D19 default applies unless you decide otherwise: an ERP stays the book of record, so `posted` means committed to the Databricks ledger and `erp_confirmed` is optional.
  - **The real inputs:** your approval matrix, SoD rules, templates, recurring journals and close calendar are requested in [docs/domain/stage-7-inputs.md](../domain/stage-7-inputs.md).
- **2026-09-30.** Stage 7a was built on sample inputs, and its gate passed 84 of 84 criteria on the second run ([docs/stages/stage-7a-report.md](../stages/stage-7a-report.md)).
  - **The first run** passed 83 of 84. Trivy found two HIGH OpenSSL vulnerabilities, published after the previous scan, in the Debian base of our images. Both Dockerfiles now pin Debian's patched OpenSSL until the base image ships it.
  - **Waiting for your sign-off.**
- **2026-09-30.** You signed off Stage 7a ([docs/stages/stage-7a-signoff.md](../stages/stage-7a-signoff.md)). Its gate passed 84 of 84 criteria.
  - **The sign-off accepts** ADR-0016 and the snapshot changes.
  - **`openpyxl` is approved** for XLSX import.
  - **Next, by your choice:** Stage 7b on sample inputs. It starts by fixing the temporary OpenSSL pin.
- **2026-10-01.** Stage 7b was built on sample inputs ([docs/stages/stage-7b-plan.md](../stages/stage-7b-plan.md), approved automatically), and its gate passed 97 of 97 criteria on the second run ([docs/stages/stage-7b-report.md](../stages/stage-7b-report.md)).
  - **What it adds:** posting approved journals to the Databricks ledger exactly once, the ERP write-back with a stub ERP, trial balance and drill-down, a Postgres-to-Delta tie-out and XLSX import ([ADR-0017](../adr/0017-posting-and-the-erp-write-back.md)). The OpenSSL pin is gone.
  - **The first run** passed 96 of 97. A test in the Jobs API emulator's suite still listed the bundle's three jobs, and the bundle now has `post_journals` as a fourth.
  - **Moved to 7c:** multi-currency manual journals. The engine's automated entries in the journal population, and GL-history risk factors, still wait for the Spark rule compiler.
  - **Waiting for your sign-off.**
- **2026-10-01.** You signed off Stage 7b ([docs/stages/stage-7b-signoff.md](../stages/stage-7b-signoff.md)). Its gate passed 97 of 97 criteria.
  - **The sign-off accepts** ADR-0017, its two differences from the plan (the tie-out by totals, and the reworded race criterion) and the table-registry snapshot change.
  - **D19 keeps its default for now:** an ERP stays the book of record, and it is still to be confirmed before production.
  - **`psycopg[binary]` is approved** as an engine dependency for the posting job.
  - **Next, by your choice:** 7c on sample inputs, multi-currency manual journals, which finishes Stage 7.
- **2026-10-01.** Stage 7c was planned on sample inputs ([docs/stages/stage-7c-plan.md](../stages/stage-7c-plan.md), approved automatically). It adds manual journals in foreign currencies:
  - rates come from a narrow, typed lookup over `ref.fx_rates`, so the read path's entity filter stays as signed off;
  - conversion follows D9 and is frozen at submit, bound into the approval, with one rounding line per journal;
  - reversals reuse the original's rate.
- **2026-10-01.** Stage 7c was built on sample inputs, and its gate passed 109 of 109 criteria on the first run ([docs/stages/stage-7c-report.md](../stages/stage-7c-report.md), [ADR-0018](../adr/0018-multi-currency-manual-journals.md)).
  - **Stage 7 is complete** on sample inputs. The engine's entries in the journal population, and GL-history risk factors, still wait for the Spark rule compiler.
  - **Also changed:**
    - ADR-0002's allowlist gains `ADD COLUMNS` as feature 19;
    - ADR-0017's XLSX replay note is amended;
    - both images now upgrade Debian's PCRE2 for CVE-2026-103111.
  - **Waiting for your sign-off.**
- **2026-10-01.** You signed off Stage 7c ([docs/stages/stage-7c-signoff.md](../stages/stage-7c-signoff.md)). Its gate passed 109 of 109 criteria, and Stage 7 is complete on sample inputs.
  - **The sign-off accepts** ADR-0018 and its three rules (the accounting date's rate with no fallback, no triangulation, reversals at the original's rate), the contract changes and the amendments to ADR-0002 and ADR-0017.
  - **Images take every Debian security fix at build time** (`apt-get -y upgrade`), instead of upgrading named packages.
  - **Next, by your choice:** Stage 8 on sample inputs, close management. It starts with the Dockerfile change.
- **2026-10-01.** Stage 8 was planned on sample inputs ([docs/stages/stage-8-plan.md](../stages/stage-8-plan.md), approved automatically). A survey found the Databricks GL holds only manual journals, because the journaling engine's Spark job waits for the compiler. So Stage 8 is split into three parts, each with its own gate and sign-off:
  - **8a, the general ledger:** a sample stand-in job writes the reference engine's journals to Delta, with configuration snapshots in Postgres. It adds balances and roll-forward from the whole GL, and the ERP trial-balance extract with its tie-out.
  - **8b, the close:** the close checklist, flux analysis and `PeriodCloseWorkflow` with a soft close.
  - **8c, account reconciliations:** profiles with auto-certification, templates 8 and 12 through a supporting-balances job, and reconciling items.
- **2026-10-02.** Stage 8a was built on sample inputs, and its gate passed 118 of 118 criteria on the first run ([docs/stages/stage-8a-report.md](../stages/stage-8a-report.md), [ADR-0019](../adr/0019-the-general-ledger-on-databricks.md)).
  - **The Databricks GL has the engine's journals.** The `journal_ref` stand-in job writes the reference engine's lines and rollups for an entity, from a configuration snapshot in Postgres. On the stack it journals 339 US01 events into 927 lines and 67 rollups, and a rerun writes nothing.
  - **Balances, the roll-forward and the ERP tie-out** read the whole GL (the engine's rollups and the posted manual journals). An integration client uploads the ERP's trial balance as CSV.
  - **Nothing is booked twice.** Pins cover the configuration and inputs. Before writing, the job compares what the ledger already holds, which covers a failed run and a period closed since. A run waits for the previous one to be settled.
  - **Also changed:**
    - 7b's posting job checks its fence before its feed commit;
    - posting's busy message names the shared ledger lease;
    - the trial balance's response drops `version`.
  - **Waiting for your sign-off,** with a separate approval for the stand-in job.
- **2026-10-02.** You signed off Stage 8a ([docs/stages/stage-8a-signoff.md](../stages/stage-8a-signoff.md)). Its gate passed 118 of 118 criteria on the first run.
  - **The sign-off accepts** ADR-0019 (configuration snapshots in Postgres, a written ledger frozen until the Spark job's correction state, the ERP as the book of record), the contract changes, and three changes to 7b's behaviour (the trial balance drops `version`, posting's busy message, the posting job's second fence check).
  - **The stand-in is approved:** `journal_ref` runs the reference engine as a job, on sample volumes, until the rule compiler exists.
  - **Next, by your choice:** 8b on sample inputs (the close checklist, flux analysis and the soft close).
- **2026-10-02.** Stage 8b was planned on sample inputs ([docs/stages/stage-8-plan.md](../stages/stage-8-plan.md), approved automatically): a soft close, the close checklist and flux analysis as configuration, and a hard close that waits for the checklist.
  - **The soft close** stops automated entries (recurring journals, the journaling engine) but keeps manual adjustments and posting.
  - **Tasks** get business-day due dates from the entity's calendar, read through a narrow lookup like 7c's rates. They have dependencies, assigned preparers and reviewers, automatic system checks, and reminders.
  - **Flux explanations** are bound to the server's amounts, and the review is bound to a hash of the flux lines.
  - **`PeriodCloseWorkflow`** is a Postgres state machine with idempotent scheduler tasks, since Temporal was dropped (ADR-0007).
- **2026-10-03.** Stage 8b was built on sample inputs, and its gate passed 128 of 128 criteria on the first run ([docs/stages/stage-8b-report.md](../stages/stage-8b-report.md), [ADR-0020](../adr/0020-the-period-close.md)).
  - **A soft close** stops automated entries (recurring journals, automatic reversals, the journaling engine) and keeps manual adjustments and posting.
  - **The close checklist and flux rules are configuration.**
    - **Tasks** have business-day due dates from the entity's calendar (a narrow lookup like 7c's rates), dependencies, assigned preparers and reviewers or system checks, and reminders.
    - **Flux explanations** are bound to the server's amounts, and the review to a hash of the lines.
  - **`PeriodCloseWorkflow`.** A period with a checklist is soft-closed first, and closes only when every task is done and no GL check predates the ledger's last change. On the stack, US01's July is soft-closed, reviewed and closed.
  - **Waiting for your sign-off.**
- **2026-10-03.** You signed off Stage 8b ([docs/stages/stage-8b-signoff.md](../stages/stage-8b-signoff.md)). Its gate passed 128 of 128 criteria on the first run.
  - **The sign-off accepts** ADR-0020 (the soft close, the close checklist and flux rules as configuration, flux explanations bound to the server's amounts, and the hard close), and the contract changes (the `close_checklist` and `flux_rules` kinds, `soft_closed`, and the `close.prepare` and `close.review` actions).
  - **The repository moves to GitHub,** and development continues in Claude Code in the cloud. [CLAUDE.md](../../CLAUDE.md) carries the standing rules.
  - **Next, by your choice:** 8c on sample inputs, account reconciliations.
