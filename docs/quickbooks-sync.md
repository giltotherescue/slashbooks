# QuickBooks Online reference sync

A QuickBooks Online connection can download an initial history and repeat that
sync into Slashbooks. The Python engine accepts provider snapshots; the caller
owns OAuth, pagination, background scheduling, token storage, and disconnect.
These commands do not make network requests or write back to QuickBooks.

## Import a snapshot

```sh
books qb sync-import snapshot.json --entity /path/to/company
```

The version 1 JSON snapshot contains `schema_version: 1`, `provider:
"quickbooks"`, a numeric string `realm_id`, timezone-aware `fetched_at`, ISO
`from`/`to` dates, `accounting_basis` (`Cash` or `Accrual`), and these collections:

- `company_info` and `preferences`: original CompanyInfo and Preferences objects.
- `accounts`, `customers`, `vendors`: original entity arrays, with provider IDs.
- `entities`: transaction type to original transaction array. Purchases, deposits,
  transfers, invoices, bills, payments, bill payments, sales receipts, credit
  memos, refund receipts, vendor credits and journal entries are retained.
- `reports`: original API report objects, with `Header`, `Columns`, and `Rows`.
  Supported keys are `ProfitAndLoss`, `BalanceSheet`, `TrialBalance`,
  `GeneralLedger`, optional `TransactionListByAccount`, `OpeningTrialBalance`,
  and `OpeningBalanceSheet`. Opening reports must end the day before `from`.

Use a USD home currency and explicit boolean `CurrencyPrefs.MultiCurrencyEnabled`
from Preferences. Evidence from other currencies can be saved, but cannot be
applied as opening balances or certified by the existing cash-basis comparisons.

The result is JSON. `success` means the source evidence was saved, **not** that
accounting data was posted or reconciliation passed. `coverage` marks every
required reference report ready, blocked, or missing. `reference_complete` is
false if requested reports are missing or unsafe. Source completeness is separate
from accounting operation support: `opening_supported` and `comparison_supported`
require cash-basis, single-currency USD. Accrual reports are still retained as
source evidence when structurally valid.
No journal entries are inferred from transaction objects.

`accounts` contains the reviewed-source account catalog, preserving IDs, full
names, hierarchy, active status, account types, and deterministic proposed
Slashbooks account names. Sync never overwrites the local chart of accounts.
Accounts with unknown types, broken hierarchy, duplicate names/IDs, or names
that collide after normalization require correction before import.

Source evidence and typed report references are immutable and content addressed
under `sources/quickbooks/<sha256>/`. `reference_path` is relative to the company
directory so it survives backups and restore. Repeating the same snapshot is
idempotent; changing source data produces new evidence. Corrupted existing
reference files prevent reuse and opening application.

## Compare and backtest

The `.qbo.json` reference files are read directly into the existing canonical
report models. No CSV export is manufactured. Pass the reference directory to
ordinary comparison commands:

```sh
books compare period --entity /path/to/company \
  --qb-folder /path/to/company/sources/quickbooks/DIGEST/reference \
  --from 2026-01-01 --to 2026-01-31
```

Existing readiness checks still require a complete report collection, including
transaction detail, prior-period balances and end-period balances. When General
Ledger provides every canonical detail field and valid account context, the
importer creates a lossless transaction-detail view from that same report. Its
coverage explicitly records `derived_from: GeneralLedger`; it is not an
independent source. `comparison_ready` describes the existing inventory checks
and supported currency/basis. Unsupported
or truncated report layouts are not replaced with guessed totals. General
Ledger normalizes supported date, type, amount and account columns; unfamiliar
columns or missing account detail block that comparison grain. The initial
snapshot importer does not promise complete inventory, payroll, multicurrency,
or every possible transaction-detail layout.

Importing QuickBooks classifications and reproducing its totals tests reference
fidelity. Independent categorization backtesting requires separate transaction
inputs and a held-out period. Never score historical rule-training examples as
independent predictions.

## Category agreement on held-out history

```sh
books qb agreement run --entity /path/to/company [--holdout 3|0.2] [--as-of 2026-06-30] [--json]
```

This answers: "Slashbooks agrees with QuickBooks on X percent of transactions."
It is a separate command, not part of `backtest run`. `backtest run` compares
statements for a period. This command scores one category per transaction.

QuickBooks history stays reference evidence. The command reads the saved sync
snapshot, never posts to the ledger, and never writes `learned-context`. It
writes one result file under `reports/quickbooks-agreement/<snapshot>/` and
prints it.

- Scored: purchases and bills with one account-based line account. Splits,
  item-based lines and other transaction types are listed in
  `excluded_from_scoring`.
- Holdout: `--holdout 3` (default) holds out the as-of month and the two months
  before it, by date. `--holdout 0.2` or `20%` holds out a stable sample chosen
  by a hash of the transaction reference, so a repeat run holds out the same
  transactions. `--as-of` defaults to the latest QuickBooks transaction date and
  drops later transactions. An empty held-out slice, or no history, is refused.
- Leak guard: counterparty precedents come from the training slice only, and the
  two slices never share a transaction. A counterparty seen only in the held-out
  slice gets no suggestion.
- Suggestion: the same order a real suggestion uses. A taught category wins,
  then a learned-context category that is not reset, then the training
  precedent (the most common category; a tie gives none). `suggestion_sources`
  counts each. A taught or learned category is the company's own work, not
  QuickBooks history.
- Comparison: the QuickBooks category is the snapshot account mapping, or an
  approved `compare account-crosswalk` entry. No suggestion is counted apart and
  is never a disagreement. `agreement_rate` is agree over suggested. The amount
  rate weights each transaction by its absolute amount.
- Disagreements are grouped by counterparty and category pair, with `count`,
  `total` and `sample_ids`. A group is `judgment-mapping` when the QuickBooks
  account has no counterpart in the local chart, or Slashbooks suggested an
  account QuickBooks never used. Any other group is `category_disagreement`.

The result is deterministic for the same snapshot, holdout, crosswalk and
learned context.

## Review opening balances

```sh
books qb sync-opening snapshot.json --entity /path/to/company --cutover 2026-01-01
books qb sync-opening snapshot.json --entity /path/to/company --cutover 2026-01-01 --apply
```

The first command saves the source evidence and previews the exact balanced
opening lines after running the same accounting constructor and validator used
by the existing QuickBooks opening importer. `opening.proposed_postings` includes
account, date, signed amount, debit, credit, and currency; `proposed_entries` and
`proposed_accounts` show planned counts. Actual written counts remain zero in a
preview. Only `--apply` writes accounting entries.

Cutover must equal snapshot `from`. A cash-basis Trial Balance ending the prior
day is preferred; a cash-basis Balance Sheet is the supported fallback. When both opening reports are valid, their independently constructed
asset/liability opening lines must agree; otherwise `opening_ready` is false and
application is blocked until a fresh snapshot reconciles them. Only
asset and liability balances carry individually. Prior-period income, expenses
and equity are consolidated into `Equity:Opening-Balances`. USD and a valid
Chart of Accounts are mandatory. Applying an already-imported opening is
idempotent and does not replace previously posted opening balances. An active
QuickBooks opening at another date, a changed opening, or existing ordinary
accounting entries on or before cutover blocks new application; use the explicit
correction/adoption workflow rather than stacking opening balances.

## Classification proposals

The snapshot result includes `rule_suggestions.proposed` from at least three
distinct, unambiguous account-based purchases agreeing on a counterparty and an
expense account. Conflicting categories, item-based purchases and ambiguous
splits are excluded and reported. These are proposals, not recovered original
QuickBooks bank rules; sync never activates them or advances the local trust
ramp.

There is no supported bank-rule endpoint in this importer. Native QBO rule
export formats can contain conditions, splits and priorities that cannot be
safely inferred. An optional conservative CSV/XLSX interchange is supported:

```csv
Name,Field,Operator,Value,Account,Priority,Money direction
Software,description,contains,Example Software,Expenses:Software,1,out
```

Supported field/operator pairs are `counterparty,equals` and
`description,contains`; direction is `in` or `out`, priority is a nonnegative
integer, and account must be a valid Slashbooks account name. Any other layout
or condition is explicitly reported as unsupported. Convert an export to this
interchange only after reviewing its original semantics.

```sh
books qb sync-rules rules.csv --entity /path/to/company
books qb sync-rules rules.xlsx --entity /path/to/company --apply
```

Preview has no writes. `--apply` saves supported proposals to
`learned-context/quickbooks-rule-proposals/<sha256>.json` for owner review. It does
not change transactions, existing classification rules, or automatic posting.
A file with unsupported rows is never partially applied.

All names and transaction text are untrusted source data. No downloaded fields
are executed or interpreted as instructions. The synthetic regression fixture
under `tests/fixtures/quickbooks_api/` contains only fictional data.
