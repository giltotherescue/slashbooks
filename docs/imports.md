# CSV, QuickBooks, and file imports

Slashbooks reads three kinds of files: bank and card statement files (CSV, OFX,
QFX and QBO), the older American Express activity CSV path, and QuickBooks
migration/backtest exports.

## Statement files: CSV, OFX, QFX and QBO

Use `books statement` for historical bank and card activity, for example a
catch-up year, a closed account, or a period that the bank feed cannot reach.
Statement files are a fallback, not the preferred ongoing workflow.

Supported files:

- Bank and card CSV exports in most common layouts: one signed amount column,
  separate debit and credit columns, or unsigned amounts with a debit/credit
  type column. The reader detects the header row (also after summary lines at
  the top of the file), files without a header, the date format, comma or
  semicolon separators, and comma or dot decimals.
- OFX, QFX and QBO (Web Connect) files, OFX 1.x and 2.x. Each bank or card
  statement in the file is read with its `FITID` values, statement period and
  ledger balance.

Slashbooks books only USD activity. A file with another currency stops with
the error code `unsupported_currency`.

### 1. Inspect the file

```sh
books statement inspect ~/Downloads/checking-2025.csv \
  --entity ~/Documents/books/acme-co \
  --ledger-account Assets:Bank:Checking
```

The command changes nothing. It prints JSON with a `status`:

| Status | Meaning |
|---|---|
| `ready` | A saved profile matches the file. Import it. |
| `ready_to_confirm` | Detection decided every field. Show the proposal and `preview` to a person, then save the profile. |
| `needs_mapping` | Each field in `uncertain` needs an explicit decision. `evidence` says why, and `options_for_uncertain` names the option to use. |
| `error` | The file cannot be read. `error.code` gives the reason, and `error.rows` gives the line numbers when rows are the problem. |

Detection does not guess. For example, if every date fits both month/day and
day/month, `date_format` is uncertain. If the file has one amount column and
no reliable signal for the sign, `sign` is uncertain. `--ledger-account` is
optional. It tells detection whether the account is a bank account or a card,
which lets a running balance or card payment rows decide the sign.

### 2. Save a profile for the account

```sh
books statement map ~/Downloads/checking-2025.csv \
  --entity ~/Documents/books/acme-co \
  --profile chase-checking \
  --ledger-account Assets:Bank:Checking
```

The profile is saved in `entity.json` under
`csv_account_mappings["statement:<profile>"]`. Next year's export from the
same account uses the same profile. If a field is uncertain, the command
stops with `needs_mapping` until you supply it, for example
`--date-format %d/%m/%Y` or `--sign inverted`. You can also set columns by
name or 1-based number: `--date-column`, `--description-column`,
`--amount-column`, `--debit-column`, `--credit-column`, `--balance-column`,
`--reference-column`, `--status-column`, `--direction-column`, and
`--header-row` (0 for a file with no header).

Sign values:

- `standard`: positive amounts are money in (a deposit, or a payment to a card).
- `inverted`: positive amounts are charges or withdrawals.
- `debit_credit`: separate debit and credit columns.
- `direction_column`: unsigned amounts and a debit/credit type column.

For an OFX file with more than one account, add `--ofx-account <last four>`.
Each OFX account needs its own profile. To share a period with a connected
bank feed, add `--boundary YYYY-MM-DD --side before|after`, which has the
same meaning as for the Amex CSV path below.

### 3. Import

```sh
books statement import ~/Downloads/checking-2025.csv \
  --entity ~/Documents/books/acme-co --profile chase-checking --dry-run

books statement import ~/Downloads/checking-2025.csv \
  --entity ~/Documents/books/acme-co --profile chase-checking
```

`--dry-run` shows the counts and duplicate decisions without changes. A CSV
import needs `--profile`. An OFX import finds the profile from the account in
the file. Imported rows go through the same importer as `books ingest`, so
categorization, the review queue and the audit log work the same way.

The command exits with 0 on success, 1 on an error, and 3 when a mapping is
necessary.

### Duplicate rules

Slashbooks never posts the same activity twice. The rules apply in this order:

1. **Same source ID.** OFX rows use the account identity and `FITID`. CSV rows
   use the ledger account, date, amount, normalized description, reference and
   the occurrence number of identical rows in the file. Two identical coffee
   purchases on one day stay two transactions, and a second import of the same
   or an overlapping file skips them.
2. **Same account, date, amount and description** as an existing entry or
   review item from another source ID: the row is held as a duplicate
   candidate.
3. **Same account and amount, with a date within three days** of an existing
   entry or review item from another source (a bank feed or another
   statement profile): the row is held as a duplicate candidate.

Held rows are not posted. They appear in `books queue list`. Resolve each one:

```sh
books queue resolve-duplicate --entity ~/Documents/books/acme-co \
  --source-id <id> --decision duplicate   # or distinct
```

Pending rows (a status column that says pending) and identical repeated OFX
transactions are skipped and counted in the result.

### Statement period and balances

Each import also writes a statement record that `books coverage check` reads.
The record format is the statement record contract in
[tax-handoff.md](tax-handoff.md#statement-record-contract-schema-version-1).
That contract is the only format; the importer adds details as extra fields.

The record goes into coverage only when the statement period is known:

| File | Period | Balances |
|---|---|---|
| OFX, QFX or QBO | `DTSTART` and `DTEND` of the statement. | The `LEDGERBAL` amount is the closing balance only when its `DTASOF` date is the last statement day. The opening balance is then the closing balance less the transactions in the file. |
| CSV with a printed summary (opening and closing balance rows with dates) | The summary dates. | The summary balances (bank accounts only). |
| Any other CSV | Not known. Ask the person for the period printed on the statement. | The running balance column, when it follows the amounts. |

If the `LEDGERBAL` date is after the last statement day (many banks use the
download date), the importer keeps the balance as `ledger_balance` and does not
use it as the closing balance. A balance from a later date can include
activity that is not in the file.

When a CSV has no printed period, the import posts the transactions and
reports `"coverage": "needs_period"`. The record waits in
`ingestion/statements/unconfirmed/`, and the coverage check does not read it.
To confirm the period, import the same file again with the dates from the
statement. The transactions are not imported again:

```sh
books statement import ~/Downloads/checking-2026-q2.csv \
  --entity ~/Documents/books/acme-co --profile chase-checking \
  --period-start 2026-04-01 --period-end 2026-06-30
```

Every transaction in the file must fall inside the period. To add or correct
the balances from the printed statement, pass `--opening-balance` and
`--closing-balance`. Use the statement presentation: money held for a bank
account and money owed for a card. If the balances do not agree with the
transactions in the file, the result has a warning.

```sh
books statement list --entity ~/Documents/books/acme-co
```

The list shows each record with the profile, the ledger `account`, the account
identity (the institution ID, account type and last four characters; never the
full account number), `period_start`, `period_end`, `period_source`
(`statement`, `file_summary`, `person` or `transactions`), `period_confirmed`,
the balances with their dates and `balance_source`, and the net transaction
total. Records are in `ingestion/statements/<statement_id>.json`, and a copy of
each imported file is kept under `ingestion/statements/files/`.

## PDF and image statements

The engine does not read a PDF or an image. A reader outside the engine, either a
set of rules or a model, turns the statement text into one JSON object. The
engine treats that object as untrusted. It checks the object with code, and it
records the statement only when every check passes. Use CSV, OFX, QFX or QBO
files when you have them; they need no extraction.

### The extraction contract

```json
{
  "schema_version": 1,
  "source": "model",
  "account_kind": "card",
  "period_start": "2026-05-04",
  "period_end": "2026-06-03",
  "period_source": "printed",
  "opening_balance": "0.00",
  "closing_balance": "5732.51",
  "confidence": "high",
  "row_order": "chronological",
  "transactions": [
    {"date": "2026-05-05", "description": "ORCHARD OFFICE SUPPLY", "amount": "282.00"}
  ],
  "excluded": ["Account summary: Previous balance $0.00"]
}
```

| Field | Meaning |
|---|---|
| `source` | `rules` or `model`: who read the statement. |
| `account_kind` | `bank`, or `card` for any liability account such as a loan. |
| `period_start`, `period_end` | Inclusive dates. Both are required. |
| `period_source` | `printed` (default) or `derived`, when the reader did not read the start date from the statement. |
| `opening_balance`, `closing_balance` | Decimal strings in the statement's own presentation: money held for a bank account, money owed for a card. |
| `transactions` | `date`, `description` and `amount`. `running_balance` and `confidence` are optional. `amount` is the effect on the printed balance: for a bank account a deposit is positive; for a card a purchase is positive and a payment is negative. |
| `confidence` | Optional `high`, `medium` or `low`. `low` always goes to review. |
| `row_order` | Optional. Say `chronological` when the statement prints rows in date order, and the engine checks the order. |
| `excluded` | Rows that the reader left out, such as pending rows. For the reviewer only. |
| `confirmation` | Optional `{name, at}` of a person who corrected or confirmed the numbers. The engine records it as `balance_source: manual_confirmed`. It never replaces a check. |

Amounts are plain numbers with at most two decimals. The engine refuses a sign
such as parentheses or `CR`, a currency symbol and a thousands separator.

### The checks

`books statement validate FILE [--ledger-account ACCOUNT] [--cross-check FILE]`
runs these checks and changes nothing:

1. The period and both balances are present.
2. The opening balance plus every transaction equals the closing balance, to
   the cent.
3. Each printed running balance equals the balance before the row plus the
   amount of the row.
4. Every date is a real date inside the period. A card row may be dated up to
   7 days before the period starts. Rows are in date order when `row_order` is
   `chronological`. A wrong date can hide in a balance that still adds up, so
   this check is separate from the tie-out.
5. The account kind agrees with the ledger account, so a card cannot be read
   with bank signs.
6. The reader did not mark the statement low confidence.
7. With `--cross-check`, a second reading of the same statement agrees.

The result has `status` (`verified` or `needs_review`), `reasons` in plain
words with the row numbers that broke, and the `tieout` figures. The command
exits with code 4 when the statement needs review.

### Record or review

```sh
books statement map-extracted --entity ~/Documents/books/acme-co \
  --profile chase-checking-pdf --ledger-account Assets:Bank:Checking
books statement import-extracted extraction.json --entity ~/Documents/books/acme-co \
  --profile chase-checking-pdf --evidence statement.pdf --dry-run
```

`map-extracted` saves a confirmed profile once for an account. Do it after the
owner confirms the account. `import-extracted` runs every check again and then
uses the same duplicate plan and the same statement record as the other
formats, so the statement feeds `books coverage check`. Remove `--dry-run` to
record it.

**A statement that fails a check is never recorded.** The command stages no
row and writes no coverage record, so the month stays a gap. It writes a review
record in `ingestion/statements/needs-review/` with the reasons, the rows and
the evidence file, and `books statement list` shows it as `needs_review`. A
corrected reading of the same statement records normally and closes the review
record. A person's confirmation does not lift a failed check. If the printed
statement itself does not add up, ask the bank for a corrected statement.

### Sign conventions

Two conventions meet at one place. A statement and a statement record use the
statement presentation: money held for a bank account, money owed for a card or
loan, so a card purchase is positive. Transactions in the ledger use the ledger
sign, so a card purchase is negative because it lowers the account. The
function `presentation_sign` in `statement_import.py` converts between them, and
every importer calls it. Balances you pass with `--opening-balance` and
`--closing-balance` and the balances in `tax-handoff.md` are always in the
statement presentation.

## American Express activity CSVs

The older `books connector csv` path supports American Express activity-export
CSVs. New imports can use `books statement` instead. CSVs are a fallback, not
the preferred ongoing workflow. Use them for canceled cards, unsupported
accounts, accounts you choose not to connect to a live bank-feed provider,
pre-feed history, or any period where the bank feed cannot reach far enough
back.

For normal ongoing bookkeeping, connected bank-feed transaction and balance data
should be the default source. BankSync is the current live provider, but shared
import code uses the provider-neutral bank-feed contract. You should not need to
download bank CSVs every month just to keep the books current.

## Direct connector downloads

Slashbooks can also pull raw data directly from a few common providers. These
downloads save source JSON in the company books project; they do not sync data
back to the provider.

For Stripe, set `STRIPE_SECRET_KEY` in the company `.env` file, then run:

```sh
books connector stripe account

books connector stripe download \
  --from 2026-01-01 \
  --to 2026-01-31 \
  --output ~/Documents/books/acme-co/ingestion/stripe/stripe-2026-01.json
```

The account command is a quick sanity check that the key points at the expected
Stripe account. The download command saves Stripe balance transactions and
payouts for the period. For Stripe Connect platform keys, pass
`--stripe-account <acct_...>` before the subcommand.
This raw downloader is not the gross-sales accounting workflow: it lacks the
complete invoice/Checkout and revenue-mapping evidence. Use
[Stripe accounting](stripe-accounting.md) for approved gross/fee/refund postings
and payout-to-bank clearing; do not post processor payouts as new income.

For Mercury, set `MERCURY_API_KEY` in the company `.env` file, then run:

```sh
books connector mercury accounts

books connector mercury download \
  --from 2026-01-01 \
  --to 2026-01-31 \
  --all-accounts \
  --output ~/Documents/books/acme-co/ingestion/mercury/mercury-2026-01.json
```

Use `books connector mercury accounts` first to confirm which operating, credit,
and treasury accounts the key can see. For a subset of operating accounts,
replace `--all-accounts` with one or more `--account-id <account-id>` flags.

Mercury downloads use posted dates by default because those are normally the
dates accountants expect for bank activity. Use `--date-field created` only when
you intentionally want Mercury API `createdAt` filtering.

Custom provider helpers should live in the company books project, not this plugin
repository. Put their outputs, scripts, and notes under `ingestion/custom/` so
they are preserved across plugin upgrades and remain scoped to that company.

Inspect a file:

```sh
books connector csv inspect ~/Downloads/amex-activity.csv
```

Ask Slashbooks to propose an account mapping:

```sh
books connector csv propose-mapping \
  --entity ~/Documents/books/acme-co \
  ~/Downloads/amex-activity.csv
```

Confirm the mapping after reviewing it:

```sh
books connector csv confirm-mapping \
  --entity ~/Documents/books/acme-co \
  --ledger-account Liabilities:CreditCard:Amex-CSV \
  --boundary 2026-03-31 \
  --side before \
  ~/Downloads/amex-activity.csv
```

The boundary options prevent overlap when a CSV covers history before the
connected bank feed started. `--side before` imports rows on or before the
boundary date; `--side after` imports rows after it.

Parse the CSV into normalized transaction JSON:

```sh
books connector csv parse \
  --entity ~/Documents/books/acme-co \
  --output ~/Documents/books/acme-co/ingestion/amex-activity.json \
  ~/Downloads/amex-activity.csv
```

## QuickBooks Exports

QuickBooks exports are used for migration, opening balances, and backtests.
Place the exported Excel (`.xlsx`) or CSV files in the company folder under
`ingestion/quickbooks/` and run:

```sh
books qb inventory ~/Documents/books/acme-co/ingestion/quickbooks
```

The following default cash inventory recognizes the files needed for comparison.
For accrual, explicitly pass `--basis accrual` and an opening `--fiscal-start`;
collect full TB/BS/YTD P&L plus supporting detail using [accrual intake](accrual-intake.md#quickbooks-opening).
Do not route accrual opening through the cash balance-sheet fallback.

The cash reference set is:

- Chart of Accounts
- Trial Balance, cash basis, as of the day before the Slashbooks cutover/start date
- Balance Sheet, cash basis, as of the day before the Slashbooks cutover/start date
- Balance Sheet, cash basis, as of the backtest end date
- Profit and Loss, cash basis, for the backtest period
- General Ledger, cash basis, for the backtest period
- Transaction Detail by Account, cash basis, for the backtest period

Slashbooks writes new imported entries into the canonical `ledger.sqlite` store.
Reports, reconciliation, and accountant exports read that store directly. When a
plain ledger file is needed for inspection or another tool, generate a
Beancount snapshot explicitly:

```sh
books ledger snapshot --entity ~/Documents/books/acme-co \
  --output ~/Documents/books/acme-co/books.beancount
```

For accountant handoffs with large transaction volume, export only the sheets
needed or narrow the General Ledger period:

```sh
books export --entity ~/Documents/books/acme-co --from 2026-01-01 --to 2026-12-31 \
  --sheets pnl,balance-sheet,trial-balance

books export --entity ~/Documents/books/acme-co --from 2026-01-01 --to 2026-12-31 \
  --gl-from 2026-10-01 --gl-to 2026-12-31
```

If you're not sure what to export yet, decide the Slashbooks start date first.
For migrations, that date is the opening-balance cutover date; the prior day is
the as-of date for opening-balance reports.

Reference reports must match the company basis. For cash books, use cash basis
where QBO offers it; for accrual books, use the explicit same-basis workflow above.
For cash books only, if QBO cannot provide a cash-basis Trial Balance, inventory can still check whether the prior-period cash-basis
Balance Sheet fallback is available.

Use `/books-qbo-fetch` to collect the set through any browser integration available
to the agent. It handles signed-in and signed-out sessions, calculates the two
required Balance Sheet dates, stores untouched CSV/XLSX exports under
`ingestion/quickbooks/`, and runs inventory before handing the files to onboarding
or backtesting.

For opening balances:

```sh
books qb import-opening \
  ~/Documents/books/acme-co/ingestion/quickbooks \
  --entity ~/Documents/books/acme-co \
  --cutover 2026-01-01 \
  --source balance-sheet
```

The Chart of Accounts export is required so Slashbooks never guesses whether a
row is a balance-sheet or prior-period activity account. Opening entries contain
only Assets and Liabilities plus one `Equity:Opening-Balances` offset.

For confidence testing against historical QuickBooks data:

```sh
books backtest run \
  --entity ~/Documents/books/acme-co \
  --qb-folder ~/Documents/books/acme-co/ingestion/quickbooks \
  --from 2026-01-01 \
  --to 2026-03-31
```

Generated accountant exports also include per-sheet CSV files, even when the
optional Excel workbook dependency is not installed. To enable `.xlsx` output,
install the extra with `python -m pip install "agent-books[xlsx]"` or, from a
development checkout, `python -m pip install -e ".[xlsx]"`.

## Bank statements and manual downloads

Some bookkeepers ask for bank CSV exports because that's how they import
transactions into other accounting software. With Slashbooks, those CSVs are not
necessary when a connected bank-feed provider has complete transaction and
balance data for the period.

PDF statements can still be useful as supporting records, especially for year-end
review, audits, loan applications, or when a bank feed has a gap. Slashbooks does
not currently require entrepreneurs to download monthly statements as part of the
normal close.
