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

Each company keeps its books in one currency (`currency` in `entity.json`, USD
when absent). A file in another currency, or a file with amounts in more than one
currency, stops with the error code `unsupported_currency`. The message names
both currencies. A company with no entries yet takes the currency of its first
file that names one. Slashbooks does not convert currencies. See
[company currency](company-currency.md).

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

### Tie-out

Each statement in the result has a `tieout` that checks the file alone: the
opening balance plus the rows equals the closing balance. All sums are in
Python, in the statement presentation (money owed is positive for a card).

```json
{"status": "matches", "opening": "1000.00", "rows_total": "2142.26",
 "expected_closing": "3142.26", "closing": "3142.26", "difference": "0.00", "row_count": 6}
```

`status` is `matches`, `differs` or `no_balances` (a balance is missing, so
nothing can be proved; most CSV files). `counts.to_import` is the number of
rows that the import would add. By default a `differs` result is a warning and
the rows import. With `--require-tieout` a `differs` result stops before any
write: the status is `needs_review`, the error code is `tieout_differs`, and
the exit code is 4. A dry run reports the same.

`books coverage check --format json` adds a `tieout` to each statement and to
each month. It compares the books with the statement: the statement opening
balance plus the books movement in the period, against the statement closing
balance. `status` is `matches`, `differs`, `no_balances`, or `not_aligned`. A
month gets a status only when one statement is exactly that month. A statement
that spans months is checked on its own period and its months are
`not_aligned`. `waiting_rows` counts staged rows in the period that are not in
the books yet, so a difference can be read with the review backlog. A
`differs` result adds the readiness warning `statement_tieout_differs`. It is a
warning, not a blocker.

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

### Import check

`books import check` answers one question about a statement import: does what
the books hold agree with the file. It is read-only, deterministic and uses
Decimal sums in Python. It writes nothing.

```sh
books import check st-69ff55d30830086e --entity ~/Documents/books/acme-co
books import check st-69ff55d30830086e --entity ~/Documents/books/acme-co --stored
books import check st-69ff55d30830086e --entity ~/Documents/books/acme-co --format text
```

The import ID is the `statement_id` from `books statement list`. The command
reads the archived copy of the file again and compares it with the books as
they are now. A recompute uses the profile as it is saved now, not as it was at import time;
if the profile changed, the rows can differ from the stored result. Use `--stored` to see the result of the
original import. A file that is not a `books statement import` record, and a
statement from `import-extracted`, cannot be checked (`unsupported_import`).
`--stored` prints the result saved when the file was imported and does not
recompute it. The exit code is 0 for every result and 1 for an error.

Each `books statement import` runs the same check after the write and stores the
result in `ingestion/statements/checks/<import_id>.json`. The result is also the
`import_check` of each statement in the import result. A dry run has none. The
hosted product can show one status line from the stored file.

```json
{
  "schema_version": 1,
  "import_id": "st-69ff55d30830086e",
  "kind": "statement",
  "status": "matches",
  "account": "Assets:Bank:Checking",
  "period": {"from": "2026-01-01", "to": "2026-01-31"},
  "checks": [
    {"id": "count", "status": "matches", "source": "6", "books": "6", "difference": "0",
     "detail_code": "all_rows_accounted"},
    {"id": "debits", "status": "matches", "source": "358.99", "books": "358.99",
     "difference": "0.00", "detail_code": "totals_agree"},
    {"id": "credits", "status": "matches", "source": "2501.25", "books": "2501.25",
     "difference": "0.00", "detail_code": "totals_agree"},
    {"id": "closing", "status": "matches", "source": "3142.26", "books": "3142.26",
     "difference": "0.00", "detail_code": "queued_rows_included"},
    {"id": "opening", "status": "not_checkable", "source": null, "books": null,
     "difference": null, "detail_code": "first_statement"}
  ],
  "counts": {"in_source": 6, "recorded": 0, "queued": 6, "duplicate": 0, "pending_skipped": 0},
  "waiting_rows": 6,
  "computed_at": "2026-02-01T00:00:00Z",
  "books_digest": "sha256:23d7ff0628a3570b"
}
```

`kind` is `statement`. Other kinds (Stripe, bank feed, QuickBooks, migration)
can use the same shape later. `status` is `matches` (every check matches),
`differs` (one check differs), `partial` (some checks match and some are
`not_checkable`) or `not_checkable` (no check could run). `computed_at` is the
time of the run and `books_digest` is a short digest of the ledger postings and review
files that the run read, so a later run shows whether the books changed. It is
not the hosted service's integer `books_revision`.

| Check | What it compares |
| --- | --- |
| `count` | Rows in the file against rows recorded + queued + duplicate + pending skipped. A row in none of these is unaccounted: `differs`, `rows_unaccounted`. |
| `debits`, `credits` | Money out (charges, withdrawals) and money in (payments, deposits) in the file against the books. Posted rows use their ledger postings and queued rows use their queued amount. Both totals are positive, for an asset and for a card. |
| `closing` | The opening balance plus the books movement in the period against the closing balance (the same sums as `coverage check`). Queued rows count in the books. The rows of the file are also added to the opening balance, and a mismatch there is `differs`, `file_rows_differ`. |
| `opening` | The opening balance against the closing balance of a recorded statement for the same account that ends the day before this one begins (`prior_statement_closing`). |

`counts` uses these rules:

- `recorded` rows are in the ledger. `queued` rows wait in the review queue.
- `duplicate` rows are held duplicate candidates, rows a person confirmed as
  duplicates, rows that the profile boundary leaves to another source, and
  repeated rows inside the file. Their money is held by another entry, so the
  money checks count them at the file amount (`duplicates_at_file_amount`).
- `pending_skipped` rows are pending rows in the file. The importer skips them
  and their amounts are not in any total.
- `waiting_rows` is the number of rows that wait for a person: queued rows and
  held duplicates that no one has resolved.

A check is `not_checkable` with a `detail_code`, and never a guess:

- `foreign_currency`: the statement is not in the company currency. The money
  checks stop; `count` still runs.
- `no_balances`: the file has no opening or closing balance. `count`, `debits`
  and `credits` still run. Most CSV files are like this, so their status is
  `partial`.
- `period_unconfirmed`: nobody confirmed the statement period, so the books
  movement cannot be selected. Pass `--period-start` and `--period-end`.
- `not_aligned`: another recorded statement for the same account overlaps the
  period (for example a partial-month statement beside a full month), so the
  books movement in the period cannot be tied to this file alone. This is the
  same rule that `coverage check` uses for a month.
- `no_prior_statement`: an earlier statement exists but none ends the day
  before this one begins. `first_statement`: there is no earlier statement,
  so the opening has nothing to compare with. It does not lower `status`.
- `no_prior_balance`: the prior statement has no closing balance.
- `books_unreadable`: the ledger or a review file cannot be read, so every check
  is `not_checkable` instead of reporting a false difference.

Balances are in the statement presentation (money owed is positive for a card),
so a card with `opening` 1000.00 and a 130.01 difference reads the same as in
the tie-out above. A `differs` result is a signal for a person, not a block:
the import has already happened. Use `--require-tieout` to stop an import
before any write.

### Bank balance against the books

`books balance check` compares a balance that a bank feed reported with the
books balance for the mapped ledger account on the same day. It is read-only.
Balance, coverage and review-readiness checks hold a shared lock on the company
through all ledger, staging and evidence reads. Two checks can run together and a
check needs no write access to the company folder. Local imports, confirmations
and configuration updates wait until the snapshot is complete. The first
check may create the empty operational file `.books.lock`; it changes no
accounting records. Hosted workspace manifests exclude this lock file. If the
ledger file is damaged, `books balance check` stops with the error code
`ledger_unreadable`.
The host supplies the bank facts:

```sh
books balance check --entity ~/Documents/books/acme-co --input balances.json
```

The input is a JSON list of `{"account_id", "balance", "as_of", "data_through"}`.
`retrieved_at` is a fallback date when `as_of` is null. `balance` uses the
statement presentation (money held; money owed, positive, for a card). Set
`"sign": "ledger"` for a card balance that is negative when money is owed.
`account_id` is looked up in `bank_account_mappings`. Feeds that map to one
ledger account are added together into one row only when each provider account
appears once and every balance has the same effective date. Each row has `status`,
`account_ids`, `ledger_account`, `bank_balance`, `books_balance`, `difference`,
`waiting_rows`, `waiting_total`, `compared_through` and `compared_basis`
(`as_of`, `retrieved_at`, `data_through` or `latest`). `status` is `matches`,
`waiting_in_review` (the difference equals the staged rows), `differs`,
`bank_newer` (the bank date is after `data_through`), `unmapped` or
`no_bank_balance`. A repeated provider account or mixed/missing dates in a
combined balance returns `not_aligned`, with `reason` of `duplicate_account_id`
or `balance_dates_not_aligned`; no combined balance or difference is claimed.

### Reconcile a saved statement

After importing a statement with its confirmed period and both source balances,
use the saved statement ID from `books statement list --entity PATH`:

```sh
books reconcile --entity ~/Documents/books/acme-co --statement-id STATEMENT_ID
```

The engine verifies the archived original file against its saved SHA-256, reads
its confirmed dates, balances and source-row total, and retains the original
intake time when available. Re-reading unchanged evidence does not reopen a
person's resolved discrepancy. It converts bank/card presentation signs and compares those figures with
the ledger. No balance, activity total, timestamp or evidence hash needs to be
transcribed. This observes the saved statement; it does not refresh a bank feed.

Missing or changed originals, incomplete source figures, conflicting current
statements and replaced statements are refused. Choose the current statement or
recheck its intake before retrying. A ledger difference remains a discrepancy;
reconciliation does not post missing transactions or close the period. Check each
month's readiness before claiming the requested period is ready for review.
Use the captured-source options for provider readings or other supported evidence;
they cannot be combined with `--statement-id`.

An embedding host can read `books statement reconciliation-preview --entity PATH
--id STATEMENT_ID` before unattended reconciliation. This stricter preview
reparses the archived CSV or OFX without manual overrides and checks its original
period and balances against the saved record. A CSV without those source figures,
or a manually transcribed or extracted statement, needs owner-attested facts
through the host's protected confirmation path. The preview does not promote
manually supplied figures into original-source proof.

### Month-end proof from a bank feed

`books balance prove` says, for each finished month of a feed-connected checking
or savings account, whether the feed balance proves the month-end balance. It is
read-only. For a proven month it prints the exact figures for `books reconcile`.

```sh
books balance prove --entity ~/Documents/books/acme-co --input readings.json --as-of 2026-10-20
```

The input is `{"accounts": [{"account_id", "account_type", "time_zone", "currency",
"readings": [{"balance", "as_of", "retrieved_at", "data_through", "partial"}]}]}`.
`time_zone` (the IANA name of the bank's time zone) is required; `currency` is
optional. `balance` is the current balance that the bank reported when it was
read, not a statement balance. A reading is used only when `partial` is `false`,
`as_of` is not later than `retrieved_at`, it was read at least 6 real hours after
the bank's midnight that starts the day after the month end, and `data_through`
reaches the balance day.

A month is proven only when feed windows cover every day from the month start
through the balance day, nothing waits in review (and the review file can be
read), the books equal the bank on that day, and the opening balance is proven.
The opening comes from the reconciliation or statement of the month before. In
the first month of the feed windows it is zero only when nothing in the books is
dated earlier and every posting of the month came from a source; an
opening-balance entry dated inside that month needs a statement. Cards, loans and
other types are never proven. Each row has `status` (`proven` or `not_proven`)
and `reason`, one of `proven`, `already_reconciled`, `existing_record`,
`unmapped`, `shared_ledger_account`, `not_checking_or_savings`,
`time_zone_required`, `unknown_time_zone`, `currency_mismatch`,
`other_currency`, `unreadable_postings`, `waiting_unreadable`,
`no_balance_after_month_end`, `feed_incomplete`, `waiting_in_review`,
`books_differ`, `opening_unproven` or `opening_differs`.

Limits that remain:

- A transaction that the books date on the other side of the month end than the
  bank does is not found when the two agree again on the reading day and no
  reading falls on the month end. A statement finds it.
- Errors that cancel each other inside one month are not found. Across a month
  end, the opening chain finds them.
- The proof shows balances, not categories: a wrong category is not found.
- Only rows waiting in review count as waiting. Rows in `staging/pending.json` and
  held duplicate candidates are not checked; a transaction that is really missing
  still shows as a difference between the books and the bank.
- The balance used is the current balance. When the bank counts pending items in
  it, the month waits.
- The readings, dates, time zone and feed windows are facts from the host. The
  engine checks them against each other and against the books, not against the bank.

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

OFX, QFX and QBO readers validate every transaction amount and statement balance
inside each bank and card statement (`STMTRS` and `CCSTMTRS`) as finite exact
cents before importing any row or retaining the file. An empty balance amount is
skipped. Investment sections are not read; an investment file still returns
`unsupported_statement`. Import
checks include reversal and correction lineage when comparing file totals with
the books. A first statement adds period movements to its supplied opening
balance once; an imported opening journal on that first date supplies state,
not an additional transaction. Cumulative bank balance checks still include it.
