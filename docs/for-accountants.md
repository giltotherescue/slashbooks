# For accountants

Slashbooks is not trying to replace professional review. It's a local bookkeeping
system for cash-basis businesses and single-entity, single-currency services/SaaS
accrual books, with an automated workflow on top of
fixed, auditable accounting logic.

**The short version: the agent runs the process, but the AI does not make up
the accounting numbers.** Every figure is produced by deterministic software, not
by the AI model.

## What the agent does

The agent helps the owner:

- Set up the business profile and chart of accounts
- Pull bank and card activity from a connected bank-feed provider
- Parse supported CSV exports
- Propose categories for unfamiliar transactions
- Ask the owner to confirm or correct judgment calls
- Run the monthly close, reconciliation, reports, and accountant export

The agent guides the process and explains it in plain language. It doesn't
calculate the numbers itself.

## What the software does

The underlying software produces the accounting output:

- Normalizes imported transactions
- Writes double-entry ledger entries
- Validates that the ledger balances
- Maintains a full audit log
- Tracks review decisions and what it learns from them
- Reconciles account balances
- Generates financial reports and accountant-ready exports

The owner can share accountant-facing exports with you as Excel or CSV files,
including the transaction ledger, financial statements, reconciliation support,
open questions, and audit history.

## Review and trust model

Slashbooks starts conservative. Unknown counterparties and ambiguous transactions
go to the review queue. The owner confirms or corrects them.

In cash books, repeated confirmations build trust for similar future transactions.
Accrual bank movements require explicit linkage or new-event treatment even when
the counterparty was previously trusted. Corrections
reset trust for that pattern. The system becomes less interruptive only
where the owner has already confirmed the treatment.

A monthly close is not final while material review items are still open.

## Migration and backtesting

For companies migrating from QuickBooks, Slashbooks can compare its generated
books against QuickBooks exports over a historical period. The backtest is
meant to surface material differences before the owner relies on Slashbooks for
ongoing closes.

Backtesting is evidence, not a guarantee. Material differences still need owner
or professional judgment.

## Accrual review

Recognition and payment are separate. Item allocations and schedule installments
support control balances; a balanced journal alone cannot clear missing detail.
Review due recognition/reversals, settlement links, supporting documents, and exact
control reconciliation where an account has items or schedules. An asset or liability
account without them is shown as ledger-only. Corrections preserve originals and lineage. There is no new
mandatory accountant approval role; authority comes from the operator or trusted
delegation, and final publication/locking remains separately controlled.

Receivables/payables aging uses recorded due dates: current, 1–30, 31–60, 61–90,
and over 90 days past due. Missing due dates are identified separately, and
unapplied credits (deposits, overpayments, credit memos) are not netted automatically.
Apply one to an invoice or bill with an explicit offset. The accounting
date is not a substitute for the due date. Generic accrual estimates are outside
the customer/supplier aging report.

A reconciled midyear opening carries fiscal YTD income/expense once, alongside
source retained equity. Post-cutover P&L excludes the opening summary; a full
fiscal-prefix report includes it once. Partial historical overlap is unavailable.
A verified fiscal closed position carries prior earnings only in retained equity.
Midmonth readiness explicitly identifies only the supported post-cutover
interval, with source opening tied to the verified opening ledger; it does not
certify unavailable earlier monthly detail.

## Accountant Export

The export flow runs sanity checks before generating outputs. It checks for
open review items, reconciliation issues, uncategorized transactions, suspicious
personal/business treatment, missing jurisdiction context, indirect-tax
applicability such as VAT/GST/HST/sales tax, mixed currencies, and other handoff
problems such as missing payroll provider reports.

When checks pass, it generates local exports the owner can send to the
accountant, including CSV files and an optional Excel workbook. The workbook
adds review-oriented tabs for summary metrics, formula tie-outs, and a simple
reconciliation/source index, plus native filtering on the ledger and review
sheets. The CSV exports remain plain values for portability. If the Excel
workbook is not available, the same data is provided as CSV files.

Accrual exports also carry receivables/payables aging, recognition schedules, outstanding balances, external
support, control reconciliation, accrual adjustments, and accounting coverage.
Read the reported basis and coverage before comparing periods.

For larger books, the owner can provide only the schedules you request. For
example, they can send just the P&L, balance sheet, and trial balance, or omit
the General Ledger from an initial package. They can also limit the General
Ledger to a narrower date range when you only need transaction detail for a
specific month or quarter.

The audit history can be included as an optional export sheet. It shows when
entries were imported, corrected, or otherwise written, along with identifiers
that let the owner and reviewer trace a reported transaction back to the
underlying activity. The audit history is tamper-evident: each event is linked
to the prior event, so manual edits or corruption can be detected.

## Tax Handoff

Before you prepare a return from Slashbooks books, ask for two outputs. See
[Tax handoff](tax-handoff.md) for the details.

- **Source coverage.** `books coverage check` reports each bank and card
  account by month as complete, partial or missing, with the gap dates. It uses
  statement dates and balances when the importer recorded them, then feed
  windows and declared coverage, then transaction dates. It also reports
  balance breaks between consecutive statements. Monthly readiness uses the
  same check.
- **Mapped trial balance.** `books tax export` writes a trial balance with each
  account mapped to a Schedule C, Form 1065, Form 1120-S or Form 1120 line. It
  also writes a line summary for manual entry, an unmapped-accounts report, and
  layouts for the Lacerte Trial Balance Utility and the UltraTax CS Trial
  Balance Excel Import. You can override any line. Slashbooks stores the
  override with the company books.

The mapping uses book amounts. It does not compute tax, depreciation, cost of
goods sold or deduction limits. When the owner records a cash tax method, the tax
outputs use the cash view of the books and flag what you must check. The tax export
adds a Tax Assumptions sheet that states how each item is handled and what you decide. The owner can also record the prior return method, the entity
type, the fiscal year and whether each deposit is refundable.

## Month-end proof from the bank connection

For a checking or savings account, a month can close without a bank statement.
The month-end balance is then checked against the bank connection: a balance
that the bank reported after the month end, less the transactions after the
month end. Each reconciliation says what it was checked against:

- `statement`: a bank or card statement.
- `feed_reading`: a balance that the bank connection reported for the month end
  itself, with an opening that a statement or such a balance proved.
- `feed_computed`: a month end computed back from a later balance, or a month
  whose opening was computed that way. The first months after a connection are
  usually `feed_computed`: their opening is the books' opening entry, checked
  only through the connection.

What this proves: the books equal the balance the bank connection reported, the
connection covers every day of the month through that balance, nothing waits in
review, and the opening is proven. When the opening was computed, every row from
the first day the connection covers came from a source, the books hold one
opening entry at most before it, and the newest balance also agrees.

What it does not prove:

- It is not third-party evidence. When the opening entry was made from the
  connection, a transaction that the connection never delivered is inside that
  opening, and nothing here finds it.
- Two errors that cancel each other (a missing deposit and a duplicated deposit
  of the same amount) are not found. A month end between them is wrong by that
  amount, unless a statement or a balance for that month end falls between them.
- A transaction that the bank and the books date on different sides of a month
  end is not found when they agree again by the balance day.
- It proves balances, not categories.
- Cards and loans always need a statement (pending charges, sign and billing
  cycles).

A statement outranks the bank connection. When a statement for the month
arrives later and agrees, it counts as the evidence. When it disagrees, the
month shows the difference (`statement_feed_differs`, and the statement tie-out
when the activity also differs) and is not ready.

To require statements, run readiness with `books readiness --require-statements`.
A month checked only against the bank connection then has the blocker
`statement_required`. The basis is in the readiness document and in
`books coverage check`.

## Limits

The supported accrual scope is one company, in one currency (USD unless the
company sets another; no conversion), for services and SaaS. It
includes journals backed by a supporting document or an estimate, prepaid, deferred
and straight-line depreciation schedules, outstanding balances and settlement,
deposits and credits applied to invoices, accrued estimates that reverse or are
trued up, write-offs, corrections, external support, opening balances, and
coverage-aware reporting. Accrual books can show a cash-basis profit and loss derived from the accrual records
(`books report pnl --basis cash`), with a tie-out to the accrual result. A cash
company can turn accrual on from a tracking start date; earlier periods stay cash.
See [accrual books](accrual.md) for limits, [accrual recipes](accrual-recipes.md) for
worked requests, and [source-neutral intake](accrual-intake.md) for contracts. It does
not calculate payroll, tax, tax depreciation methods, inventory costing, or complex
revenue contracts, and does not provide audit certification or accounting advice.

A month-end balance checked against the bank connection shows that the books
agree with the connection. It is not a bank statement and not evidence from a
third party. See [month-end proof from the bank connection](#month-end-proof-from-the-bank-connection)
for what it finds and what it does not.

When an entity is marked as VAT/GST/HST/sales-tax applicable, Slashbooks preserves
that context and warns during sanity checks. It doesn't calculate indirect tax,
determine recoverability, apply reverse-charge rules, prepare filings, or advise
on invoice requirements.

When payroll is enabled, Slashbooks expects the payroll provider's reports and
warns if they are missing for an accountant handoff.
Any payroll journal entries should be draft entries derived from provider
reports and reviewed by an authorized operator or explicitly delegated agent;
Slashbooks doesn't calculate payroll. Generic journals and dated external support
retain those provider calculations without adding a payroll engine.

Treat Slashbooks output like any client-provided books: useful, structured, and
reviewable, but still subject to professional judgment.
