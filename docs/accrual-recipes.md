# Accrual recipes

Worked, tested requests for the accrual patterns a small services or SaaS company
meets. Every request below ran end to end against synthetic books with the
`books` CLI. Copy one, change the names, dates and amounts, and run it.

You can build a pattern that is not listed here from the building blocks in the
next section. The engine checks every request, so an unsupported request fails
with the exact field and the next command to run. Start with
`books accrual schema` and `books accrual capabilities`; they list every request
shape. This page is a set of examples, not a second contract.

## Before you start

**Chart of accounts.** A new accrual company starts with a chart that includes
`Assets:Receivable:Accounts-Receivable`, `Liabilities:Payable:Accounts-Payable`,
`Assets:Other:Prepaid-Expenses`, `Assets:Fixed:Equipment`,
`Assets:Fixed:Accumulated-Depreciation`, `Liabilities:Other:Accrued-Liabilities`,
`Liabilities:Other:Deferred-Revenue` and `Liabilities:Other:Customer-Deposits`.
List the chart and each account's role with:

```sh
books accrual accounts --entity . --format text
```

Add anything else a recipe names. These recipes use the accounts below, which the
starter chart does not include:

```sh
books entity account-add . --account Liabilities:Other:Payroll-Tax
books entity account-add . --account Expenses:Utilities
books entity account-add . --account Expenses:Bonuses
books entity account-add . --account Expenses:Bad-Debt
books entity account-add . --account Expenses:Depreciation
books entity account-add . --account Income:Services
```

**Supporting document.** Every request that records a new amount names a stored
supporting document in `evidence_ids`. Store the document's facts once and use
the `id` it returns. `books accrual intake` records facts; it posts nothing.

```json
{
  "schema_version": 1,
  "currency": "USD",
  "source": {"namespace": "acme.docs", "account_id": "acme", "object_id": "northwind-inv-4471", "version": "1"},
  "dates": {"issued": "2026-01-31"},
  "amounts": {"gross": "600.00"},
  "observed_facts": {"description": "Northwind Hosting invoice 4471, January hosting"}
}
```

```sh
books accrual intake --entity . --input evidence/northwind-inv-4471.json
```

An estimate has no document yet. A journal or an accrual item can state an
`estimate_basis` in plain text instead (recipes 11 and 12).

**Preview, then apply.** Every change has two steps. Preview writes the exact
plan; apply posts that unchanged preview. If anything changed in between, apply
refuses and you preview again.

```sh
books accrual item preview --entity . --input request.json > preview.json
books accrual item apply --entity . --input preview.json
```

Applying the same preview twice does nothing the second time: the command returns
the original receipt with `status: already_applied`. `books accrual receipt
--entity . --id OPERATION_ID` shows a committed receipt.

**Signs and dates.**

- A settlement's bank `amount` is signed from the bank's side: money in is
  positive, money out is negative. Item and allocation amounts are positive.
- A journal posting takes `debit` or `credit` amounts, or one signed `amount`
  (debits positive). Ledger balances show assets positive and liabilities negative.
- `service_start` and `service_end` are both inclusive. A year of service that
  starts on January 15 ends on January 14.
- Amounts are strings with at most two decimals (`"1200.00"`).

## Building blocks

| Block | Use it when | Command group |
| --- | --- | --- |
| Plain journal | You know the debits and credits and need no tracking of who owes what | `accrual journal` |
| Item | An invoice, bill, estimate or credit has an open balance to track and settle | `accrual item` |
| Schedule | One amount is spread over dates: prepaid, deferred revenue, depreciation | `accrual schedule` |
| Offset | A credit, deposit or prepayment is applied to an invoice or bill | `accrual item offset-*` |
| Opening | A new company starts with balances carried in | `accrual opening` |

Recipes 1 to 5 use items and offsets, 6 to 10 use schedules, 11 to 13 use
journals and adjustments, and 14 to 16 cover opening balances and the close.

### Recipe 0: plain journal, no supporting detail needed

Use a plain journal for any entry whose debits and credits you can state. An asset
or liability account that has no registered items or schedules is `ledger_only`:
its balance shows in the reports and never blocks a close. Do not create an item
only to satisfy the books.

```json
{
  "id": "payroll-tax-jan",
  "date": "2026-01-31",
  "description": "January payroll taxes owed to the agency",
  "postings": [
    {"account": "Expenses:Taxes", "debit": "450.00"},
    {"account": "Liabilities:Other:Payroll-Tax", "credit": "450.00"}
  ],
  "evidence_ids": ["<evidence-id>"]
}
```

```sh
books accrual journal preview --entity . --input journal.json > preview.json
books accrual journal apply --entity . --input preview.json
```

The ledger shows Dr `Expenses:Taxes` 450.00 and Cr `Liabilities:Other:Payroll-Tax`
450.00 on 2026-01-31. `books accrual report --kind reconciliation` lists the
payroll tax account as `ledger_only` with the note "Ledger balance, no supporting
detail required".

A journal needs `evidence_ids` or an `estimate_basis`. Its preview can show a
`control_support_required` warning when the account name contains Receivable,
Payable, Deferred, Prepaid or Accrued. The warning is advice: the balance stays
`ledger_only` unless you register items or schedules for that account.

**When an account has detail and a journal posts to it.** Once an account has
items or schedules, its ledger balance must equal that detail. A plain journal on
such an account leaves a difference, and the reconciliation row is `unreconciled`
with a ready-to-use `action`. Either register the amount as an item or schedule, or
explain it with a support snapshot in `residual` mode. Here a journal added 75.00
of unbilled work to receivables (a plain journal on the receivables account):

```json
{
  "id": "unbilled-apr",
  "date": "2026-04-30",
  "description": "Unbilled April work",
  "postings": [
    {"account": "Assets:Receivable:Accounts-Receivable", "debit": "75.00"},
    {"account": "Income:Services", "credit": "75.00"}
  ],
  "estimate_basis": "Hours logged, invoice not sent"
}
```

The reconciliation then shows receivables 2575.00 against supporting detail
2500.00. Explain the 75.00:

```json
{
  "operation_id": "explain-ar-apr",
  "account": "Assets:Receivable:Accounts-Receivable",
  "as_of": "2026-04-30",
  "currency": "USD",
  "mode": "residual",
  "managed_support_ids": ["invoice-orbit-1001", "invoice-vega-1002"],
  "details": [{"id": "unbilled-apr", "balance": "75.00", "description": "Unbilled April work, hours logged"}],
  "evidence_ids": ["<evidence-id>"]
}
```

`managed_support_ids` lists the open items the engine already counts; copy them
from the reconciliation row's `action`. Run `books accrual support preview` and
`books accrual support apply` with it. The row becomes `reconciled`.

## Items

### Recipe 1: unpaid bill, including a bill split across accounts

Use an item for a bill you owe. The expense counts on the bill's `as_of` date; the
payment is a separate step (recipe 3). `due_date` is the payment deadline and has
no effect on when the expense counts.

```json
{
  "operation_id": "bill-hosting-jan",
  "id": "bill-hosting-jan",
  "kind": "payable",
  "account": "Liabilities:Payable:Accounts-Payable",
  "counterparty": "Northwind Hosting",
  "currency": "USD",
  "original_amount": "600.00",
  "as_of": "2026-01-31",
  "due_date": "2026-02-15",
  "reference": "INV-4471",
  "origin": {"mode": "new", "source_id": "northwind-inv-4471", "counter_account": "Expenses:Hosting"},
  "evidence_ids": ["<evidence-id>"]
}
```

```sh
books accrual item preview --entity . --input bill.json > preview.json
books accrual item apply --entity . --input preview.json
```

The ledger shows Dr `Expenses:Hosting` 600.00 and Cr `Liabilities:Payable:Accounts-Payable`
600.00 on 2026-01-31. `books accrual report --kind outstanding` lists the bill as
open with its due date.

To split one bill across expense accounts, replace `counter_account` with
`origin.lines`. The amounts must add up exactly to `original_amount`, and one
payable balance is posted:

```json
{
  "operation_id": "bill-office-jan",
  "id": "bill-office-jan",
  "kind": "payable",
  "account": "Liabilities:Payable:Accounts-Payable",
  "counterparty": "Harbor Office Supply",
  "currency": "USD",
  "original_amount": "100.00",
  "as_of": "2026-01-31",
  "due_date": "2026-02-28",
  "origin": {
    "mode": "new",
    "source_id": "harbor-inv-90",
    "lines": [
      {"account": "Expenses:Office", "amount": "60.00"},
      {"account": "Expenses:Software", "amount": "40.00"}
    ]
  },
  "evidence_ids": ["<evidence-id>"]
}
```

The ledger shows Dr `Expenses:Office` 60.00, Dr `Expenses:Software` 40.00, and Cr
accounts payable 100.00. If the bank already recorded the payment, do not create
the bill as a new expense. Link the existing entry with `origin.mode: existing` and
its `source_id`.

### Recipe 2: customer invoice

Use a receivable item for an invoice you sent. Income counts on `as_of`.

```json
{
  "operation_id": "invoice-orbit-1001",
  "id": "invoice-orbit-1001",
  "kind": "receivable",
  "account": "Assets:Receivable:Accounts-Receivable",
  "counterparty": "Orbit Labs",
  "currency": "USD",
  "original_amount": "2000.00",
  "as_of": "2026-03-10",
  "due_date": "2026-04-09",
  "reference": "INV-1001",
  "origin": {"mode": "new", "source_id": "invoice-1001", "counter_account": "Income:Services"},
  "evidence_ids": ["<evidence-id>"]
}
```

```sh
books accrual item preview --entity . --input invoice.json > preview.json
books accrual item apply --entity . --input preview.json
```

The ledger shows Dr `Assets:Receivable:Accounts-Receivable` 2000.00 and Cr
`Income:Services` 2000.00 on 2026-03-10. The aging report groups the invoice by
its due date.

### Recipe 3: payment, with partial settlement

A settlement matches one bank movement to one or more open items. A partial
payment leaves the rest of the item open. Paying a bill uses a negative bank
amount.

```json
{
  "operation_id": "orbit-payment-1",
  "date": "2026-03-20",
  "source": {"mode": "new", "source_id": "bank-receipt-orbit-0320", "bank_account": "Assets:Bank:Checking", "amount": "1000.00"},
  "allocations": [{"item_id": "invoice-orbit-1001", "amount": "1000.00"}],
  "evidence_ids": ["<evidence-id>"]
}
```

```sh
books accrual item settle-preview --entity . --input payment.json > preview.json
books accrual item settle-apply --entity . --input preview.json
```

The ledger shows Dr `Assets:Bank:Checking` 1000.00 and Cr receivables 1000.00 on
2026-03-20. Income is not counted again. The outstanding report shows the invoice
with 1000.00 remaining.

Paying the bill from recipe 1 is the same request with `"amount": "-600.00"` and
the bill's `item_id`:

```json
{
  "operation_id": "pay-northwind",
  "date": "2026-02-12",
  "source": {"mode": "new", "source_id": "bank-payment-northwind-0212", "bank_account": "Assets:Bank:Checking", "amount": "-600.00"},
  "allocations": [{"item_id": "bill-hosting-jan", "amount": "600.00"}],
  "evidence_ids": ["<evidence-id>"]
}
```

One payment can name several items in `allocations`, and one item can receive
several payments. When the bank feed already holds the movement, set
`source.mode` to `existing` and `source.source_id` to that entry's ID, so cash is
not recorded twice.

### Recipe 4: overpayment kept as credit

When a customer pays more than the invoice, the extra amount is not income. Record
it as an open credit with `excess`. It stays on the customer's account until you
apply it (recipe 5) or refund it.

```json
{
  "operation_id": "invoice-delta-2001",
  "id": "invoice-delta-2001",
  "kind": "receivable",
  "account": "Assets:Receivable:Accounts-Receivable",
  "counterparty": "Delta Corp",
  "currency": "USD",
  "original_amount": "300.00",
  "as_of": "2026-03-01",
  "due_date": "2026-03-31",
  "reference": "INV-2001",
  "origin": {"mode": "new", "source_id": "invoice-2001", "counter_account": "Income:Services"},
  "evidence_ids": ["<evidence-id>"]
}
```

```json
{
  "operation_id": "delta-payment-2001",
  "date": "2026-03-15",
  "source": {"mode": "new", "source_id": "bank-receipt-delta-0315", "bank_account": "Assets:Bank:Checking", "amount": "350.00"},
  "allocations": [{"item_id": "invoice-delta-2001", "amount": "300.00"}],
  "excess": {"id": "delta-credit-0315", "account": "Liabilities:Other:Customer-Deposits", "reference": "Overpayment on INV-2001"},
  "evidence_ids": ["<evidence-id>"]
}
```

```sh
books accrual item settle-preview --entity . --input overpayment.json > preview.json
books accrual item settle-apply --entity . --input preview.json
```

The ledger shows Dr `Assets:Bank:Checking` 350.00, Cr receivables 300.00 and Cr
`Liabilities:Other:Customer-Deposits` 50.00 on 2026-03-15. The outstanding report
lists a 50.00 "Unapplied credit" for Delta Corp.

### Recipe 5: customer deposit applied to an invoice

A deposit arrives before the invoice. Record it as a credit with empty
`allocations` and an `excess` that names the customer. When the invoice exists,
apply the credit with `item offset`.

```json
{
  "operation_id": "vega-deposit",
  "date": "2026-02-03",
  "source": {"mode": "new", "source_id": "bank-receipt-vega-0203", "bank_account": "Assets:Bank:Checking", "amount": "500.00"},
  "allocations": [],
  "excess": {"id": "vega-deposit-credit", "account": "Liabilities:Other:Customer-Deposits", "counterparty": "Vega Inc", "reference": "Deposit for project"},
  "evidence_ids": ["<evidence-id>"]
}
```

The ledger shows Dr bank 500.00 and Cr `Liabilities:Other:Customer-Deposits` 500.00
on 2026-02-03. Later, record the invoice (a receivable item as in recipe 2, for
`Vega Inc`) and apply the deposit:

```json
{
  "operation_id": "apply-vega-deposit",
  "date": "2026-03-10",
  "credit_item_id": "vega-deposit-credit",
  "target_item_id": "invoice-vega-1002",
  "amount": "500.00",
  "reason": "Apply the February deposit to invoice 1002"
}
```

```sh
books accrual item offset-preview --entity . --input offset.json > preview.json
books accrual item offset-apply --entity . --input preview.json
```

The ledger shows Dr customer deposits 500.00 and Cr receivables 500.00 on
2026-03-10. The invoice has 1500.00 remaining and the deposit credit is fully used.
Both items must name the same `counterparty`. When both items sit on the same
control account, the offset only links them and posts nothing. A vendor
prepayment is the mirror case: a credit with `direction: "debit"` applied to a bill.

## Schedules

A schedule spreads one amount over dates. Its `method` is `actual_day` (exact per
day), `equal_month` (whole calendar months, starting on the 1st and ending on a
month end) or `custom` (a dated list you provide). The first two round each
installment down to the cent and give the leftover cents to the last installment.
Posting is separate from creating a schedule: `schedule due-preview --through DATE`
lists what is due, and `schedule due-apply` posts it.

`origin` says where the money came from. `origin.date` is the day the money moved; it can
be earlier than `service_start`. `funding_account` is the bank or card account that paid.

### Recipe 6: annual prepaid expense

Use it for a cost paid in advance, such as an annual software plan. The payment is
an asset until each month uses it.

```json
{
  "operation_id": "software-annual-create",
  "id": "software-annual",
  "kind": "prepaid",
  "account": "Assets:Other:Prepaid-Expenses",
  "recognition_account": "Expenses:Software",
  "currency": "USD",
  "amount": "1200.00",
  "service_start": "2026-01-01",
  "service_end": "2026-12-31",
  "method": "equal_month",
  "reference": "Annual software subscription",
  "origin": {"mode": "new", "date": "2026-01-01", "source_id": "bank-payment-software-0101", "funding_account": "Assets:Bank:Checking"},
  "evidence_ids": ["<evidence-id>"]
}
```

```sh
books accrual schedule preview --entity . --input prepaid.json > preview.json
books accrual schedule apply --entity . --input preview.json
books accrual schedule due-preview --entity . --through 2026-03-31 --operation-id recognize-2026-03 > due.json
books accrual schedule due-apply --entity . --input due.json
```

Creating the schedule posts Dr `Assets:Other:Prepaid-Expenses` 1200.00 and Cr bank
1200.00 on 2026-01-01. Each month then posts Dr `Expenses:Software` 100.00 and Cr
prepaid 100.00 on its last day. After March, 900.00 remains prepaid. If the bank
already recorded the payment, use `origin.mode: existing` with the entry's
`source_id`.

### Recipe 7: deferred revenue, January 15 to January 14

Use it when a customer pays before the service. The money is a liability until the
service is delivered. A service that starts on a day other than the 1st uses
`actual_day`.

```json
{
  "operation_id": "bigco-annual-create",
  "id": "bigco-annual",
  "kind": "deferred",
  "account": "Liabilities:Other:Deferred-Revenue",
  "recognition_account": "Income:Subscriptions",
  "currency": "USD",
  "amount": "12000.00",
  "service_start": "2026-01-15",
  "service_end": "2027-01-14",
  "method": "actual_day",
  "reference": "BigCo annual plan",
  "origin": {"mode": "new", "date": "2026-01-15", "source_id": "bank-receipt-bigco-0115", "funding_account": "Assets:Bank:Checking"},
  "evidence_ids": ["<evidence-id>"]
}
```

The funding entry posts Dr bank 12000.00 and Cr `Liabilities:Other:Deferred-Revenue`
12000.00 on 2026-01-15. The 365 service days give these installments: January
558.90, February 920.54, March 1019.17 and so on, ending with a 460.34 installment
for January 1 to 14, 2027. Each posts Dr deferred revenue and Cr
`Income:Subscriptions` on the month's last day. After March, 9501.39 remains
deferred. Use `equal_month` only for whole calendar months.

### Recipe 8: equipment with straight-line depreciation

A `depreciation` schedule keeps the asset at cost and builds a contra account. Give
`contra_account`; `salvage_value` is not depreciated. Here the depreciable base is
2400.00 minus 240.00 salvage, over 24 months.

```json
{
  "operation_id": "laptop-create",
  "id": "laptop",
  "kind": "depreciation",
  "account": "Assets:Fixed:Equipment",
  "contra_account": "Assets:Fixed:Accumulated-Depreciation",
  "recognition_account": "Expenses:Depreciation",
  "currency": "USD",
  "amount": "2400.00",
  "salvage_value": "240.00",
  "service_start": "2026-02-01",
  "service_end": "2028-01-31",
  "method": "equal_month",
  "reference": "Laptop, 24 months",
  "origin": {"mode": "new", "date": "2026-01-05", "source_id": "bank-payment-laptop-0105", "funding_account": "Assets:Bank:Checking"},
  "evidence_ids": ["<evidence-id>"]
}
```

The funding entry posts Dr `Assets:Fixed:Equipment` 2400.00 and Cr bank 2400.00 on
2026-01-05. Each month from February posts Dr `Expenses:Depreciation` 90.00 and Cr
`Assets:Fixed:Accumulated-Depreciation` 90.00. After March, equipment shows 2400.00,
accumulated depreciation -180.00, and book value 2220.00. The first depreciation
month is your choice: this schedule starts the month after purchase.

### Recipe 9: usage-based installments

Use `custom` when the pattern is not even: usage, milestones or an agreed schedule.
List the dated amounts in ascending order. They must add up to the amount, and each
must fall between `service_start` and `service_end`.

```json
{
  "operation_id": "api-credits-create",
  "id": "api-credits",
  "kind": "prepaid",
  "account": "Assets:Other:Prepaid-Expenses",
  "recognition_account": "Expenses:Hosting",
  "currency": "USD",
  "amount": "300.00",
  "service_start": "2026-01-01",
  "service_end": "2026-03-31",
  "method": "custom",
  "installments": [
    {"date": "2026-01-31", "amount": "50.00"},
    {"date": "2026-02-28", "amount": "100.00"},
    {"date": "2026-03-31", "amount": "150.00"}
  ],
  "reference": "Prepaid API credits, used as consumed",
  "origin": {"mode": "new", "date": "2026-01-01", "source_id": "bank-payment-api-0101", "funding_account": "Assets:Bank:Checking"},
  "evidence_ids": ["<evidence-id>"]
}
```

The funding entry posts Dr prepaid 300.00 and Cr bank 300.00. Due recognition then
posts Dr `Expenses:Hosting` 50.00 on 2026-01-31, 100.00 on 2026-02-28 and 150.00
on 2026-03-31. The schedule is complete after March.

### Recipe 10: end a subscription early with a refund

`terminate` ends a schedule after a last service day. The engine recognizes service
through `effective_date` (a partial month is prorated by day), then releases the
unearned balance to `offset_account`. Use the bank for a cash refund, a
receivable or payable for a credit, or an income or expense account for a
write-off.

Take an annual design tool, paid 1200.00 on 2026-01-01 (a prepaid schedule like
recipe 6, `id` `design-tool`). Months January to March have been recognized. On
2026-05-10 you learn it was cancelled and 800.00 refunded, effective April 30.

```json
{
  "operation_id": "design-tool-terminate",
  "id": "design-tool",
  "action": "terminate",
  "date": "2026-05-10",
  "effective_date": "2026-04-30",
  "offset_account": "Assets:Bank:Checking",
  "reason": "Subscription cancelled; vendor refunded the unused months",
  "evidence_ids": ["<evidence-id>"]
}
```

```sh
books accrual schedule change-preview --entity . --input terminate.json > preview.json
books accrual schedule change-apply --entity . --input preview.json
```

The ledger shows Dr `Expenses:Software` 100.00 and Cr prepaid 100.00 on 2026-04-30
for April, then Dr bank 800.00 and Cr prepaid 800.00 on 2026-05-10. Total expense
is 400.00 and nothing remains prepaid. `date` is the posting date and must not be
before `effective_date`. Other actions are `cancel`, `refund` and `supersede`; see
`books accrual schema`.

## Journals and estimates

### Recipe 11: monthly accrued expense that reverses itself

Use it for a cost you know you incurred but have no bill for. Give an
`estimate_basis` and a `reversal_date`. The journal needs no stored document, and
no supporting detail for the accrued liability.

```json
{
  "id": "utilities-accrual-jan",
  "date": "2026-01-31",
  "description": "January utilities, bill not received yet",
  "postings": [
    {"account": "Expenses:Utilities", "debit": "450.00"},
    {"account": "Liabilities:Other:Accrued-Liabilities", "credit": "450.00"}
  ],
  "estimate_basis": "Same as the December bill",
  "reversal_date": "2026-02-01"
}
```

```sh
books accrual journal preview --entity . --input accrual.json > preview.json
books accrual journal apply --entity . --input preview.json
books accrual journal due-reversals --entity . --as-of 2026-02-01 --apply
```

Without `--apply`, `due-reversals` only lists what is due. The January entry posts
Dr `Expenses:Utilities` 450.00 and Cr `Liabilities:Other:Accrued-Liabilities`
450.00. On 2026-02-01 the exact reverse posts. When the real bill arrives, record
it as a payable item (recipe 1) in February:

```json
{
  "operation_id": "bill-powerco-feb",
  "id": "bill-powerco-feb",
  "kind": "payable",
  "account": "Liabilities:Payable:Accounts-Payable",
  "counterparty": "PowerCo",
  "currency": "USD",
  "original_amount": "480.00",
  "as_of": "2026-02-10",
  "due_date": "2026-03-02",
  "reference": "Utilities, January usage",
  "origin": {"mode": "new", "source_id": "powerco-bill-0210", "counter_account": "Expenses:Utilities"},
  "evidence_ids": ["<evidence-id>"]
}
```

January shows 450.00 of utilities, February shows 30.00 (480.00 minus the reversed
450.00), and the accrued liability is back to zero.

### Recipe 12: bonus accrual trued up to the actual amount

Use an accrual item when the estimate must stay visible until the real amount is
known. Then `true_up` settles the estimate against the actual bill and keeps the
difference in expense.

```json
{
  "operation_id": "bonus-q1-estimate",
  "id": "bonus-q1",
  "kind": "accrual",
  "account": "Liabilities:Other:Accrued-Liabilities",
  "currency": "USD",
  "original_amount": "3000.00",
  "as_of": "2026-03-31",
  "reference": "Q1 bonus estimate",
  "estimate_basis": "Target bonus pool for the quarter",
  "origin": {"mode": "new", "source_id": "bonus-q1-estimate-src", "counter_account": "Expenses:Bonuses"}
}
```

Record the approved amount as an ordinary bill, then true up:

```json
{
  "operation_id": "bonus-q1-actual",
  "id": "bonus-q1-actual",
  "kind": "payable",
  "account": "Liabilities:Payable:Accounts-Payable",
  "counterparty": "Staff bonus pool",
  "currency": "USD",
  "original_amount": "2700.00",
  "as_of": "2026-04-15",
  "reference": "Q1 bonuses as approved",
  "origin": {"mode": "new", "source_id": "bonus-q1-approval", "counter_account": "Expenses:Bonuses"},
  "evidence_ids": ["<evidence-id>"]
}
```

```json
{
  "operation_id": "bonus-q1-true-up",
  "item_id": "bonus-q1",
  "action": "true_up",
  "actual_item_id": "bonus-q1-actual",
  "date": "2026-04-15",
  "counter_account": "Expenses:Bonuses",
  "reason": "Bonuses approved at 2,700.00"
}
```

```sh
books accrual item adjust-preview --entity . --input true-up.json > preview.json
books accrual item adjust-apply --entity . --input preview.json
```

The estimate posts Dr `Expenses:Bonuses` 3000.00 and Cr accrued liabilities 3000.00
on 2026-03-31. The approved bill posts Dr bonuses 2700.00 and Cr accounts payable
2700.00. The true-up posts Dr accrued liabilities 3000.00 and Cr bonuses 3000.00,
so net bonus expense is 2700.00 and the accrued liability is zero. The variance (actual
minus the estimate removed) is stored on the adjustment and shown in the preview
`details.true_up` and in the receipt `warnings` entry `true_up_variance`, with
`estimate_amount`, `actual_amount` and `variance`. Two other actions exist: `credit` reduces an item and
the income or expense (a discount), and `writeoff` removes a balance (recipe 13).

### Recipe 13: bad-debt write-off

Use it for an invoice you will not collect. The write-off moves the balance to an
expense account and closes the item. A `credit` or `writeoff` request also needs
a `source_id` that is unique to this adjustment, and stored supporting documents.

```json
{
  "operation_id": "invoice-ridge-3001",
  "id": "invoice-ridge-3001",
  "kind": "receivable",
  "account": "Assets:Receivable:Accounts-Receivable",
  "counterparty": "Ridge Co",
  "currency": "USD",
  "original_amount": "300.00",
  "as_of": "2026-01-20",
  "due_date": "2026-02-19",
  "reference": "INV-3001",
  "origin": {"mode": "new", "source_id": "invoice-3001", "counter_account": "Income:Services"},
  "evidence_ids": ["<evidence-id>"]
}
```

```json
{
  "operation_id": "writeoff-ridge-3001",
  "source_id": "writeoff-ridge-3001-src",
  "item_id": "invoice-ridge-3001",
  "action": "writeoff",
  "date": "2026-03-31",
  "amount": "300.00",
  "counter_account": "Expenses:Bad-Debt",
  "reason": "Customer closed; no response after three reminders",
  "evidence_ids": [
    "<evidence-id>"
  ]
}
```

```sh
books accrual item adjust-preview --entity . --input writeoff.json > preview.json
books accrual item adjust-apply --entity . --input preview.json
```

The ledger shows Dr `Expenses:Bad-Debt` 300.00 and Cr receivables 300.00 on
2026-03-31. The invoice shows 0.00 remaining.

## Opening balances

### Recipe 14: opening balances from scratch

Use `opening` once, for a new accrual company, from a spreadsheet, another system
or a trial balance. The cutover is the first day of the accrual books. The opening
entry is dated the day before, and the accounts it uses are opened on that date.
`lines` are signed (debits positive) and must balance. Open invoices and bills go
in `items`, remaining prepaid or deferred balances go in `schedules`, and other
balances need `external` rows. Each supporting row set must equal its account's
balance. `evidence_refs` names a company-relative file that holds the source list.

```json
{
  "operation_id": "opening-2026",
  "cutover": "2026-01-01",
  "lines": [
    {
      "account": "Assets:Bank:Checking",
      "amount": "30000.00"
    },
    {
      "account": "Assets:Receivable:Accounts-Receivable",
      "amount": "300.00"
    },
    {
      "account": "Assets:Other:Prepaid-Expenses",
      "amount": "600.00"
    },
    {
      "account": "Liabilities:Payable:Accounts-Payable",
      "amount": "-100.00"
    },
    {
      "account": "Equity:Opening-Balances",
      "amount": "-30800.00"
    }
  ],
  "items": [
    {
      "id": "inv-1",
      "kind": "receivable",
      "account": "Assets:Receivable:Accounts-Receivable",
      "counterparty": "Delta Corp",
      "reference": "INV-1",
      "due_date": "2025-12-15",
      "open_amount": "300.00"
    },
    {
      "id": "bill-9",
      "kind": "payable",
      "account": "Liabilities:Payable:Accounts-Payable",
      "counterparty": "Hosting Co",
      "reference": "BILL-9",
      "due_date": "2026-01-10",
      "open_amount": "100.00"
    }
  ],
  "schedules": [
    {
      "id": "insurance-2025",
      "kind": "prepaid",
      "account": "Assets:Other:Prepaid-Expenses",
      "recognition_account": "Expenses:Software",
      "amount": "1200.00",
      "recognized_amount": "600.00",
      "service_start": "2025-07-01",
      "service_end": "2026-06-30",
      "method": "equal_month",
      "currency": "USD",
      "reference": "Annual software plan from July"
    }
  ],
  "evidence_refs": [
    "evidence/opening-ar-ap-list.txt"
  ]
}
```

```sh
books entity init ~/Documents/books/acme --name "Acme" --basis accrual --cutover-date 2026-01-01
books accrual opening preview --entity ~/Documents/books/acme --input opening.json > plan.json
books accrual opening apply --entity ~/Documents/books/acme --input plan.json
```

The ledger shows the entry on 2025-12-31 with each line. Receivables 300.00 and
payables 100.00 are tracked as open items, and prepaid 600.00 is a schedule with 600.00
already recognized: January posts Dr software 100.00 and Cr prepaid 100.00. Pay or
collect an opening item with an ordinary settlement (recipe 3). If a supporting
row does not equal its account balance, the preview is `blocked` and lists the
account, the support amount and the difference.

### Recipe 15: opening balances from a cash company

Slashbooks does not convert a company in place. Keep the cash company for earlier
periods, start a new accrual company, and carry the closing cash balances in.

```sh
books entity init NEW_DIR --name NAME --basis accrual --cutover-date 2026-01-01
books accrual opening draft --entity CASH_DIR --cutover 2026-01-01 > draft.json
```

The draft prints a wrapper with `status`, `next_steps`, `agent_must_add` and a
`request` object. Edit `draft.json` and use it as `opening.json`: `opening preview` reads its `request`. It holds the cash
balances for 2025-12-31, with prior earnings in `Equity:Retained-Earnings`. A cash
company has no receivables or payables, so add the open invoices and bills that the
owner provides as `items`, add receivable and payable lines, and add one balancing
equity line so the lines still balance. Then preview and apply:

```sh
books accrual opening preview --entity NEW_DIR --input opening.json > plan.json
books accrual opening apply --entity NEW_DIR --input plan.json
```

The balance sheet at 2026-01-01 shows the cash balances, the receivable, and equity
that includes the added amount.

## Close the month

### Recipe 16: month-end close

Close in this order. The engine tells you the exact next command for each blocker.

1. Record this month's schedule recognition and due reversals:

   ```sh
   books accrual schedule due-preview --entity . --through 2026-01-31 --operation-id recognize-2026-01 > due.json
   books accrual schedule due-apply --entity . --input due.json
   books accrual journal due-reversals --entity . --as-of 2026-02-01 --apply
   ```

2. Read the reports. They state the basis and list unposted recognition as an
   unresolved issue until step 1 is done:

   ```sh
   books report pnl --entity . --from 2026-01-01 --to 2026-01-31 --format text
   books report balance-sheet --entity . --as-of 2026-01-31 --format text
   books accrual report --entity . --kind reconciliation --as-of 2026-01-31 --format text
   ```

3. Declare the dates each source covers. Coverage is additive: a new range joins
   the ranges already recorded, and a gap between ranges blocks any period that
   spans it.

   ```sh
   books entity source-coverage . --source bank-checking --from 2026-01-01 --to 2026-01-31 --account Assets:Bank:Checking
   ```

4. Reconcile each bank account with the statement. Keep the statement in the company
   folder and pass its SHA-256 hash. Export and readiness both need this step.

   ```sh
   books reconcile --entity . --account Assets:Bank:Checking --source-balance 6900.00 \
     --source-opening-balance 0.00 --source-transaction-total 6900.00 \
     --source-snapshot-at 2026-02-01T09:00:00Z --source-through 2026-01-31 \
     --source-evidence-ref evidence/statement-2026-01.txt \
     --source-evidence-hash evidence/statement-2026-01.txt=$(shasum -a 256 evidence/statement-2026-01.txt | cut -d' ' -f1) \
     --as-of 2026-01-31
   ```

   Liabilities, including a credit card, are negative in the ledger. If the reconcile
   report says "Likely sign mistake", run it again with the sign it prints.

5. Check readiness and save the package:

   ```sh
   books readiness --entity . --period 2026-01 --as-of 2026-02-05 --output readiness-2026-01.json
   ```

   The state is `ready_for_review` when there are no blockers. Each blocker names
   the next command. Copy `package_id` from the file.

6. A person closes the period with that package ID in `close.json`.

```json
{
  "operation_id": "close-2026-01",
  "period": "2026-01",
  "action": "close",
  "as_of": "2026-02-05",
  "reason": "January books reviewed and ready",
  "readiness_package_id": "<readiness-package-id>"
}
```

```sh
books accrual period preview --entity . --input close.json > preview.json
books accrual period apply --entity . --input preview.json
```

Ready is not closed: preparing the month and locking it are separate. The
engine refuses a posting dated inside a closed period.

**Reopen.** A person reopens a closed period; an agent cannot. If a late entry needs a closed month, write a `reopen`
request, then preview and apply it:

```json
{
  "operation_id": "reopen-2026-01",
  "period": "2026-01",
  "action": "reopen",
  "as_of": "2026-02-10",
  "reason": "Late vendor credit for January"
}
```

```sh
books accrual period preview --entity . --input reopen.json > preview.json
books accrual period apply --entity . --input preview.json
```

Post the correction, then close the period again with a fresh readiness package.
Or choose a correction date in an open period.

## Export for the accountant

Export reads the saved reconciliation, so run the reconcile step (recipe 16, step 4)
for each bank account first. Without it, export stops with the figures it needs.
Excel output needs `pip install 'agent-books[xlsx]'`; without the extra, export
writes the same data as CSV files.
