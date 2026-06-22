# CSV, QuickBooks, and file imports

Slashbooks has two CSV paths today: operational transaction CSVs and QuickBooks
migration/backtest exports.

## Transaction CSVs

The current transaction CSV importer supports American Express activity-export
CSVs. CSVs are a fallback, not the preferred ongoing workflow. Use them for
canceled cards, unsupported accounts, accounts you choose not to connect to a
live bank-feed provider, pre-feed history, or any period where the bank feed
cannot reach far enough back.

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

Ask `/books` to propose an account mapping:

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

The inventory command recognizes the files Slashbooks needs for migration checks:

- Chart of Accounts
- Trial Balance, cash basis, as of the day before the Slashbooks cutover/start date
- Balance Sheet, cash basis, as of the day before the Slashbooks cutover/start date
- Balance Sheet, cash basis, as of the backtest end date
- Profit and Loss, cash basis, for the backtest period
- General Ledger, cash basis, for the backtest period
- Transaction Detail by Account, cash basis, for the backtest period

Slashbooks writes new imported entries into the canonical `ledger.sqlite` store
and then regenerates `books.beancount` as a deterministic compatibility
snapshot. The snapshot is still useful for inspection and tools that expect a
plain ledger file, but indexed reports and reconciliation can read the store
directly once it exists.

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

Because Slashbooks is building cash-basis books, QuickBooks reference reports
should use cash basis where QBO offers it. If QBO cannot provide a cash-basis
Trial Balance, inventory can still check whether the prior-period cash-basis
Balance Sheet fallback is available.

Basic QuickBooks Online export steps:

1. Go to **Reports**, then **Standard reports**.
2. Search for and open the report.
3. Set the report date or period.
4. If **Accounting method** is available, choose **Cash**, then select
   **Run report**.
5. Select **Export/Print** then **Export to Excel**.
6. Move the downloaded file into `ingestion/quickbooks/`.

For Chart of Accounts, go to **Accounting -> Chart of Accounts**, select
**Run report**, then use the export icon to export to Excel.

For opening balances:

```sh
books qb import-opening \
  ~/Documents/books/acme-co/ingestion/quickbooks \
  --entity ~/Documents/books/acme-co \
  --cutover 2026-01-01
```

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
