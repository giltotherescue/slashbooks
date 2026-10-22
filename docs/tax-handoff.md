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

A person can also record facts for the preparer: the tax method
([Tax method view](#tax-method-view)), the [tax profile](#tax-profile), the kind of
each [deposit](#deposits-advance-payment-or-refundable) and the
[fiscal year](#set-the-fiscal-year). When a tax method is recorded, the export adds
a Tax Assumptions sheet, a Fixed Assets sheet, and a Deposits sheet when the view is the cash view of accrual
records. Slashbooks stores these facts and lists missing inputs; it does not decide a
tax rule. An agent can run each preview, and a person applies it.

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

Balances are book balances from the engine, as recorded (see "Accounting basis
and historical coverage" below), or the tax method view when a tax method is recorded. Balance columns are
debit-positive. Income and expense accounts show activity in the period.
Balance sheet accounts show the start and end balances. Because the books are
not closed, income and expenses before the period appear as one computed row,
`Equity:Prior-Periods-Net-Income`, so the trial balance stays in balance.
When the period starts on the fiscal year start, this row is the same figure that the balance sheet and
`books report trial-balance` show as "Retained earnings from prior fiscal activity". The account name stays,
because saved tax lines and account numbers use it.
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

### Tax method view

The tax outputs follow the method the company files on, instead of the recorded books, as soon as
a person records that method. One key in `entity.json` controls it. A person sets it with one command:

```sh
books entity set-tax-method PATH --method cash          # preview
books entity set-tax-method PATH --method cash --apply  # change it
```

- `tax_method` is `cash` or `accrual`. Absent means unknown. With no `tax_method`, every tax output
  is the same as before the view existed: it reads the recorded books.
- The tax method view is always on once a tax method is set. An old `tax_method_view` key in
  `entity.json` is ignored on read, is never written, and is dropped when the method changes.
  The option `--tax-view` is removed. A script that still passes it gets a refusal
  (`tax_view_removed`) and nothing changes, so an old `--tax-view off` cannot be ignored without notice.
- The command previews unless `--apply` is given. `--plan-hash` refuses a changed company.
  An exact repeat returns `already_applied`. The ledger audit log gets a `tax-method-set` event.
  No entry, report, period lock or published statement changes.
- It is a person-only write. An agent can run the preview. Standing posting authority never
  covers `--apply`.

When a tax method is set, `books tax map`, `books tax export`, the Read Me, `README.txt`,
`manifest.json` and the tax workbook use this view:

| Tax method | Books | View used |
|---|---|---|
| cash | cash books (accrual off) | the recorded books, which are cash books |
| cash | accrual records (capability 1) | the cash view derived from the accrual records |
| accrual | accrual records | the recorded books; refused before the accrual tracking start |
| accrual | cash books (accrual off) | refused: the books have no accrual view |

The handoff, `tax map --format json` and `manifest.json` carry `tax_method`, `view` and
`tax_view` (`source`, `label`, `assumptions` and, for the handoff, `flags`). The "Basis" line of the Read Me
names the method and the view. The cover of the accountant package gets a "Tax method" row.

The cash view for tax:

- Unpaid invoices are not income and unpaid bills are not expenses. Each is income or expense
  on the date cash settles it, also when that date is in the next tax year.
- Receivable, payable, accrued, deferred revenue and customer deposit balances leave the trial
  balance. One computed row, `Equity:Accrual-To-Cash-Adjustment`, keeps the trial balance balanced.
- Deferred revenue and customer deposits are income on receipt.
- A prepaid schedule follows its recorded `tax_treatment` (see `docs/accrual.md`). `expense_when_paid` is
  expense on the payment date. `capitalize` keeps the unused part on the balance sheet and
  deducts the recognition entries as they post. With no recorded value, the view follows the
  books: the same as `capitalize`.
- Collected opening receivables and paid opening payables are income and expense on the cash date,
  on the rows `Income:Opening-Receivables-Collected` and `Expenses:Opening-Payables-Paid`.

The view never falls back to the other view. The export refuses, writes no file and names the
reason when the view cannot be computed: a range that starts before the accrual cutover or inside
an imported summary period, an accrual range before the tracking start, accrual books that cannot
produce a cash view, or a cash view that does not tie to the accrual books.

`tax_view.flags` lists what the preparer must check. Each flag has a `code` and a `message`:

- `depreciation_not_tax_adjusted`: fixed assets stay on the balance sheet and depreciation is book
  depreciation as recorded.
- `difference_from_accrual_view`: cash-method net income minus accrual net income, with one row per group.
- `deferred_revenue_on_receipt`, `bad_debt_not_deductible`, `accrued_liabilities_excluded`,
  `receivables_payables_excluded`, `opening_items_on_receipt`.
- `prepaid_capitalized`, one per prepaid schedule kept on the balance sheet, with `treatment_source`
  `recorded` (a person recorded `capitalize`) or `not_recorded` (no value is recorded).
- `cash_view_unclassified` and `cash_view_noncash_entry`: entries the cash view cannot explain.
- `method_change_possible`: the tax method is cash and `prior_return_method` is `accrual` or
  `unknown`. It is a flag only. The export is not refused. See "Method change" below.
- `prepaid_tax_treatment_not_recorded`: the prepaid schedules with no recorded `tax_treatment`.
- `deposit_kind_not_recorded`: the deposits with no recorded `deposit_kind` (the view assumes
  advance payment). `deposit_refundable`: one per deposit recorded as refundable.

These flags only list missing inputs and recorded facts. Slashbooks applies no tax rule to the
tax profile or to the asset list. It does not block or decide from them.

### Method change

Accrual books, or an unknown prior return method, do not show the method on the tax return.
Slashbooks cannot know the method on the last return. The preparer confirms it. A change from accrual
to cash needs IRS consent (Form 3115) and a section 481(a) adjustment. The preparer handles both.
Slashbooks does not prepare them, does not name the guidance that applies this year, and does not
refuse the export. It adds the flag `method_change_possible`.

### Tax assumptions

The tax export states what it did and what it left alone, so the preparer can review it. The
section is data from the books and the settings. It applies no tax rule and gives no tax advice.
It is in `tax_view.assumptions` (tax export, `manifest.json` and `tax map --format json` with a
period), in the "Tax Assumptions" sheet right after the Read Me, in `tax-assumptions-*.csv`, and in a
short Read Me section. It has four parts:

1. Settings in effect: tax method and its source, fiscal year start, prior return method, entity type,
   currency, period covered, the basis of the recorded books and the view used for tax.
2. How each item is handled. One row each: prepaid expenses, customer deposits, deferred revenue,
   opening receivables and payables, credit card charges, bad debts, depreciation and expensing,
   accrued liabilities, inventory and tax year. Each row has the handling in this export, the source of
   the handling (recorded by a person, assumed default or not adjusted) and whether the preparer decides.
3. Counts that show how complete the inputs are: prepaid schedules with no recorded tax treatment,
   deposits with no recorded kind (assumed advance payment), fixed-asset rows listed, and open flags by code.
4. The list of what the preparer decides: the tax method and the tax year on the filed return,
   cash-method eligibility and tax-shelter status, the 12-month rule and the election out, Form 3115 and
   the section 481(a) adjustment, depreciation, section 179, bonus and de minimis, deposit and
   trust or escrow classification, the inventory method, and card financing.

Points the sheet states:

- Passing the 12-month test only removes the capitalization requirement. It does not prove that an
  item is deductible.
- The election out of the 12-month rule is made on the timely filed original return. A schedule
  override in Slashbooks is not that election.
- A deposit recorded as refundable is a proposal that a person recorded. Check the agreement. Do not
  classify a deposit, or funds held in trust or escrow, from an account name.
- When the export reads the recorded books as they are (the accrual method, or cash books), the
  rows say "Not adjusted".
- `placed_in_service` in `tax_view.fixed_assets` holds the purchase date and stays for old readers.
  Use `purchase_date`.

### Tax profile

A person records two facts for the preparer. The tax export lists them in `tax_view.tax_profile`
and in the Read Me:

```sh
books entity set-tax-profile PATH --prior-return-method cash --entity-type single_member_llc          # preview
books entity set-tax-profile PATH --prior-return-method cash --entity-type single_member_llc --apply  # record it
```

- `prior_return_method`: the method of the last tax return filed: `cash`, `accrual`, `unknown`,
  or `none` for a first-year business.
- `entity_type`: `sole_proprietor`, `single_member_llc`, `partnership`, `s_corp`, `c_corp`,
  `nonprofit`, `other` or `unknown`.
- Give one field or both. A field that is left out keeps its value.
- Preview unless `--apply`; `--plan-hash` refuses a changed company; an exact repeat returns
  `already_applied`; the audit log gets a `tax-profile-set` event. It is a person-only write.

### Deposits: advance payment or refundable

A deferred schedule or a customer deposit item (a credit item on a liability account) can carry
`deposit_kind`. The item must come from a customer payment (`settle-apply` with `excess`), a posted
payment (origin `existing`) or an opening balance; a credit memo is refused. Only a person records it:

```sh
books accrual item deposit-kind --entity PATH --id DEPOSIT --deposit-kind refundable              # preview
books accrual schedule deposit-kind --entity PATH --id SCHEDULE --deposit-kind refundable --apply  # record it
```

The cash view for tax treats them so:

- `advance_payment` (also when nothing is recorded): income on the receipt date, as before.
- `refundable`: no income on receipt and no expense on refund. It stays a liability in the tax
  trial balance. The view places income when it is applied to an invoice (on the application
  date, also for an opening-balance deposit) or forfeited (an adjustment of the deposit into
  income). A refundable deferred schedule is income as its recognition entries post.
- If a deposit recorded as refundable has no cash receipt and no opening balance behind it, the
  view refuses with `refundable_deposit_not_cash_backed`.

The view keeps its tie-out to the accrual books to the cent. `tax_view.deposits` and the
"Deposits" sheet list every deposit with its kind. A deposit with no recorded kind shows
"assumed advance payment". The books, the reports and the report cash view never change.

### Fixed assets worksheet

`tax_view.fixed_assets` and the "Fixed Assets" sheet list, as data only:

- each depreciation schedule: purchase date (the origin date of the schedule), cost, account, vendor (the payee
  of the purchase entry, when known), reference and book depreciation to the end of the period;
- each entry with no depreciation schedule whose postings to `Assets:Fixed` accounts are a net
  debit, on the account it debits most. A move between two fixed-asset accounts nets to zero and
  is left out. Accounts whose name contains "Accumulated" and depreciation contra accounts are
  left out. An opening-balance entry has `source` `opening`.

`in_period` says whether the purchase date is in the export period. The purchase date is not the
placed-in-service date. Slashbooks does not record the placed-in-service date, and the two can differ.
The sheet header says "Purchase Date" and the sheet carries a note. The preparer sets the
placed-in-service date.

Slashbooks computes no tax depreciation. It does not choose section 179, bonus depreciation or the de
minimis safe harbor. The preparer does. Amounts are ceilings, and each has conditions that the preparer
checks. For reference only (verify before use): the de minimis safe harbor is $2,500 per invoice or
item without an applicable financial statement, and $5,000 with one (Treas. Reg. 1.263(a)-1(f)). It needs
written accounting procedures at the start of the year, book expensing, and an annual election on the
return. It excludes inventory, land and spare parts. Cost alone does not qualify an item, so any size
screen is a screening aid only.

The defaults need review by a tax professional: the configured fiscal year and the treatment of
opening receivables collected after the change. Slashbooks applies no prepaid tax rule. The
books-export skill tells an agent how to propose a prepaid `tax_treatment` under the 12-month
rule (Treas. Reg. 1.263(a)-4(f)) for the preparer to confirm; a person records it.

A historical opening summary supports its complete fiscal period, not arbitrary earlier months.
Unsupported pre-cutover subperiods or dates before the source balances are refused before any
handoff files are written. A supported full-summary export identifies the included summary and
states that historical transaction detail is unavailable. Periods entirely before optional accrual
tracking began remain cash periods, and a period that crosses the tracking start is not refused.


## Set the fiscal year

Record the company's established fiscal calendar with a preview, then apply that preview:

```sh
books entity set-fiscal-year PATH --start 07-01
books entity set-fiscal-year PATH --start 07-01 --apply --plan-hash HASH_FROM_PREVIEW
```

`07-01` means July 1 through June 30; `01-01` is the calendar-year default.
The setting accepts the first day of any month (`MM-01`). It does not support
52/53-week calendars or short transition years. It records the company's calendar;
it does not elect or change its tax year with a tax authority.

The command audits the change and refuses a stale preview or invalid ledger.
It is an owner/preparer decision and is not covered by standing posting authority.
An imported opening summary must agree with the chosen start; changing its
historical coverage requires a separate reviewed correction.

The fiscal year does not change a prepaid tax treatment: the engine uses only a recorded
value. Tax-view metadata records `fiscal_year_start`. Accrual
balance sheets use the calendar for current-year earnings; imported summaries
retain their evidenced fiscal calendar. Explicit report/export date ranges remain
as requested. `--year` remains the calendar-year shortcut; for July–June, use
`--from 2026-07-01 --to 2027-06-30`. Existing exported files and published statements are unchanged.

Capitalized prepaid balances remain assets even when an account also holds
prepaids deducted on payment; only the capitalized portion appears in the tax view.

The calendar limitations follow [IRS Publication 538](https://www.irs.gov/publications/p538).
Confirm the company's established method and calendar with its preparer.
