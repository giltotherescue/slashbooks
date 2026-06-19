# For accountants

Slashbooks is not trying to replace professional review. It's a local bookkeeping
system for simple cash-basis businesses, with an automated workflow on top of
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

The canonical records live in local plain-text files the owner can share with
you: the ledger, the chart of accounts, and a complete audit log.

## Review and trust model

Slashbooks starts conservative. Unknown counterparties and ambiguous transactions
go to the review queue. The owner confirms or corrects them.

Repeated confirmations build trust for similar future transactions. Corrections
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

## Limits

Slashbooks is currently scoped to simple cash-basis bookkeeping. It is not designed
for payroll, inventory, revenue recognition, multi-user accounting controls,
complex accrual accounting, audit engagements, or tax advice.

When an entity is marked as VAT/GST/HST/sales-tax applicable, Slashbooks preserves
that context and warns during sanity checks. It doesn't calculate indirect tax,
determine recoverability, apply reverse-charge rules, prepare filings, or advise
on invoice requirements.

When payroll is enabled, Slashbooks expects the payroll provider's reports and
warns if they are missing for an accountant handoff.
Any payroll journal entries should be draft entries derived from provider
reports and confirmed by the accountant; Slashbooks doesn't calculate payroll.

Treat Slashbooks output like any client-provided books: useful, structured, and
reviewable, but still subject to professional judgment.
