# BlackLine concept map

This map shows where each BlackLine Transaction Matching and Journal Entry concept lands in our platform, so the accounting team can move the configuration across and check that nothing is dropped. BlackLine facts come only from `reports/BlackLine capabilities and API deep dive.md` and its research notes, and our side comes mainly from the "Engine design" and "Marketplace domain model" sections of `docs/plan/ROADMAP.md`, with the rest of the roadmap supplying API names, decisions and the stage that builds each piece. Where the roadmap has no counterpart, a row says "not planned yet" with the stage it would most naturally join, or "out of scope".

The confidence column has three labels, so "public" means BlackLine's own documentation or API specs confirm it, "inferred" means the report reasons it out but no source states it, and "confirm from your tenant" means BlackLine does not publish it, or only a partner does, so the answer sits in your configuration. When a concept is public but its settings are not, the settings get their own row or a "confirm from your tenant" note, and every tenant item comes back as a checklist at the end.

## Transaction Matching

BlackLine's chain runs from Import Definition to Data Source to Match Set to pass rules, each result carries a `MatchType`, and In-Transit Definitions turn what is left into reconciliation items. The API exposes these containers, down to each pass rule's name and type, but not the conditions inside the rules. Our chain keeps the same shape, so an export maps across object by object, and Stage 12 loads it through BlackLine field-name aliases on our config models.

| BlackLine concept | What it is | Ours | Confidence | Notes |
|---|---|---|---|---|
| Import Definition and Data Source | Loads rows into a Data Source, optionally on a schedule and with pre- and post-process procedures | `DataSource`, mapping a landing table through typed DSL expressions into `match.items` with lineage (Stage 3) | public | |
| Data source and match set settings | Columns, `tableType`, display and subtotal fields, filters | Typed mapping and filters (Stage 3), subtotals and saved views (Stage 5) | confirm from your tenant | Procedures and display fields are not planned yet (Stages 3 and 9) |
| Match Set and groups | Data sources and pass rules bound to one entity, account, team, preparer and approver, typed `matching` or `intercompany`, nestable in groups | `MatchSet` with sides A, B or C, filters, owners, schedule, group hierarchy and the same two types | public | The account binding is not planned yet (Stage 8) |
| Pass rule | Holds the matching criteria, automatic or suggested | `PassRule` with outcome `auto` or `suggested` and kind `1:1`, `1:M`, `M:1`, `M:M`, `self_net` or `chain` | public | BlackLine confirms only the first four kinds |
| Waterfall behavior | Automatic matches are final, suggested pairs stay open to later rules | Rules run by `sequence`, and auto rules supersede pending suggestions | inferred | |
| Pass-rule conditions and order | Fields, operators, group keys, filters, sequence | Typed DSL conditions and a `sequence` per rule | confirm from your tenant | Not in the API, and some customers have more than 100 rules |
| Tolerances | A date window ("within 30 days") and absorbing bank-fee or FX variances | Amount, date and currency tolerances per rule | public | Configurable amount tolerances come only from a partner |
| Tolerance values and arithmetic | Your actual values and how they apply | Per-rule settings, and a `difference` policy that can emit an adjustment | confirm from your tenant | Oracle ARCS semantics fill the gaps, see below |
| `MatchType` | Eight values (`automatic`, `suggested`, `manual`, `unloaded`, `suspended`, `automaticAndManual`, `partial`, `suggestedAndPartial`) | Group `match_type` (`automatic`, `suggested`, `manual`, `partial`) and a group `status` | public | Meanings are not public, so confirm from your tenant before equating the two `partial` values |
| Matching Engine job | Runs a match set now, at a start time or on a schedule, tracked by `jobProfileId` | `POST /match-sets/{id}/runs` returns a `run_id`, scheduled or on demand, one writer per match set (Stage 5) | public | |
| Manual match and approval | Manual matches, optionally through workflow approval | Match, confirm, reject and unmatch commands (Stage 3 job, Stage 5 API), with preparer and approver roles under SoD | public | Unmatch and match with a difference are not public |
| Exceptions and reason codes | Flagged unmatched transactions, routed with SoD and organized by user-defined reason codes | `/exceptions` with assignee, reason code, comment and write-off (Stage 5) | public | Confirm your code list, statuses and thresholds from your tenant |
| Items and adjusting journals | "Create Item" sends adjustments or timing items to a reconciliation, and adjusting journals come in bulk by rule | A write-off emits an `adjustment_request` that journaling books exactly once (Stages 3, 5 and 6) | public | |
| In-Transit Definition | Creates reconciliation items for a period from chosen match types and a date window | `timing_rules` mark residual items as in transit with an expected clear date (Stage 3), feeding Stage 8 reconciling items | public | BlackLine can also select matched types, so check yours |

## Journal Entry

BlackLine builds journals from five constructs (template, one-off journal, master, automated definition and batch) and calls preparation and approval "certification", and its API publishes the meta-model but not each tenant's field lists or status values. Our Stage 6 journaling engine, which turns accounting events into journal lines through posting rules, has no BlackLine counterpart in the report, so every row below lands in the Stage 7 journal workflow unless it says otherwise. For us `posted` means committed to the Databricks GL tables, and D19, which you confirm by the Stage 5 gate, keeps an ERP as the legal book of record by default.

| BlackLine concept | What it is | Ours | Confidence | Notes |
|---|---|---|---|---|
| Journal Template | A journal type with its own header and line columns, held as `dynamicData` | `JournalTemplate` with typed columns, and journals store `dynamic_data` per template version | public | Field lists and settings like `maxNumLines` are tenant data, so confirm from your tenant |
| Column attributes | `required`, `readOnly`, `isHidden`, `defaultValue`, `defaultSql`, `balanceBy`, `creditDebitGroupNum`, `validateField`, `restrictNegativeValues` and more | The same validation attributes, of which the roadmap names `required`, `readOnly`, `defaultValue`, `balanceBy`, `validateField` and `restrictNegativeValues` | public | Non-validation ones such as `defaultSql` and `isHidden` are not planned yet (Stage 7), and what `balanceBy` and `creditDebitGroupNum` do is not public |
| Manual journal and Excel import | Keyed journals, or an Excel import into the Journals Grid | `/journals` with CSV and XLSX import, and a grid in Stage 9 | public | |
| Journal Master | Recurring journal with `frequencyCode`, preparer, approver and `canAutoCertify` | `RecurringJournal` on a schedule, auto-certified only where policy allows | public | `frequencyCode` values are not public, so confirm from your tenant |
| Automated Journal Definition | Journals built from data, typed `Matching`, `Items` or `ICH`, from a data source or match set, with grouping and summarizing | `AutoJournalDefinition` from match adjustments, exception write-offs or accounting events | public | `Items` is not planned yet (Stage 8), and intercompany `ICH` is out of scope |
| Journal Batching | Related journals certified and posted together, never partially | `JournalBatch`, all or none | public | |
| Certification actions | Certify, validate, decertify, reject, and bulk request or cancel of decertification | Submit, approve, reject, recall, post, reverse | public | What decertify does is not public, so confirm from your tenant |
| Certification-state counters | `notPrepared`, `prepared`, `approved`, `reviewed`, `accountReviewed`, `accountManaged`, `financialReviewed`, `autoCertified`, `rejected` | `draft → submitted → approved(L1..Ln) → posted → erp_confirmed`, plus `rejected`, `recalled`, `reversed` | public | Counter names, not a status list |
| Journal status values | Values of `finalCertificationState`, `statusName`, `postingStatus`, `certificationStatus` | Our states above | confirm from your tenant | Read them off your journal history |
| Approval routing | By amount thresholds, roles and materiality, with auto-certify for reasonable entries | Approval matrices (L1..Ln), thresholds, delegation, reminders, and an AS 2401.61 risk score | public | |
| Approval levels and SoD settings | Your levels, thresholds and auto-certify rules | Approval-matrix and SoD config under maker-checker | confirm from your tenant | Eight role slots on a batch suggest up to eight workflow roles, which is inferred |
| Validation | Template field rules, and live ERP checks for SAP | Template attributes, plus Stage 6 checks on balance, accounts, dimensions and open period | public | Live ERP checks are not planned yet (Stage 7) |
| Supporting documents | Files on a journal, reused through its linked reconciling item | `/journals/{id}/attachments` | public | Reuse across reconciliations is not planned yet (Stage 8) |
| Posting and document numbers | Posts through ERP connectors or web services, and gets the document number back through `scheduleupdatepostingstatus` | `posted` commits to the Databricks GL, and `POST /posting/status` takes BlackLine's write-back fields (`documentId`, `statusCode`, `statusMessage`, `erpDocUrl`) and sets `erp_confirmed` | public | Direct ERP connectors are not planned yet, and Stage 7 tests a stub ERP feed |
| Reversal | "Journal automatically reversed and document numbers retrieved", from one e-book | `reversed` state, reverse action and auto-reversals | public | Flag, date and approval are not public, so confirm from your tenant |
| Links to reconciliations and tasks | Journals as children of reconciliation items, masters on tasks, tasks closed on posting | Not planned yet (Stage 8) | public | |

## Data model and integration

Two names mean different things on each side. In BlackLine an "item" is a reconciliation item, while our `match.items` are the source rows being matched (BlackLine calls them transactions), so BlackLine items map to our Stage 8 reconciling items. The report infers that a BlackLine account is a reconciliation unit rather than a GL account, and "Key vs Non-Key" there is an account attribute, not the key segments that identify the account, so I think the unit maps best to our entity, account and dimension set, which your account keys will confirm or not.

Getting history out is the hard part of leaving, because the API is write-mostly, so it has to come through report exports, the journal endpoints, the supporting-document archive (GCP instances only) and your data-return rights, before a deletion that BlackLine's MSA caps at 180 days after termination. Stage 12 covers that extraction and a parallel run of two or more closes.

| BlackLine concept | What it is | Ours | Confidence | Notes |
|---|---|---|---|---|
| Entity and team | Entity tree by entity type, plus a team hierarchy that grants product access | `/entities`, with every role scoped by entity | public | Child-entity access and teams are not planned yet (Stage 4) |
| Account | Key vs Non-Key flag, risk rating, certification frequency and six reference labels | `ref.accounts`, `dimensions`, `account_mappings`, and reconciliation attributes in Stage 8 | public | |
| Account key | Entity identifier, then account and further "Key" segments | Entity, account and dimension set | confirm from your tenant | Column names come from a partner |
| Period | End date, frequency, closed flags for period, balances, tasks and intercompany | `/periods` (open, soft-close, close, lock), fenced, with late events booked to the first open period | public | Task flag is not planned yet (Stage 8), intercompany flag is out of scope |
| Balances and currency rates | GL, subledger, bank and multi-currency feeds, with rates for every period | `ledger.balances`, `gl_rollup_lines`, trial balance (Stage 8), `ref.fx_rates` | public | |
| Reconciling item | Five reference fields, classed as list component, timing item or required adjustment | Reconciling items (Stage 8) | public | |
| Users and roles | Entity-driven and account-driven roles per product | Entity-scoped roles with SoD blocks (Stage 4) | public | |
| Data ingestion and import status | `PUT /dataingestion/...` JSON or headerless TSV, SFTP and `message-version` layouts, with import IDs, record errors and `fileSha256` | Out of scope, since data lands in Databricks landing tables, and our runs report control totals through `GET /runs/{id}` | public | Push feeds need re-pointing at landing |
| Matching configuration API | Reads match sets and definitions, starts imports and jobs | `/data-sources`, `/match-sets` and rules under maker-checker (Stage 5) | public | |
| Getting data out | Report exports and journal reads, but no read-back of match results, balances or reconciliations, and no webhooks | Read APIs for items, groups, runs, journals and GL lines, plus webhooks | inferred | The absence comes from reading all 21 specs |
| Authentication and idempotency | Password grant with a user's API key, `bl-roleid`, `bl-requestid` on a few endpoints | Client-credentials API clients, `Idempotency-Key` on every POST | public | |

## Gaps we close on purpose

Where BlackLine is silent on pass rules, the roadmap borrows Oracle Account Reconciliation (ARCS) semantics, which the report treats as a design reference rather than evidence about BlackLine, and where the report finds no vendor documenting the behavior, as with unmatch, match with a difference, net-to-zero in one source and rule testing, the roadmap makes its own call. These are the rows where our results may differ from BlackLine's, so I would watch them most closely in the Stage 12 parallel run.

| What BlackLine does not publish | What the roadmap decides | Borrowed from |
|---|---|---|
| Comparison operators | Equals, normalized equals, `contains` with a minimum length, starts and ends with, RE2-subset regex, fuzzy only after blocking (Engine design) | Oracle ARCS for `contains`, the rest ours |
| Tolerance arithmetic | Exact, absolute, percent or capped percent in integer arithmetic, with calendar or business-day windows that can be asymmetric (Engine design) | Oracle ARCS |
| Precedence and ties | `unique_only` by default, and a group that needed the ID tie-break is flagged ambiguous and downgraded to suggested unless an approved rule allows it (Engine design) | Oracle ARCS "No Ambiguous", stricter on ties |
| Many-to-many and netting | `M:M` needs an exact key, one-to-many search is bounded with an explicit `search_limit_reached`, and `self_net` groups items that sum to zero within tolerance (Engine design) | Oracle ARCS for the exact key, the rest ours |
| Match with a difference, source corrections | Manual match with a difference and reason code, and a corrected source row breaks its group and cancels its adjustment (Backend API, Engine design) | Ours |
| Rule testing | `/rules:validate` and a dry-run simulate that commits nothing (Backend API) | Ours |
| Carry-forward and duplicates | Open items stay in the pool with no age cut-off, and deduplication by source identity lets duplicate transactions surface as breaks (Engine design) | Ours |
| Preparer and approver separation | SoD blocks self-approval, self-activated rules, and preparing and posting the same journal, and Gate 7 tests every state transition (Backend API) | Ours |
| Multi-currency journals | Events balance per currency in integer minor units, with exact FX, half-even rounding and a rounding line per event (D9) | Ours |
| Pre-built match sets | Marketplace templates (12 of them) keyed on real PSP, bank and carrier formats (Marketplace domain model) | Ours |

## What to pull from your tenant before migrating

The roadmap asks for the matching export at Stage 3 and the journal configuration at Stages 6 and 7. Everything below is a read-only call or an admin-screen capture, since `POST /v2/matching/...` calls, journal certification routes, `PUT /dataingestion/...` and other write calls change your tenant. Journal calls need a `bl-roleid` header (from `GET /v1/roles`), and matching calls need a Matching Business or System Admin user.

For Transaction Matching:

- [ ] Every match set and everything the API returns for it, from `GET /v2/matching/match-sets?ShowAll=true` and `GET /v2/matching/match-sets/{matchSetId}`.
- [ ] Import Definitions with `tableType`, `showImport` and process flags from `GET /v2/matching/import-definitions`, plus what each procedure does.
- [ ] Data source columns and types, and each match set's display fields, subtotal fields, filters and saved views.
- [ ] Pass-rule conditions and order, with group keys and filters, as screenshots from the system administrator screen under Products, then Match Sets.
- [ ] Tolerance values, date units and direction, currency handling and what happens to the difference.
- [ ] What `unloaded`, `suspended`, `partial`, `suggestedAndPartial` and `automaticAndManual` mean in your data.
- [ ] In-Transit Definitions from `GET /v2/matching/in-transit-definitions`, with their match types and date windows.
- [ ] Reason codes, exception statuses and aging, write-off thresholds, manual-match approvals, unmatch permissions and match-with-difference behavior.
- [ ] Engine and import schedules, and whether you use SAP open-item clearing, which nothing in the roadmap replaces.

For Journal Entry:

- [ ] Templates and columns from `GET /v1/journaltemplates` and `GET /v1/journaltemplates/{templateId}/columns`, plus derivation logic the API does not show.
- [ ] Journal Masters with `frequencyCode` values, Automated Journal Definitions with grouping, summarizing and `creationPeriodType`, and certification reason codes, from the `/masters`, `/automated-journal-definitions` and `/certification/reasoncodes` sub-routes once we confirm their full path and method in the spec.
- [ ] Status values, read off your history with `GET /v1/journals?periodId=…` and `GET /v1/journals/{journalId}`.
- [ ] Approval levels, routing thresholds, auto-certification rules, SoD settings and delegation.
- [ ] Validation rule types, and whether each fires at save, submit, approve or post.
- [ ] Reversal practice, ERP posting settings per connection, and whether you use split journals (`/{journalId}/splitjournals`), which are not planned yet (Stage 7).

For the organization:

- [ ] Entities, periods, roles, users and teams from `GET /v1/mdm/entities`, `GET /v1/periods`, `GET /v1/roles`, `GET /v1/users`, `GET /v1/users/roles-products` and `GET /v1/teams/hierarchy`, which also give us the SoD baseline.
- [ ] Which Verity features you use, since leaving BlackLine gives them up and they are out of scope for us.
