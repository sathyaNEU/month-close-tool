# Accounting-policy sheet for the marketplace programs

This sheet asks you, as controller, to choose how each marketplace program is accounted for. The platform turns every answer into configuration, so nothing here is hard-coded, and changing an answer later goes through the same maker-checker approval as any other rule change. Each question lists the options the platform supports, and where the public filings of comparable marketplaces (The RealReal, ThredUp, Etsy, eBay and Poshmark) point one way it also suggests a default, which reflects those filings and not any view of your business. Your sign-off is needed before journaling is built (Stage 6), and matching does not depend on it.

To fill it in, mark one option per program for each question, add a note wherever your policy needs more than the options allow, and sign at the end. If a program does not apply to your business, mark it "not used". The programs are described in [marketplace-lifecycle-and-money-flows.md](marketplace-lifecycle-and-money-flows.md).

## When revenue and the seller payable are booked

The trigger is the lifecycle step at which the platform books its fee revenue and the amount it owes the seller. The suggestions follow when comparable marketplaces publicly say they pay sellers (after verification, on the ship date for verified sellers, and after delivery for listings).

| Program | Payment | Verification passed | Shipped | Delivered | Return window closed | Suggested |
|---|---|---|---|---|---|---|
| Verified marketplace | [ ] | [ ] | [ ] | [ ] | [ ] | Verification passed |
| Direct-ship | [ ] | [ ] | [ ] | [ ] | [ ] | Shipped |
| Consignment | [ ] | [ ] | [ ] | [ ] | [ ] | Shipped (our assumption, since no public source states it) |
| Listings | [ ] | [ ] | [ ] | [ ] | [ ] | Delivered |

Notes:

## Agent or owned inventory

As an agent the platform books only its net commission and fees, and with owned inventory it books the full sale price as revenue with the item's cost as cost of goods sold.

| Program | Agent (net) | Owned inventory (gross and COGS) | Suggested |
|---|---|---|---|
| Verified marketplace | [ ] | [ ] | Agent |
| Direct-ship | [ ] | [ ] | Agent |
| Consignment | [ ] | [ ] | Agent |
| Listings | [ ] | [ ] | Agent |

Notes:

## Shipping charged to buyers

Shipping can be booked net, as an agent passing the carrier's cost through, or gross, as shipping revenue with the carrier charge as a cost. The public filings we used do not point one way, so there is no suggestion here.

| Program | Net as agent | Gross | Suggested |
|---|---|---|---|
| Verified marketplace | [ ] | [ ] | Your current policy |
| Direct-ship | [ ] | [ ] | Your current policy |
| Consignment | [ ] | [ ] | Your current policy |
| Listings | [ ] | [ ] | Your current policy |

Notes:

## Buyer incentives

Promotions and discounts the platform funds can reduce revenue, reduce revenue only up to the revenue earned from that buyer with any excess going to marketing expense, or go to marketing expense entirely.

| Program | Contra-revenue | Contra-revenue up to cumulative revenue, then marketing | Marketing expense | Suggested |
|---|---|---|---|---|
| Verified marketplace | [ ] | [ ] | [ ] | Contra up to cumulative revenue, then marketing |
| Direct-ship | [ ] | [ ] | [ ] | Contra up to cumulative revenue, then marketing |
| Consignment | [ ] | [ ] | [ ] | Contra up to cumulative revenue, then marketing |
| Listings | [ ] | [ ] | [ ] | Contra up to cumulative revenue, then marketing |

If you choose "contra-revenue up to cumulative revenue, then marketing", two details decide the cap (added 2026-09-30):
- **Period.** Is the cap the revenue earned from that buyer across all their orders to date, or only on the order that carries the incentive?
- **Scope.** Does the cap count only the revenue from the buyer (the buyer fee, shipping when booked gross, and the item price when the platform owns the inventory), or also the commission earned from the seller on the buyer's orders?

Notes:

## Presentation

These apply to the whole company rather than per program.

| Question | Options | Suggested |
|---|---|---|
| Money in transit from payment providers | [ ] Cash equivalent  [ ] Accounts receivable | Your current policy |
| Payment processing fees | [ ] Cost of revenue  [ ] Selling, general and administrative | Your current policy |
| Chargeback losses | [ ] Through a reserve  [ ] A transaction-loss line when they occur | Through a reserve |

Notes:

## Failed authentication

When an item fails authentication in the verified marketplace, the order is cancelled, the buyer is refunded, and the seller is usually charged a penalty.

| Question | Your answer |
|---|---|
| Penalty charged to the seller (a fixed amount, a percent of the sale price, or both, per currency) | |
| How the penalty is booked (other revenue, or a reduction of a cost line) | |
| Who bears the cost of returning the failed item to the seller | |
| Is the penalty netted from the seller's next payout, or charged to their card | |

## Chargebacks and the reserve

| Question | Your answer |
|---|---|
| How the reserve is sized (a percent of gross merchandise value, a historical loss rate, or another basis) | |
| When the platform tries to recover a chargeback from the seller, and when it absorbs the loss | |
| How a late win (money returned after a lost dispute) is booked | |

## Carrier charges

| Question | Your answer |
|---|---|
| Is the label booked at the quoted price with the difference booked when the carrier invoices, or is the difference accrued | |
| Who bears weight and address corrections (the platform or the seller) | |

## Store credit and gift cards

| Question | Your answer |
|---|---|
| Breakage method: [ ] in proportion to redemptions  [ ] when redemption becomes remote  [ ] no breakage | |
| Do store credit and gift cards expire, and after how long | |
| Which jurisdictions require escheat, with their dormancy periods (usually from your legal team) | |

## Sales tax

The platform treats sales tax as a marketplace facilitator's pass-through, so tax collected is a liability until remitted and never counts as revenue. Please confirm this for every jurisdiction you collect in, or list the exceptions.

| Question | Your answer |
|---|---|
| Facilitator treatment confirmed for all jurisdictions, or the exceptions | |

## Foreign exchange

The platform converts amounts with exact decimal arithmetic and records the rate type, date and version on every converted line, and translation into a reporting currency stays in consolidation, outside this platform.

| Question | Your answer |
|---|---|
| Rate used to book foreign-currency transactions (for example the daily spot rate, and its source) | |
| Rate used for the month-end revaluation of open foreign-currency balances (for example the closing rate) | |

## Sign-off

By signing, you confirm that the answers above are the accounting policies the platform should configure for each program. Any later change is made as a configuration change in the platform, which records who made it and who approved it.

| | |
|---|---|
| Name | |
| Title | |
| Date | |
| Signature | |
