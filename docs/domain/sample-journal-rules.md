# Sample journaling rules (for testing only)

**These are sample rules, not your company's accounting.** They let the platform be built and tested on the generated marketplace until your controller's answers, chart of accounts and dimensions arrive ([stage-6-inputs.md](stage-6-inputs.md)). Every sample configuration id starts with `sample_`.

In production, the accounting team writes its own rules as configuration, and each rule change goes through maker-checker approval like any other. The samples show the shapes they will use:
- policy answers;
- a chart of accounts and dimensions;
- account mappings;
- event types;
- automated journal definitions.

They live in `close_contracts/samples/journaling.py`. Each is plain data that the API accepts.

## How the pieces fit

1. **Event types** say how landing rows become accounting events. An event type can look up another table by its key, for example an order event looking up its order. Each event type declares its fields (`amount_<role>` in minor units, `attr_<name>`) and its identities, such as "card plus store credit equals price plus buyer fee plus shipping plus tax less incentive".
2. **Automated journal definitions** (BlackLine's term) say which event types they journal, when (a condition), and their journal lines. Each line has a side, an account, an amount, dimensions and its own condition.
3. **Account mappings** pick an account from a key, such as a policy switch or an attribute, so a rule never hard-codes a choice that policy makes.
4. **The policy answers** (per program: trigger, model, shipping, incentives; and three presentation switches) are visible to every rule as `policy_*` fields.

## The sample policy answers

| Program | Revenue and seller payable at | Model | Shipping | Buyer incentives |
|---|---|---|---|---|
| Verified marketplace | Verification passed | Agent | Net, as agent | Contra revenue up to the revenue from the buyer on that order, then marketing |
| Direct-ship | Shipped | Agent | Net, as agent | Contra revenue |
| Consignment | Shipped | Agent | Net, as agent | Marketing |
| Listings | Delivered | Agent | Net, as agent | Contra revenue |

The presentation switches:
- money held by payment providers is a cash equivalent;
- processing fees are a cost of revenue;
- lost chargebacks are charged to a reserve.

## The sample entries

| Event | Debit | Credit |
|---|---|---|
| Order captured | PSP clearing (card), stored value (store credit) | Sales tax payable (tax), buyer deposits (card + store credit − tax) |
| The program's trigger (agent) | Buyer deposits; incentives (contra revenue and/or marketing, by policy) | Seller payable (price − seller fee − processing fee), commission, processing-fee revenue, buyer-fee revenue, shipping (revenue if gross, cost recovery if net) |
| The program's trigger (owned inventory) | Buyer deposits; cost of goods sold (the seller's net); incentives | Product revenue (price), seller payable (the seller's net), buyer-fee revenue, shipping |
| Cancellation or failed verification, before the trigger | Buyer deposits, sales tax payable | PSP clearing (refund), stored value (store credit returned) |
| The same, after revenue at payment | Sales tax payable; the trigger entry reversed | PSP clearing (refund), stored value |
| Partial refund | Refunds and concessions (contra revenue) | PSP clearing, or stored value when refunded as store credit |
| Clawback (full refund after the seller was paid) | Seller payable (the seller's net), refunds and concessions (the rest) | PSP clearing (refund) |
| Seller penalty | Seller payable | Penalty revenue |
| Processing fee taken from the settlement | Processing fees (cost of revenue or operating expense, by policy) | PSP clearing |
| Processing fees invoiced | Processing fees | Fees payable |
| Chargeback lost / won back | Chargeback reserve or loss (by policy), dispute fee / PSP clearing | PSP clearing / the reserve or loss, dispute fee |
| Provider payout | Cash | PSP clearing |
| Seller payout completed | Seller payable | Payout provider clearing |
| Payout provider funded | Payout provider clearing | Cash |
| Shipping label bought | Shipping expense | Carrier accrual |
| Carrier invoice line | Carrier accrual (transportation) or shipping expense (corrections) | Carrier payable |
| Sales tax remitted | Sales tax payable | Cash |

"Revenue from the buyer on that order" is the sample's cap on contra revenue. It is the buyer fee, plus shipping when shipping is booked gross, plus the price when the platform owns the inventory. The policy sheet's option caps at the revenue earned from the buyer, which can mean all their orders to date. The sheet now asks your controller which period and which revenue the cap uses.

## Rules at the edges

- **Zero and negative amounts.** A line whose amount is zero isn't written. A negative amount is a data problem: its absolute value posts to suspense on the opposite side, recording the intended account.
- **Missing accounts.** A mapping key with no account posts to suspense, recording the key it looked for.
- **Dimensions.** A missing required dimension (program on revenue accounts, provider on PSP clearing) posts to suspense.
- **Broken identities.** An event that breaks an identity is balanced to suspense.
- **Configuration errors.** An event whose identities hold but whose lines don't balance is a configuration error, and its event type stops in that run.
- **FX.** Each line is converted to the entity's currency at the day's spot rate. The rounding residual goes to FX rounding, one line per event.
- **Periods.** An event dated in a closed period posts to the first open period, as a prior-period adjustment.

## What the sample leaves out

- **Realized FX on cross-border settlements.** It is booked from Stage 5's cross-currency matches. Until then, those amounts stay in PSP clearing in their own currency.
- **Revaluation, breakage and escheat, and estimating the chargeback reserve.** These come in later slices.
- **A cumulative incentive cap.** Capping at everything a buyer has bought to date needs a running revenue balance per buyer. The sample caps each order on its own until your controller answers the question on the sheet.
