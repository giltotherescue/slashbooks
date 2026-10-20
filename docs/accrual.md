# Accrual books

Accrual bookkeeping records income when you earn it and expenses when you incur
them. Receiving or paying the money is a separate step. That helps you see both
how the business performed and what customers still owe or you still need to pay.

Accrual is opt-in. Slashbooks supports straightforward accrual books for one
company, in USD, for services and SaaS businesses. An owner, bookkeeper,
accountant, or authorized agent can do the work. There is no separate mandatory
accountant approval role. The accounting engine calculates the amounts; the agent
collects the facts, explains the choices, and runs the work you have authorized.

Worked, tested examples of every pattern below are in
[accrual recipes](accrual-recipes.md). Read them to see the exact request for an
unpaid bill, a customer deposit, deferred revenue, equipment, an accrued estimate,
a write-off, an early cancellation, opening balances, and the month-end close.

## Ask for the task you need

You can use `/books` or ask in ordinary language:

| Ask your agent | What the books should show |
|---|---|
| “Record this $600 contractor bill for July. We have not paid it yet.” | A July expense and a $600 amount owed to the contractor. Payment later reduces the amount owed. |
| “We paid $1,200 for software covering January 1 through December 31. Spread the cost across those months.” | A prepaid expense schedule: $100 expense each month and $1,100 still prepaid after January. |
| “This customer paid $1,200 upfront for twelve months of service. Record the income as we provide the service.” | Deferred revenue, with $100 earned each month when an equal monthly allocation fits the agreement. |
| “Match this $400 customer payment to the amount they owe us.” | The customer balance falls by $400. Income already earned is counted once. |
| “This customer paid $500 before we invoiced them. Apply it to the invoice.” | The deposit is held as a customer credit, then applied to the invoice. The invoice balance falls by $500. |
| “We bought a $2,400 laptop. Depreciate it over 24 months.” | The laptop stays at cost. Each month adds depreciation expense and builds accumulated depreciation. |
| “Accrue $450 for January utilities; the bill has not come yet.” | A January expense that reverses itself when the real bill arrives. |
| “We will not collect this $300 invoice.” | A bad-debt expense and a receivable of zero. |
| “Record this payroll provider's July adjustment and keep its supporting report.” | The provider's accounting amounts and supporting detail, without recalculating payroll. |
| “Prepare July's books and tell me what needs attention before we close.” | Current adjustments, payments, schedules, reconciliations, and a concrete list of anything still missing. |

Give the agent the bill, statement, agreement, or other supporting document. It
should look for an existing entry or payment before recording another one. It
will ask for a missing fact when the answer changes the accounting, such as the
service dates or which bill a payment covers. You do not need to supply internal
record IDs, choose amount signs, or prepare JSON files.

## Choose cash or accrual books

Cash is the default for a new Slashbooks company. Ask for accrual during setup
when that is the accounting basis you intend to use. Cash books generally record
income and expenses when money moves; accrual books separate earning or incurring
an amount from its payment.

For an existing company, preserve the basis already used for its books. Changing
a report setting does not convert cash books into accrual books, and Slashbooks
does not rewrite earlier periods. A cash company can add accrual records from a
date forward and keep its cash history: see
[Turn on accrual for an existing company](#turn-on-accrual-for-an-existing-company).
Local and Cloud books use the same accounting rules, and an imported set of books
must use the destination's selected basis.

## Review the result before recording it

The agent should explain a proposed action in business terms:

> Record $100 of July software expense. The payment is already recorded, so this
> adds no bank payment. After July, $500 remains prepaid. The supplier agreement
> supports the amount and service dates.

A useful review answers:

- Which date and reporting period does this affect?
- What happens to income, expenses, money owed, or a prepaid balance?
- Is the payment already in the books, or is this a new payment with its own supporting document?
- What amount will remain after the action?
- Which document supports it, and is anything still uncertain?

Accountants can also inspect the dated journal entries, debits and credits,
allocations, and supporting schedules. Corrections preserve the original entry
and its relationship to the replacement. If the books or supporting facts change
after a preview, the agent must prepare a fresh one. An uncertain result should be
checked against the saved accounting receipt before the action is repeated.

## Bills, customer balances, and payments

Accounts receivable are amounts customers owe for income already recorded.
Accounts payable and accrued liabilities are costs recorded but still unpaid.
Slashbooks can track their original amounts, partial payments, credits,
write-offs, and remaining balances. Supply the actual due date when known; the
accounting date does not establish when payment is due.

An overpayment or a deposit that arrives before the invoice is recorded as an open
customer credit, not as income. Applying that credit to an invoice (an offset) is
a separate step that reduces the invoice balance. A vendor prepayment works the
same way in the other direction. A bill that covers several expense categories can
be split across them while it stays one amount owed.

Ask the agent to match a receipt or payment to the relevant balance. One payment
can cover several balances, and one balance can have several payments. If the
bank movement is already recorded, the agent links it instead of recording cash
again. An ordinary vendor category does not establish whether a payment settles
an earlier bill or pays for a new expense.

These are accounting records. Slashbooks does not send customer invoices, run a
purchasing system, or execute payments.

## Prepaid expenses and customer prepayments

Prepaid expenses are costs paid in advance, such as an annual software agreement.
Deferred revenue is money received before the related service has been earned.
Their schedules show the original amount, what belongs in each month, what has
already been recorded, and what remains.

Supply the first and last covered service days as written in the agreement; both
days are included (a year starting January 15 ends January 14). Whole calendar
months can use equal monthly amounts (`equal_month`); any other start or end uses
actual days (`actual_day`, exact per day). Usage-based, milestone or uneven
patterns use `custom` with a dated list of amounts that adds up exactly. The
engine rounds each installment down to the cent and gives the last installment
the leftover cents, then shows the complete schedule for review.

A fixed asset uses a `depreciation` schedule: the asset keeps its full cost, each
period debits the depreciation expense and credits an accumulated depreciation
account (`contra_account`), and an optional `salvage_value` is not depreciated.

Slashbooks calculates a straight-line schedule from the cost, salvage value, and
service dates you give it. It does not decide a useful life or a tax depreciation
method; those come from you or your preparer.

For a cancellation, refund, or changed agreement, ask the agent to update the
existing schedule. To end service early, the `terminate` change recognizes service
through the last service day (a partial month is prorated by day) and releases the
unearned balance to a refund, credit or write-off account. It should explain how
much relates to future service and how much changes an expense or income already
recorded. Previous months remain
traceable. Missing service dates or earlier allocations need supporting detail
before an automatic schedule can be used.

## Estimates and adjustments

An accrued cost with no bill yet can be recorded as a journal with a plain-text
estimate basis and a reversal date. It reverses itself on that date, and the real
bill is then recorded as an ordinary unpaid bill. To keep an estimate visible until
the real amount is known, record it as an accrued balance and later true it up
against the actual bill; the variance stays in expense. An invoice you will not
collect is written off to a bad-debt expense.

## Entries that need no supporting detail

Items and schedules are optional. A balanced journal with a supporting document or
an estimate basis is enough for any adjustment you can state in debits and credits.
An asset or liability account with no registered items or schedules is
`ledger_only`: the reports show its balance and it never blocks a close.

Once an account does have items or schedules, its ledger balance must equal that
detail. If a plain journal later posts to it, the reconciliation shows the
difference and an exact next step: register the amount as an item or schedule, or
explain it with a support snapshot in `residual` mode. See
[recipe 0](accrual-recipes.md#recipe-0-plain-journal-no-supporting-detail-needed).

## Saved accrual rules

Some bills repeat. A person decides once how to treat Northwind Hosting's annual
plan, and every later run should propose the same treatment. A saved accrual rule
keeps that decision.

- **What a rule is.** It is a preference about what to propose: prepaid, deferred
  revenue, depreciation, an unpaid bill, an invoice or an accrued estimate, with the
  term, method, accounts and an optional amount threshold. The person's note is shown
  with every proposal that uses it.
- **What a rule is not.** A rule never records anything, never approves, and never
  grants authority. A person still approves each preview, or the trusted delegate that
  already has authority does. Unattended runs may read and match rules but cannot
  save one.
- **Two kinds.** A supplier or customer rule belongs to one counterparty and is kept
  on its record in `learned-context/counterparties.json`. A company rule describes a
  kind of cost, such as "software at 1,000 or more is prepaid" or "equipment at 2,500
  or more is depreciated over 36 months". It lives in
  `learned-context/accrual-rules.json`. Both are written only by Slashbooks commands.
- **Which rule wins.** A supplier or customer rule wins over a company rule. Among
  company rules, the most specific wins. The match result explains why.
- **Who set it.** Each rule keeps who set it, when, and whether a person or an agent
  decided. An agent may propose a rule and must tell the person. An agent cannot
  replace or clear a rule a person set.

Ask your agent: "Remember this for Northwind Hosting" or "From now on, treat software
over $1,000 as a prepaid expense over its service period." The commands are
`books accrual rules list`, `set-preview` and `set-apply`, `clear-preview` and
`clear-apply`, and the read-only `match`. `match` takes the facts of a bill, invoice,
payment or supporting document and returns the matching rule and a draft request for
the ordinary preview. The draft lists any fact still missing. See
[recipe 17](accrual-recipes.md#recipe-17-remember-a-treatment-for-a-supplier-or-for-a-kind-of-cost).

## Reports, coverage, and close

Ask `/books-ask` questions such as:

- “What did we earn in July, and what expenses belong to July?”
- “Which customers still owe us, and what costs remain unpaid?”
- “Which receivables are overdue?”
- “How much software or insurance cost remains prepaid?”
- “What adjustments were made this month?”
- “What is stopping July from being ready to close?”

Income and expense reports describe business performance. Bank movements describe
cash received and paid. The agent should say which one it is answering. Every
`/books-ask` answer starts with a line that names the basis and the dates it
covers. A question that asks for a cash-basis profit on accrual books is answered
from the derived [cash view](#cash-view) and says so. For cash received and paid by
account, read the bank accounts in the general ledger
(`books report general-ledger --entity . --from DATE --to DATE`). A missing
historical period is unavailable, rather than a zero amount. Aging reports use
actual due dates; balances with unknown due dates and unapplied credits are shown
separately rather than guessed or automatically netted.

For `/books-close`, the agent brings in activity, matches payments, records due
monthly allocations and reversals, checks supporting detail, and reconciles
balances. It should explain unresolved work with a next step:

> July is not ready to close. The annual software schedule still needs its $100
> July expense recorded, and the payroll balance needs the provider's supporting
> report. The other completed work is saved.

Declare which dates each source covers with `books entity source-coverage`. Coverage
is additive: each new range joins the ranges already recorded, so a later import
never erases an earlier one, and a gap between ranges blocks any month that spans
it. The saved coverage lists its `ranges`.

Preparing the month and locking or publishing it are separate actions. The agent
can complete preparation within its existing authority. A person closes the period
and a person reopens it; an agent cannot reopen a period. To reopen, write a
`reopen` request and run `books accrual period preview` and `period apply` with it;
the closed-period error prints the exact request. Then post the correction and close
again with a fresh readiness package. A report being generated successfully does not
by itself mean the month is ready. The month-end steps, with commands, are in
[recipe 16](accrual-recipes.md#recipe-16-month-end-close).

When a bank or card balance does not match, `books reconcile` reports the
difference and a likely cause. If the difference is exactly twice the balance, it
reports a sign mistake and the corrected command. The ledger shows liabilities,
including a credit card balance, as negative.

Accountant exports include the P&L, balance sheet, trial balance, adjusting
entries, receivables and payables, prepaid and deferred schedules, supporting
detail, and account reconciliations. The reports state the accounting basis,
dates covered, and unresolved issues. Historical balances remain available after
later payments or schedule changes.

Export needs a saved bank reconciliation for each period, so run `books reconcile`
for each bank account first (see recipe 16, step 4). The Excel workbook needs the
optional extra, `pip install 'agent-books[xlsx]'`; without it, export writes the
same data as CSV files.

## Cash view

Accrual books record income when you earn it. Some owners, lenders and tax preparers
also want the same period on a cash basis. Slashbooks derives that view from the
accrual books on request. The books stay accrual: the cash view writes nothing, and it
never changes a published statement or a period lock.

```sh
books report pnl --entity . --from 2026-03-01 --to 2026-03-31 --basis cash
books report pnl --entity . --from 2026-03-01 --to 2026-03-31 --basis cash --format json
```

`--basis accrual` is the default on accrual books. On cash books, `--basis cash` is
the ordinary report, and `--basis accrual` fails with a clear message: cash books
keep no invoices, bills, prepaid or deferred records to build an accrual view from.
The text, the JSON (`meta.view_label`) and a file written with `--output` all say
"Cash basis (derived from accrual books)". The JSON `meta.method` block lists the
rules below, and `meta.tie_out` carries the proof.

You can also ask: "What was our cash-basis profit for March?" or "How much cash did
we receive from customers in March?". The answer starts with
"Answer covers: cash basis (derived from accrual books), from DATE to DATE." and uses
the same view. The cash view covers profit and loss only; it is not a balance sheet.

### How each event is shown

| Event in the accrual books | Cash view |
|---|---|
| Invoice or bill (receivable or payable item) | Income or expense when cash settles it, on the settlement date, split across the item's lines (`origin.lines`, or its one counter account) in proportion to the amount settled. |
| Rounding of a split | Each line's cumulative share is floored to the cent, and the last line takes the leftover cents. When cash fully settles an item, the cumulative amount on every line equals the line exactly. |
| Credit memo, credit offset, write-off, credit or true-up (`item adjust`) | No cash moves, so no effect. A credit memo applied to an invoice lowers the cash view only through the smaller payment. |
| Customer deposit or overpayment (`excess`) | Income on the receipt date, on the line "Customer payments not yet applied to an invoice" (no category). When you apply it to an invoice, the amount moves from that line to the invoice's lines on the application date. This is how common cash reports such as QuickBooks show an unapplied payment. A refund of the credit reverses the line. |
| Supplier prepayment | The same rule on the expense side: the line "Supplier prepayments not yet applied to a bill". |
| Prepaid schedule | Expense in full on the funding date. The monthly recognition entries are ignored. |
| Deferred revenue schedule | Income in full on the receipt date. The monthly recognition entries are ignored. |
| Terminate, cancel or refund of a schedule | Cash refunded or paid back reverses the expense or income on that date. A release to a write-off account moves no cash and has no effect. |
| Depreciation | Shown as recorded, as in QuickBooks cash reports. The equipment purchase stays an asset and is not expense. |
| Accrued estimate (journal with `estimate_basis` or `reversal_date`, accrual item), its reversal and its true-up | Excluded. The actual bill follows the invoice and bill rule. |
| Credit card charge | A credit card charge counts as paid on the charge date (cash-basis tax rule). Paying the card balance later is not an expense. |
| Journal or bank transaction that touches no registered control account | Included as recorded. Card charges are expense when charged. A journal that moves income or expense against an accrued, prepaid, deferred, receivable, payable or deposit account that has no registered detail is also included, and is flagged under "Entries that may not be cash". |
| Opening receivable or payable | It has no income or expense account. Its collection or payment shows on the lines "Collected from opening receivables" and "Paid on opening payables", with no category. Net income includes them. |
| Cash that moves through a registered control account with nothing to explain it | Shown on the line "Unclassified cash movements (review needed)" and listed in the report issues. It is never dropped. |

A registered control account is an account that holds receivable, payable, accrual or
credit items, prepaid or deferred schedules, or estimate journals. An account with no
registered detail is `ledger_only` and its entries are shown as recorded.

### Tie-out

Every cash view proves itself. Accrual net income minus cash net income must equal the
reconciling items, to the cent:

```
accrual net income - cash net income = sum of the reconciling items
```

Each reconciling item is the net change, in the period, of one group of registered
control accounts, read from the ledger postings: receivables, payables, prepaid
expenses, deferred revenue, accrued liabilities and estimates, customer deposits and
credits, write-offs and true-ups (no cash), credits applied (no cash), and opening items
collected or paid. A positive amount means accrual income is higher than cash income by
that amount. The text report ends with the tie-out, and the JSON has
`meta.tie_out`. If the two sides do not agree, the report fails with an error and shows
no figures.

### Limits

- The cash view starts at the accrual cutover. A range that starts earlier fails and names
  the first available date, as other reports do.
- It does not change the books. To have a company on a cash basis, keep cash books.
  Moving between bases needs a separate conversion.
- Hand-built accruals in an account with no registered item, schedule or estimate
  journal (for example loan interest in a plain `Accrued-Interest` account) are not
  guessed at. They are included as recorded, so the cash view can show a non-cash
  expense or income. Each such entry is listed under "Entries that may not be cash"
  (JSON issue `cash_view_noncash_entry`, with date, amount, accounts and description),
  the header says "Includes $X of entries that may not be cash", and `books ask`
  mentions it. To have the cash view exclude such an accrual, record it as an estimate
  journal (with `reversal_date` or `estimate_basis`) or as an accrual item.
- The balance sheet, tax mapping and accountant workbook sheets stay on the accrual basis in this release.
- Per-vendor and per-customer cash totals need linked bank movements. Use the general
  ledger for them.

## Starting accrual books

Every accrual company begins with one opening step. The start date (the cutover) is
the first day of the accrual books. The opening entry is dated the day before and
shows balances as of that day: a company that starts January 1 opens with
December 31 balances. Slashbooks gives a new accrual company a starter chart with
receivable, payable, prepaid, fixed asset, accumulated depreciation, accrued
liability, deferred revenue, and customer deposit accounts.

- **From scratch or a spreadsheet:** one `opening` request carries the balances, the
  open invoices and bills, and any remaining prepaid or deferred amounts
  ([recipe 14](accrual-recipes.md#recipe-14-opening-balances-from-scratch)).
- **From a cash-basis Slashbooks company:** `opening draft` fills in the cash
  balances, and you add the receivables and payables
  ([recipe 15](accrual-recipes.md#recipe-15-opening-balances-from-a-cash-company)).
  The cash company is kept for earlier periods; Slashbooks does not convert it in
  place.
- **From QuickBooks:** the next section.

## Turn on accrual for an existing company

A cash company that already has activity can start keeping invoices, bills and
prepaid or deferred balances from a date, without a new company. Choose the
**tracking start**: the first day accrual records are kept, usually the first day of
the next open month. Everything before it stays the cash books you already have.

```sh
books accrual enable preview --entity PATH --tracking-start 2026-04-01 > enable.json
books accrual enable apply --entity PATH --input enable.json
```

The preview lists what will change and writes nothing. Applying it:

- upgrades the ledger to hold accrual records, and keeps a sealed backup of the old ledger;
- records the tracking start in the company file and the ledger, and writes an audit event;
- changes **no** entry, report, export, answer, period lock or published statement, and does not change the default view.

A person runs both commands. A delegated agent can run the preview, never the apply.
The same preview applied again returns `already_applied`. The tracking start cannot be
changed later. The command is refused when the date is inside a closed period, or when
the company was created on accrual (it already keeps accrual books from its first day).

One thing does change: the ledger fingerprint. A readiness package made before this step
carries the old fingerprint, so run `books readiness` again before closing a month.

### Opening items on the tracking start

If invoices or bills are still open on the tracking start, list them once. One `opening`
request carries the open invoices and bills, and any remaining prepaid or deferred
balances. Leave out `lines` and `cutover`: the engine works the balances out from the
rows, dates the one entry on the tracking start, and puts the balancing amount in
`Equity:Opening-Balance-Adjustments` (created if missing). The same tie-out applies: each
control account must equal its rows to the cent. A closed period refuses the entry.

```sh
books accrual opening draft --entity PATH --cutover 2026-04-01 > request.json
books accrual opening preview --entity PATH --input request.json > plan.json
books accrual opening apply --entity PATH --input plan.json
```

An invoice that was open on the start and is collected later is revenue in the cash view
on the day it is collected, and the cash view shows it on its own line. New bills and
invoices dated on or after the start are ordinary records
([recipe 1](accrual-recipes.md#recipe-1-unpaid-bill-including-a-bill-split-across-accounts)).
Do not also list as open an amount that an existing entry already paid; the preview warns
when entries exist on or after the start.

### Choose the default view

The default view decides what `books report`, `books ask` and exports show when you do not
say. After the step above the default stays **cash**, so nothing a person sees changes.
Unpaid invoices and bills appear only in the accrual profit and loss view; their later payments
appear in the cash view.

```sh
books entity set-view PATH --view accrual          # preview
books entity set-view PATH --view accrual --apply  # change it
```

- `accrual` is refused while accrual is off for the company.
- It changes only the default. No entry, lock, report file or published statement changes, and an audit event records the change.
- Pass `--basis cash` or `--basis accrual` to a report to pick the other view for one command.
- **The accrual view starts on the tracking start.** A report or answer for a period that starts earlier is refused and names the first available date. The cash view is available for every period.

### Every output on either view

The default view picks the view of every report output. `--basis cash|accrual` picks the other one for one command.
Every output names the view it shows.

| Output | Cash view | Accrual view |
|---|---|---|
| `report pnl` | The recorded books, or the derived cash view when accrual records exist. | Recognition on the accrual basis. Starts on the tracking start. |
| `report balance-sheet`, `trial-balance`, `general-ledger` (`--basis`) | Balances have no cash view. A cash request on a company that keeps accrual records is refused and names the profit and loss. | Labelled accrual. Starts on the tracking start. |
| `books export --basis` | The P&L and the General Ledger sheets only. The cover says "Cash view (derived ...)" and lists what is not shown. Asking for a balance sheet, trial balance, summary, checks or accrual sheet by name is refused. | The full accrual export, with the accrual sheets. The cover says "Accrual view (accrual figures start on DATE)". |
| `compare period --basis` | Profit and loss only. The balance sheet and trial balance are reported as not compared. The QuickBooks reports must be on the cash basis. | All grains, against QuickBooks accrual reports. |

- A company with no accrual records has one view, the recorded books. Its `--basis cash` output is the ordinary report.
- With accrual on and accrual records present, the cash view is always the derived view, whatever the default view. `books report pnl` and `books export` name the default view, so a person gets it without a flag. The engine's own readers (the statement of cash flows, which must tie to the bank) call the profit and loss with no view and get the recorded books.
- The tax handoff README says "Cash basis book balances" for a company with no accrual records. For a company that turned accrual on and holds accrual records it says that the balances are as recorded: cash entries before the tracking start and accrual records from it, not a tax cash-basis view.
- A comparison on a view other than the default does not read or change the saved differences of the default comparison.
- `--basis` with `--structure` or `--compare` is refused when it names a view other than the default, so a different view is never shown without its label.
- The accrual view of a company that turned accrual on later refuses every range that starts before the tracking start and names that date.

**Questions.** `books ask` follows the same rule. Plain wording such as "cash net income" or "accrual revenue" picks the view for a company that turned accrual on later and holds accrual records or has the accrual default view. With no view wording, the default view answers. Balance questions ("What do we owe?") answer from the accrual records, because balances have no cash form. A company that never turned accrual on, and a company that only turned it on, keep the original wording rules and answers.

**Series.** `profit_and_loss_by_month(entity, from, to, view, pre_tracking)` returns one item per month with its `view`. A series that crosses the tracking start on the accrual view is refused with the coverage error (`pre_tracking="refuse"`, the default). With `"label"`, a month that ends before the start is shown on the cash view and says so (`requested_view` is "accrual"), and a month the view cannot answer is `available: false` with the reason. A month inside an imported QuickBooks summary period has no cash view; the item says so and no figure is guessed.

**Migration bundles.** The bundle carries the default view (`basis`), the capability (`accrual_capability_version`) and the tracking start (`accrual_tracking_start`) together. A bundle is refused when the capability is not 0 or 1, when capability 1 has no accrual ledger, when the tracking start is not a date or has no capability, or when the company file and the ledger disagree about the tracking start. A company that never turned accrual on has the same bundle identity as before.

## Moving existing accrual books from QuickBooks

Ask the agent to bring over the opening balances and start recording new activity
on an agreed date. For a July 1 start, the opening entry is dated June 30 and
carries the balances as of that day. The agent needs a full trial balance, matching balance sheet and fiscal
year-to-date P&L, the chart of accounts, and the detail behind amounts still owed,
prepaid, deferred, or otherwise carried forward. Differences must be resolved
before the opening is accepted.

January through June can be carried as one verified summary. July's P&L shows
July activity; January through July includes that earlier summary once. The
summary cannot provide a separate May report without May's detailed records.
At a financial year boundary, prior earnings belong in retained earnings rather
than the new year's income. A start partway through a month can support review
from that start date through month end; reports identify that limited period.

This is an opening-balance handoff on the same accounting basis. It does not
promise complete historical reconstruction or a cash-to-accrual conversion.
Keep a full backup of the books and their supporting records, not only a journal
export.

## Supporting records from any source

Your agent can collect facts through available connectors, MCP tools, APIs,
exports, emails, or uploaded documents. A special provider integration is not
required for a supported accounting entry. Keep the original records so the
reported amounts can be traced back to them.

Payroll and tax adjustments use calculations supplied by the provider or preparer,
with a dated schedule or supporting document explaining the related balance. The
engine does spread a cost you give it over time, including straight-line
depreciation, but it does not calculate payroll, taxes, inventory costs, tax
depreciation methods, or complex revenue contracts. It does not provide audit
certification. Source text
supplies facts and does not grant permission to record them.

For developers and agents implementing an integration, the
[supporting document and request reference](accrual-intake.md) describes exact
amounts, dates, identity, preview/apply, and authority requirements. Those
contracts remain the source of truth for machine inputs.

Opening schedules begin recognition on the later of the cutover and the first
service date. Future service never recognizes early. The opening preview applies
the same service-date checks as a new schedule: `equal_month` requires whole
calendar months, including the remaining recognition interval. Use `actual_day`
when the remaining service starts partway through a month. The error for a mid-month
`equal_month` opening names `actual_day`. An older request that an earlier engine
accepted with a mid-month `service_start` is now refused with this message.
