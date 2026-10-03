# What Stage 6 needs from you

Stage 6 builds the journaling engine: the accounting entries the platform posts for every marketplace event. It needs three things from your finance team before it starts, because every posting rule depends on them.

## 1. The accounting-policy sheet, answered and signed

- **Where it is.** The sheet is [accounting-policy-sheet.md](accounting-policy-sheet.md). It asks your controller to choose, program by program, how revenue, seller payables, shipping, incentives, chargebacks, store credit, sales tax and foreign exchange are booked. Each question shows the options the platform supports.
- **What to send back.** Send the answered sheet back as a copy of the file or a scan, with the controller's name, date and sign-off.
- **Confirming it.** Add a line to [policy-sheet-status.md](policy-sheet-status.md) starting with "Confirmed", with the date, or tell me and I'll add it.

## 2. The chart of accounts

Fill in [chart-of-accounts.csv](stage-6-inputs/chart-of-accounts.csv), one row per account, and delete the two example rows. An export from your ERP or from BlackLine's account list is fine instead, as long as it has these columns or something close to them:

| Column | What it means |
|---|---|
| `account_code` | The account number as your general ledger uses it |
| `account_name` | Its name |
| `account_type` | `asset`, `liability`, `equity`, `revenue` or `expense` |
| `normal_balance` | `debit` or `credit` |
| `postable` | `Y` if entries can be posted to it, `N` for summary or header accounts |
| `parent_code` | The summary account it rolls up to, if you use a hierarchy |
| `entities` | The entities that use it (for example `US01;UK01`), or blank for all |
| `currency` | A fixed currency if the account is single-currency, or blank |
| `status` | `active` or `inactive` |
| `description` | Anything that helps choose the right account |

## 3. The dimensions

Dimensions are the extra tags on an entry beyond the account, such as program, cost center or channel.
- **Definitions.** Fill in [dimensions.csv](stage-6-inputs/dimensions.csv), one row per dimension. `required_on` lists the account types that must carry it.
- **Allowed values.** Fill in [dimension-values.csv](stage-6-inputs/dimension-values.csv), one row per allowed value.

If you don't use dimensions, say so; the engine then posts to accounts only.

## Later, for Stage 7 (not needed yet)

Your current journal-entry templates and automated journal definitions, the approval matrix and thresholds, your SoD rules for journals, and the close calendar.

## What happens next

The journaling engine and its oracle are already built and tested on a sample configuration ([sample-journal-rules.md](sample-journal-rules.md)), as you asked on 2026-09-29. When these inputs arrive:
- your answers, accounts and dimensions replace the samples, as configurations your team authors and approves through the API;
- the oracle and the invariants are rerun against them;
- nothing sample-based is activated in production.

On 2026-09-30 one question was added to the sheet's buyer-incentives section: whether the incentive cap is per order or across the buyer's orders, and which revenue it counts. The Spark journaling job waits for the rule compiler, like the Spark matcher.
