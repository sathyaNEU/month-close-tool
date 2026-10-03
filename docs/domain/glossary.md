# Glossary

These are the terms the platform, its configuration and its documents use, each with the meaning we give it. Where BlackLine uses a different word for the same thing, the [concept map](blackline-concept-map.md) has the translation.

## Marketplace and payments

| Term | Meaning |
|---|---|
| Program | A sales channel (verified marketplace, direct-ship, consignment or listings), each with its own policy settings |
| Authentication center | The platform's facility that inspects an item before it goes to the buyer in the verified marketplace |
| Seller level, fee tier | The seller's standing, which sets their transaction fee rate |
| Trigger | The lifecycle step at which revenue and the seller payable are recognized for a program (payment, verification passed, shipped, delivered or return window closed) |
| Agent model, owned inventory | Whether the platform books its net commission and fees (agent) or the full sale with cost of goods sold (owned inventory) |
| Presentment currency | The currency the buyer paid in, which can differ from the entity's functional currency |
| Functional currency | The currency an entity keeps its books in (USD, EUR or GBP here) |
| PSP | A payment service provider, such as Stripe or Adyen, which captures the buyer's payment and settles it to the platform |
| Capture, partial capture | Taking the authorized money from the buyer, in one amount or several |
| Settlement, PSP payout | The provider's batch transfer of captured funds, net of refunds, disputes and sometimes fees, to the platform's bank account |
| Dispute, chargeback | A buyer's bank reversing a payment, which moves through opened, fee, and won, lost or late win |
| Reserve | The liability held against expected dispute losses |
| Seller payable | What the platform owes a seller once the trigger is reached |
| Payout item | One payment to one seller inside a payout provider's batch |
| Clawback | Recovering money from a seller after they were paid, for example after a late refund |
| Split tender | An order paid partly by card and partly with store credit |
| Store credit, gift card, stored value | Balances buyers can spend on the platform, held as a liability |
| Breakage | Stored value the platform expects never to be redeemed, recognized as income under the policy chosen |
| Escheat | Handing unclaimed stored value to a state, where its law requires it |
| Marketplace facilitator | A platform that collects and remits sales tax on its sellers' behalf, so the tax is a pass-through |
| BAI2, camt.053, MT940 | Bank statement formats; BAI2 amounts carry no decimal point, and MT940 uses a decimal comma |
| Value date | The date money becomes available on a bank account, which can differ from the booking date |

## Matching

| Term | Meaning |
|---|---|
| Data source | A binding from one landing table to canonical items, with a typed column mapping, the source's identity key and its sign convention |
| Item, item version | One normalized row to be matched; a corrected source row creates a new version of the same item |
| Match set | A reconciliation, with its data sources in roles (A, B, C), scope filters, owners, schedule and pass rules |
| Pass rule | One step in the ordered waterfall that tries to group items, of a kind (`1:1`, `1:M`, `M:1`, `M:M`, `self_net` or `chain`), with conditions, tolerances and an outcome |
| Automatic, suggested | A pass rule's outcome: the group is committed, or it waits for a person to confirm or reject it |
| Tolerance | How far two sides may differ and still match, on amounts (exact, absolute, basis points or capped), dates (calendar or business days) or text |
| Group | A set of items the engine or a person matched, with a match type and a status |
| Ambiguity policy | How a rule decides when an item has more than one candidate (`unique_only`, `best_unique` or `greedy_total_order`) |
| Timing rule, in transit | A rule that classifies an unmatched item as expected to clear later, with an expected clear date |
| Break | An item or group that does not reconcile cleanly, such as a timing difference, a duplicate or an FX difference |
| Reason code | The explanation attached to an exception, which can drive an adjusting journal |
| Adjustment request | A request, created by a write-off rule or a person, for journaling to book a correction exactly once |
| Answer key | The generator's record of the correct groups and outcomes, which the engine is tested against |

## Journaling and the ledger

| Term | Meaning |
|---|---|
| Accounting event | A business fact with accounting impact, keyed by event ID and version, such as `PAYMENT_CAPTURED` |
| Posting rule | Versioned configuration that turns an event into balanced debit and credit lines |
| Subledger line | One debit or credit produced by a posting rule, traceable to its event and landing row |
| GL rollup | Subledger lines summarized by entity, period, account, dimensions and currency for the general ledger |
| Suspense | Where a line goes when its data is incomplete, with the intended account recorded |
| Period | An accounting month, open or closed, whose status is checked again at commit |
| Posted, ERP confirmed | A journal committed to the Databricks ledger, and optionally confirmed by the ERP with its document number |
| Maker-checker | The rule that the author of a change cannot approve it |

## Platform

| Term | Meaning |
|---|---|
| Entity scope | The entities a user may see; every query is limited to them by the engine |
| Lease, fencing token | The one-writer guarantee: a writer holds a lease, and a job whose token is no longer the highest refuses to commit |
| Manifest | The record each job run writes, with control totals, invariants and digests |
| Digest | An order-independent fingerprint of a table's rows, used to prove reruns give the same output |
| QuerySpec | The typed description of a read (table, columns, filters, sort and page size) that the read path runs |
| Configuration envelope | The wrapper every configuration version carries: status, effective dates, author, approver and content hash |
