# Accrual books

Accrual bookkeeping records income when you earn it and expenses when you incur
them. Receiving or paying the money is a separate step. That helps you see both
how the business performed and what customers still owe or you still need to pay.

Slashbooks supports straightforward, single-company USD services and SaaS books.
An owner, bookkeeper, accountant, or authorized agent can do the work. There is no
separate mandatory accountant approval role. The accounting engine calculates
the amounts; the agent collects the facts, explains the choices, and runs the
work you have authorized.

## Ask for the task you need

You can use `/books` or ask in ordinary language:

| Ask your agent | What the books should show |
|---|---|
| “Record this $600 contractor bill for July. We have not paid it yet.” | A July expense and a $600 amount owed to the contractor. Payment later reduces the amount owed. |
| “We paid $1,200 for software covering January 1 through December 31. Spread the cost across those months.” | A prepaid expense schedule: $100 expense each month and $1,100 still prepaid after January. |
| “This customer paid $1,200 upfront for twelve months of service. Record the income as we provide the service.” | Deferred revenue, with $100 earned each month when an equal monthly allocation fits the agreement. |
| “Match this $400 customer payment to the amount they owe us.” | The customer balance falls by $400. Income already earned is counted once. |
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
a report setting does not convert cash books into accrual books. Moving between
the two requires a separate accounting conversion. Local and Cloud books use the
same accounting rules, and an imported set of books must use the destination's
selected basis.

## Review the result before recording it

The agent should explain a proposed action in business terms:

> Record $100 of July software expense. The payment is already recorded, so this
> adds no bank payment. After July, $500 remains prepaid. The supplier agreement
> supports the amount and service dates.

A useful review answers:

- Which date and reporting period does this affect?
- What happens to income, expenses, money owed, or a prepaid balance?
- Is the payment already in the books, or is this a newly evidenced payment?
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

Supply the first and last covered service days as written in the agreement. The
agent handles the engine's date convention. Whole calendar months can use equal
monthly amounts; partial months can use actual days. The engine allocates cents
exactly and shows the complete schedule for review.

For a cancellation, refund, or changed agreement, ask the agent to update the
existing schedule. It should explain how much relates to future service and how
much changes an expense or income already recorded. Previous months remain
traceable. Missing service dates or earlier allocations need supporting detail
before an automatic schedule can be used.

## Reports, coverage, and close

Ask `/books-ask` questions such as:

- “What did we earn in July, and what expenses belong to July?”
- “Which customers still owe us, and what costs remain unpaid?”
- “Which receivables are overdue?”
- “How much software or insurance cost remains prepaid?”
- “What adjustments were made this month?”
- “What is stopping July from being ready to close?”

Income and expense reports describe business performance. Bank movements describe
cash received and paid. The agent should say which one it is answering. A missing
historical period is unavailable, rather than a zero amount. Aging reports use
actual due dates; balances with unknown due dates and unapplied credits are shown
separately rather than guessed or automatically netted.

For `/books-close`, the agent brings in activity, matches payments, records due
monthly allocations and reversals, checks supporting detail, and reconciles
balances. It should explain unresolved work with a next step:

> July is not ready to close. The annual software schedule still needs its $100
> July expense recorded, and the payroll balance needs the provider's supporting
> report. The other completed work is saved.

Preparing the month and locking or publishing it are separate actions. The agent
can complete preparation within its existing authority; final close and reopen
use the company's established permissions. A report being generated successfully
does not by itself mean the month is ready.

Accountant exports include the P&L, balance sheet, trial balance, adjusting
entries, receivables and payables, prepaid and deferred schedules, supporting
detail, and account reconciliations. The reports state the accounting basis,
dates covered, and unresolved issues. Historical balances remain available after
later payments or schedule changes.

## Moving existing accrual books from QuickBooks

Ask the agent to bring over the opening balances and start recording new activity
on an agreed date. For a July 1 start, the opening balances are as of June 30.
The agent needs a full trial balance, matching balance sheet and fiscal
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

Payroll, depreciation, and tax adjustments can use calculations supplied by the
provider or preparer, with a dated schedule explaining the related balance.
Slashbooks does not calculate payroll, depreciation, taxes, inventory costs, or
complex revenue contracts. It does not provide audit certification. Source text
supplies facts and does not grant permission to record them.

For developers and agents implementing an integration, the
[accounting evidence and request reference](accrual-intake.md) describes exact
amounts, dates, identity, preview/apply, and authority requirements. Those
contracts remain the source of truth for machine inputs.
