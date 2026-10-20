# Tax handoff: source coverage and tax-preparer export

> **Draft for review.** Every tax map and tax export is prepared from the data
> in the books only. It is not tax advice and does not replace preparation or
> review by a qualified tax professional. Agents tell the person this each time
> they show a tax mapping or hand over an export.

A tax preparer needs two things from the books before preparing a return:

1. Proof that each bank and card account has source data for every day of the
   year.
2. A trial balance with each account mapped to a line on the return.

Slashbooks gives both with `books coverage` and `books tax`. Both commands
read the books and never change ledger entries. Neither command computes tax.

## Source coverage

```sh
books coverage check --entity . --from 2026-01-01 --to 2026-12-31
books coverage check --entity . --from 2026-01-01 --to 2026-12-31 --format json
```

The check reports each expected source account and each calendar month as one
of these:

| Status | Meaning |
|---|---|
| `complete` | Every day of the month has source evidence. |
| `partial` | Some days have no evidence. The report lists the gap dates. |
| `missing` | No day of the month has evidence. |

Each month also has `verified`. It is `true` only when statements, feed
windows or owner-declared coverage cover every day. Coverage that comes only
from transaction dates is marked as inferred.

Example reasons:

- `Operating Checking has no transactions for Mar 3–31.`
- `Operating Checking statement ending Jan 31, 2026 closes at 1,500.00, but the next statement opens at 1,450.00 (difference -50.00).`

### Expected accounts

By default, the check uses the accounts in `declared_sources`,
`source_coverage`, `bank_account_mappings`, `csv_account_mappings` and statement
records. Pass `--account` one or more times to set the list yourself.

### Evidence, strongest first

1. **Statement records** with a start date, an end date and, when known, the
   opening and closing balances.
2. **Feed windows** (`kind: "feed_sync"`): a date window that a bank feed or
   provider download returned in full.
3. **Declared coverage** recorded with `books entity source-coverage`.
4. **Transaction dates** of ledger entries that have a source ID. Dates that
   are no more than `--max-quiet-days` apart (default 31) form one covered run
   from the first date to the last date. Days before the first transaction and
   after the last transaction are gaps.

### Continuity between statements

For consecutive statements of the same account, the check reports:

| Code | Severity | Condition |
|---|---|---|
| `balance_discontinuity` | error | A closing balance does not equal the next opening balance. |
| `statement_gap` | warning | Days between two statements have no statement. |
| `statement_overlap` | warning | Two statements cover some of the same days. Check for a duplicate import. |
| `statement_duplicate` | warning | Two statements have the same dates and balances. |
| `statement_conflict` | error | Two statements have the same dates and different balances. |
| `statement_record_invalid` | error | A record breaks the contract below. It adds no coverage. |

### Statement record contract (schema version 1)

This contract is the one format for statement and feed-window evidence.
Importers, connectors and hosts record it as JSON files directly in
`<company>/ingestion/statements/`. Each file holds one object or a list of
objects. The check does not read subdirectories: `files/` holds archived
source files and `unconfirmed/` holds imports whose period nobody confirmed.

```json
{
  "schema_version": 1,
  "statement_id": "operating-checking-2026-03",
  "account": "Assets:Bank:Operating-Checking",
  "kind": "statement",
  "period_start": "2026-03-01",
  "period_end": "2026-03-31",
  "opening_balance": "1000.00",
  "closing_balance": "1250.00",
  "source": "Operating Checking",
  "evidence_ref": "ingestion/files/operating-checking-2026-03.pdf",
  "evidence_sha256": "<64 lowercase hex characters>"
}
```

| Field | Rule |
|---|---|
| `statement_id` | Required. 1-128 letters, digits, dots, hyphens or underscores. Unique. |
| `account` | Required. An `Assets:` or `Liabilities:` ledger account. |
| `kind` | `statement` (default) or `feed_sync`. |
| `period_start`, `period_end` | Required. Inclusive `YYYY-MM-DD` dates. |
| `opening_balance`, `closing_balance` | Optional decimal strings with at most two decimal places. Use the statement's own presentation: money held for a bank account, money owed for a card or loan. Feed windows have no balances. |
| `source` | Optional label for reports. |
| `evidence_ref`, `evidence_sha256` | Optional pointer to the source file and its SHA-256 digest. |

Readers ignore unknown fields, so a writer can add its own details next to
the contract fields. A file importer that knows a statement's dates and
balances writes a `statement` record. A feed or API download that returned a
complete window writes a `feed_sync` record for that window.

### Who writes records

| Writer | Kind | When |
|---|---|---|
| `books statement import` | `statement` | For each imported statement whose period is known: the OFX `DTSTART`/`DTEND`, a printed CSV summary, or `--period-start`/`--period-end` from the person. See [imports.md](imports.md#statement-period-and-balances) for the balance rules. |
| `books ingest` | `feed_sync` | After a successful local import of a BankSync download (not for an account that BankSync marked partial), or of any input with `coverage_windows`. `--no-feed-window` turns this off. See [connectors.md](connectors.md#coverage-windows). |
| `books connector csv parse --period-start --period-end` | `feed_sync` (through `books ingest`) | For the date range a person chose for an Amex activity download. |
| `books coverage statement-add` | either | For a record that a person reads from a statement or a host supplies. |
| A hosted service | `feed_sync` | For each bank-feed sync window it completed. A host that reads in chunks passes `--no-feed-window` to `books ingest`, so the host is the only writer. |

A QuickBooks history import is book data, not bank source evidence, so it
writes no coverage records.

A host that writes records directly uses the same file rules: one file per
record, named `<statement_id>.json`, written atomically. For a feed window, use
`kind: "feed_sync"`, no balances, and a window that starts no earlier than the
first day the feed returned and ends before the first pending item. A stable
ID, such as the one `bookkeeping.coverage.feed_window_id()` makes, keeps a
repeated sync from adding a second file.

A host writes a window only when it read the account to the end and the
provider did not mark the read partial. This is a complete record. The engine
reads it, and it ignores the `provider` and `account_id` fields:

```json
{"schema_version": 1, "statement_id": "feed-0123456789-20260103-20260131",
 "account": "Assets:Bank:Checking", "kind": "feed_sync",
 "period_start": "2026-01-03", "period_end": "2026-01-31", "source": "Chase",
 "provider": "banksync", "account_id": "feed-1"}
```

### Readiness

`books readiness` runs the same check for its period and declared source
accounts. It adds these items without changing other readiness behavior:

- Blocker `source_month_partial` or `source_month_missing` for each gap, with
  the plain-language reason as the message.
- Blocker `source_balance_discontinuity`, `statement_conflict` or
  `statement_record_invalid` for each error finding.
- Warning `source_month_inferred`, `statement_overlap`, `statement_gap` or
  `statement_duplicate`.
- If statements or feed windows cover every day of the period for an account,
  that account does not also get the `source_coverage_incomplete` blocker for
  missing declared coverage dates.
- A `coverage` list with the month rows for each account, for display.

## Tax-line mapping

```sh
books tax lines --form 1120s
books tax map --entity . --year 2026
books tax set --entity . --return-form 1120s
books tax set --entity . --account Expenses:Coworking --line 1120s/p1/11
books tax set --entity . --account Expenses:Coworking --clear-line
```

Supported returns, with tax year 2025 federal line numbers:

| Form code | Return |
|---|---|
| `schc` | Schedule C (Form 1040): sole proprietor or single-member LLC |
| `1065` | Form 1065: partnership or multi-member LLC |
| `1120s` | Form 1120-S: S corporation |
| `1120` | Form 1120: C corporation |

The covered lines are the income and deduction lines on page 1 (Part I and
Part II for Schedule C), the Schedule K lines for interest, dividends and
charitable contributions, Schedule L, and the Schedule M-2 distributions line.

Not supported: Schedules E and F, Forms 1041 and 990, state returns, K-1
allocation, Form 1125-A and Form 4562 calculations, Schedule M-1, M-2 and M-3
calculations, and accrual conversion.

### How a line is chosen

1. **Override.** A line set with `books tax set` for that form. Overrides are
   stored in `entity.json` under `tax_handoff.line_overrides.<form>`.
2. **Name rule.** Words in the account name, such as `Advertising`, `Rent` or
   `Payroll-Taxes`.
3. **Account type.** Only for income accounts, which default to gross receipts.

Name rules and type defaults are marked `review`. An account that matches no
rule is unmapped. An account type that has no line on the chosen form, such as
officer compensation on Schedule C, is also unmapped. Unmapped accounts stay in
the export and appear in the unmapped-accounts report.

The return form comes from `--form`, then `tax_handoff.return_form`, then the
company's `legal_structure`. If none of these is clear, the command asks for
`--form` or `books tax set --return-form`.

## Tax-preparer export

```sh
books tax export --entity . --year 2026
books tax export --entity . --from 2026-01-01 --to 2026-12-31 --form 1065 --require-mapped
```

The export writes these files to `reports/tax-handoff/<form>_<period>/`:

| File | Use |
|---|---|
| `tax-trial-balance-*.csv` | Generic tax trial balance: account number, account, type, beginning balance, debit, credit, balance, form, schedule, line, line code, statement detail, mapping basis and review flag. |
| `tax-line-summary-*.csv` | Book amount per form line, for manual entry. |
| `unmapped-accounts-*.csv` | Every nonzero account without a line, with the command that fixes it. |
| `lacerte-tbu-*.csv` | Layout for the Lacerte Trial Balance Utility. |
| `ultratax-tb-import-*.csv` | Layout for the UltraTax CS Trial Balance Excel Import. |
| `tax-handoff-*.xlsx` | All of the above as sheets, plus a Read Me sheet. Needs the `xlsx` extra. |
| `README.txt` | The draft disclaimer, the period, the totals and the file list. |
| `manifest.json` | The `disclaimer`, totals, unmapped and review lists, and the SHA-256 of each file. |

The trial balance, tax-line summary and unmapped-accounts files start with a
one-cell row that holds the draft disclaimer. The header row comes next. The
Lacerte and UltraTax files start with their header row, because those products
import them; the README and the manifest in the same folder carry the
disclaimer. In the workbook, the first row of the Read Me sheet is the
disclaimer. `books tax map` and `books tax export` print it first.

Account names, notes and other text come from your source files. A text cell
that starts with `=`, `+`, `-`, `@`, a tab or a carriage return would run as a
formula in a spreadsheet program, so the CSV files write it with a leading
apostrophe and the workbook stores it as plain text. Amounts stay numbers.

Balances are cash-basis book balances from the engine. Balance columns are
debit-positive. Income and expense accounts show activity in the period.
Balance sheet accounts show the start and end balances. Because the books are
not closed, income and expenses before the period appear as one computed row,
`Equity:Prior-Periods-Net-Income`, so the trial balance stays in balance.
Distributions lines show the change in the account during the period.

### Tax software

| Product | What the export gives | Source |
|---|---|---|
| Lacerte | `lacerte-tbu-*.csv`: account number, account name, account type (`A`, `L`, `R`, `E`; equity uses `L`) and a signed balance. Import it with Tools > Import/Export Data > Lacerte Trial Balance Utility > File > Import > From Excel. Mark the `Slashbooks Tax Line` column "Do Not Import" or keep it for reference. SmartMap assigns Lacerte fields in the product. | [Using the Lacerte Trial Balance Utility to import Excel data](https://accountants.intuit.com/support/en-us/help-article/partnership/using-lacerte-trial-balance-utility-import-excel/L19njx4ka_US_en_US), [Common questions about the Lacerte Trial Balance Utility](https://accountants.intuit.com/support/en-us/help-article/federal-taxes/common-questions-lacerte-trial-balance-utility/L5uS9jw7Q_US_en_US) |
| UltraTax CS | `ultratax-tb-import-*.csv` with the five mappable fields in order: account number, description, unit, tax code, amount. Use Utilities > Trial Balance Excel Import. UltraTax tax codes are not published in a public list, so the firm records each code one time with `books tax set --ultratax-code`. Later exports reuse it. | [Import general ledger balances with tax codes](https://www.thomsonreuters.com/en-us/help/ultratax-cs/integration/integrate-with-cs-suite/import-general-ledger-balances) |
| Drake Tax | Drake imports trial balances only through its own macro-enabled Excel template, for 1065, 1120, 1120-S and 990 returns. Use the tax-line summary to fill the template rows or to key the amounts. | [Drake Tax - Trial Balance Import (KB 11166)](https://kb.drakesoftware.com/kb/Drake-Tax/11166.htm) |
| ProConnect Tax | ProConnect has no documented trial balance file import. Its spreadsheet import covers depreciation, dispositions and owner data only. Use the tax-line summary for manual entry. | [How to import tax data from a spreadsheet in ProConnect Tax](https://accountants.intuit.com/support/en-us/help-article/import-export-data-files/import-tax-data-spreadsheet-proconnect/L1e3s4KNr_US_en_US) |

The vendors do not publish the sign convention for these imports. Test one
import with a small file, and compare the totals with the tax-line summary
before you rely on it.

Account numbers default to stable numbers by type and name (1000s assets,
2000s liabilities, 3000s equity, 4000s income, 6000s expenses). A new account
can move later numbers. To keep a number across years, record it with
`books tax set --account-number`.

## What the export is not

The export maps and exports engine-computed book balances. It does not apply
the meals limit, compute depreciation, compute cost of goods sold, make
book-to-tax adjustments, or decide whether an amount is deductible. The
preparer makes those decisions.

### Accounting basis and historical coverage

The handoff uses the recorded book postings. When accrual rules apply to every transaction, unpaid invoices and bills
remain accrual amounts even if the default profit and loss view is cash. The Read Me sheet,
README.txt and manifest identify the accounting basis. The Read Me shows a short coverage
line (basis, cutover date, summary period and whether historical detail is available).
The full `accounting` block, with the account chart and hashes, stays only in
`manifest.json`, next to `accounting_basis`. A company that tracks accrual for selected
accounts only is labeled "Cash basis, with accrual tracking for selected accounts from <date>. Book balances as recorded, not a tax cash-basis view; the preparer adjusts." once it has accrual records. Turning tracking on alone changes no export.

A historical opening summary supports its complete fiscal period, not arbitrary earlier months.
Unsupported pre-cutover subperiods or dates before the source balances are refused before any
handoff files are written. A supported full-summary export identifies the included summary and
states that historical transaction detail is unavailable. Periods entirely before optional accrual
tracking began remain cash periods, and a period that crosses the tracking start is not refused.
