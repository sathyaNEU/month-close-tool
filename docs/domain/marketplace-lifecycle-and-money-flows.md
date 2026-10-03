# Marketplace lifecycle and money flows

This document describes the marketplace the platform is built for, which is who takes part, how an order moves through each program, where money moves and when, which accounting event each step emits, and which match-set template reconciles each flow. It is a design input built from public facts (the roadmap's marketplace domain model), so it makes no claim about how any particular company books its transactions, and every choice that changes the accounting is a switch on the [accounting-policy sheet](accounting-policy-sheet.md) that your controller signs off.

## Who takes part

The buyer pays for an item, and the seller (an individual or a business, with a fee tier that depends on their seller level) is owed the sale price less fees once the program's trigger is reached. The platform sits in the middle and, for verified programs, also runs the authentication center that inspects each item before it goes to the buyer. Around them sit the payment service providers, which capture the buyer's payment and settle it to the platform's bank account (the generator produces Stripe-shaped and Adyen-shaped data, and the templates also carry PayPal and Braintree keys), the payout providers that pay sellers (Hyperwallet-like for sellers outside the US, and PayPal Payouts-like), the banks whose statements arrive as BAI2, camt.053 or MT940, the carriers that bill shipping labels (FedEx-like), the tax engine that calculates and commits sales tax (AvaTax-like) together with the tax authorities it is remitted to, and the stored-value ledger that holds store credit and gift cards.

Each legal entity keeps its books in one functional currency, and the generator covers three of them, a US entity in USD, an EU entity in EUR and a UK entity in GBP. A buyer can pay in a presentment currency that differs from the entity's currency, which is where FX differences come from.

## Programs

A program is a sales channel, and each program carries its own policy settings. The defaults below are the starting point for the policy sheet, not decisions.

| Program | How an order flows | Default trigger for revenue and the seller payable | Default model |
|---|---|---|---|
| Verified marketplace | The seller ships to the authentication center, the item is inspected, and the platform ships it to the buyer | Verification passed | Agent (net commission and fees) |
| Direct-ship | A verified seller ships straight to the buyer, without inspection | Shipped | Agent |
| Consignment (stored inventory) | The seller's items are already stored at the platform's warehouse and ship from there when sold | Shipped | Agent |
| Listings | The seller ships straight to the buyer, and the seller is paid after the carrier confirms delivery | Delivered | Agent |

The trigger can be set to any of payment, verification passed, shipped, delivered or return window closed, and the model can be switched to owned inventory, where the platform owns the item it sells and books revenue gross with cost of goods sold. That is how a platform that buys stock outright, or takes title during authentication, would be configured.

## The order lifecycle

An order is placed at checkout, where the buyer's total is the item price plus buyer fees, shipping and sales tax, less any incentive, and part of it may be paid with store credit (split tender). The payment service provider captures the card or wallet part (`PAYMENT_CAPTURED`), and a later partial capture or capture correction emits `CAPTURE_ADJUSTED`, while the incentive emits `INCENTIVE_APPLIED` and the tax engine commits the tax document (`TAX_COMMITTED`).

In the verified marketplace the seller then ships the item to the authentication center, which passes it (`VERIFICATION_PASSED`) or fails it (`VERIFICATION_FAILED`). A failed item cancels the order (`ORDER_CANCELLED`), the buyer is refunded (`REFUND_ISSUED`), and the seller is usually charged a penalty (`SELLER_CHARGE_ASSESSED`), which is netted from their next payout or charged to their card. A passed item, or any order in the other programs, ships to the buyer (`SHIPPED_TO_BUYER`), the carrier confirms delivery (`DELIVERY_CONFIRMED`), and once the return window has passed without a return the order is final (`RETURN_WINDOW_CLOSED`).

Two things can reopen an order after that. A refund can be issued to the buyer's original payment method or as store credit, even after the seller has been paid, in which case the platform claws the amount back from the seller. A dispute (a chargeback) can arrive weeks later, and it moves through its own lifecycle of opened, fee charged, and then won, lost or a late win after a loss (`DISPUTE_EVENT`), with a reserve covering the expected losses.

## Where the money moves

1. The buyer's payment. The payment service provider captures the order total in the presentment currency, and the platform now has a receivable from the provider, held in a PSP clearing account until the provider settles.
2. Provider settlement. The provider batches a day's captures, net of refunds, disputes and (when fees are netted) its own processing fees, into a payout to the platform's bank account (`PSP_SETTLEMENT_RECEIVED`). Fees that are invoiced separately arrive later as a fee invoice (`PSP_FEE_BILLED`).
3. The bank credit. The payout lands on the platform's bank statement as a credit, usually one or two business days after the payout date, with the provider's reference somewhere in the statement line's reference or text fields.
4. The seller payable. When the program's trigger is reached, the seller's net proceeds (the sale price less the transaction fee, the processing fee and any penalties) become a payable to the seller, which sits in a seller-payable liability until paid.
5. The seller payout. A daily cycle batches each seller's payables into payout items (`SELLER_PAYOUT_INITIATED`) through a payout provider, which the platform funds with a transfer from its bank account, and each item later reports paid, failed, returned or unclaimed (`SELLER_PAYOUT_RESULT`).
6. Shipping. The platform buys a label at a quoted price when the order ships, and the carrier's invoice bills the actual charge later, sometimes with weight or address corrections on top (`CARRIER_CHARGE_BILLED`).
7. Sales tax. The platform collects tax as a marketplace facilitator on the tax engine's committed documents and remits it to the tax authorities (`TAX_REMITTED`), so tax is a pass-through that never counts as revenue.
8. Stored value. Store credit from refunds and gift card sales create a stored-value liability, which falls as buyers redeem it at checkout and, depending on policy and jurisdiction, through expiry, breakage or escheat to the state (`STORED_VALUE_EVENT`).
9. Currency. When a capture, settlement or payout crosses currencies, the difference between the booked rate and the settled rate is realized FX (`FX_REALIZED`), and foreign-currency balances still open at period end are revalued (`FX_REVALUED`).

## Which template reconciles each flow

Every money flow above has at least one match set that proves it happened as expected, and the keys each template matches on come from the real provider, bank and carrier formats (the roadmap has the full key list). Stage 3 builds templates 1, 2, 3, 6, 9 and 10, and the rest follow in Stages 5 and 8.

| Template | Reconciles | Flow above |
|---|---|---|
| 1. Order to PSP capture | Every order was captured, for the right amount in the presentment currency | 1 |
| 2. PSP transactions to PSP payout | Each provider payout equals the sum of the transactions it settles, net of fees | 2 |
| 3. PSP payout to bank credit | Each payout arrived at the bank, inside a value-date window | 3 |
| 4. Refund chain | A refund reached the buyer and the matching seller adjustment or transfer reversal exists | Lifecycle |
| 5. Dispute lifecycle | Every dispute step, the reserve and any recovery from the seller line up | Lifecycle |
| 6. Seller payable to payout item | Each payable was paid out, with its status | 4 and 5 |
| 7. Payout-provider funding to bank debit | Each funding transfer left the bank | 5 |
| 8. Stored-value roll-forward | Issued, less redeemed, expired and breakage, equals the liability | 8 |
| 9. Shipping label to carrier invoice | Each label was billed as quoted, or the difference is explained | 6 |
| 10. Tax collected to tax engine to remittance | Tax collected equals committed documents, and both equal what was remitted | 7 |
| 11. PSP fee accrual to fee invoice | Accrued processing fees equal the provider's fee invoice | 2 |
| 12. Clearing-account zero-out | The PSP clearing account and the seller-payable subledger tie to the general ledger | 1, 2 and 4 |

## The breaks the data will contain

A reconciliation platform is only proven by the cases that do not match cleanly, so the generator injects each of these at a configured rate and the answer key records the outcome the engine must reach.

| Break | Where it comes from |
|---|---|
| Timing | A payout cycle, a bank value date or a month end falls between the two sides |
| Partial capture or partial refund | One order has several captures or refunds instead of one |
| Split tender and store credit | Part of the order total is paid with store credit, so the card capture is smaller than the order |
| FX difference | The settled amount differs from the booked amount because the rate moved |
| Fees netted versus invoiced | Processing fees come out of the payout on one side and arrive as a separate invoice on the other |
| Chargeback and late win | A dispute debits a payout, and sometimes the money comes back after a loss |
| Duplicate | The same source row arrives twice, which must be surfaced and never silently dropped |
| Rounding and format | BAI2 amounts carry no decimal point and MT940 uses a decimal comma, so a careless parser is off by a factor of 100 |
| Clawback | A refund is issued after the seller was already paid |
| Unclaimed or returned payout | A seller payout fails, bounces or is never collected |
