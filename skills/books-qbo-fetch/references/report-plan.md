# QuickBooks report plan

Use this reference to turn the user's purpose and dates into exact QuickBooks
exports. Establish the company accounting policy first, then use the same Cash
or Accrual basis wherever QBO offers a selector. A selector alone is not policy evidence.

## Full comparison or backtest

The table describes the default cash set. For an accrual company substitute
Accrual consistently and pass `--basis accrual`; do not claim unavailable
pre-cutover transaction history from an opening summary. Use the dedicated
accrual opening branch below for a same-basis migration.

Let `from` and `to` be the inclusive comparison period. Let `prior` be the calendar
day immediately before `from`.

| Required export | QuickBooks setting | Slashbooks use |
|---|---|---|
| Chart of Accounts | Current account metadata; no historical date | Account names, types, and detail types |
| Trial Balance | As of `prior`; Cash | Opening-balance control and account balances |
| Balance Sheet | As of `prior`; Cash | Opening balances before the comparison period |
| Balance Sheet | As of `to`; Cash | Ending balance-sheet comparison |
| Profit and Loss | `from` through `to`; Cash; display one Total column | Income and expense comparison |
| General Ledger | `from` through `to`; Cash; all accounts | Detailed comparison evidence |
| Transaction Detail by Account | `from` through `to`; Cash; all accounts | Independent transaction-detail evidence |

The two Balance Sheets are separate required files even when QuickBooks gives them
similar filenames. Verify their visible **As of** dates before each export.

For the Chart of Accounts, use the Chart of Accounts page's report/export action or
an Account List report only when the resulting file contains account name, account
type, and detail type columns. Current QuickBooks balances in that export are not
historical evidence.

The Profit and Loss parser expects the report's single **Total** column. Do not set
Display columns by to Months for the canonical comparison export. A monthly P&L may
be collected as optional supporting evidence, but it does not replace the required
single-total report.

## Accrual opening

Collect Chart of Accounts, full mapped Trial Balance and Balance Sheet as of the
day before cutover, and fiscal YTD P&L through that date, all on Accrual basis.
Require an explicit fiscal start and detailed A/R, A/P, prepaid/deferred and
other non-bank control support, or dated external support that reconciles exactly.
The full TB includes source retained equity and YTD income/expense; do not add
derived current earnings twice or create an unexplained equity plug.

```sh
scripts/books qb inventory <entity-path>/ingestion/quickbooks --company=<confirmed-qbo-company> --cutover <YYYY-MM-DD> --basis accrual --fiscal-start <YYYY-MM-DD>
```

Stage sources durably under the selected company and return to onboarding for
`accrual migration preview`/`apply`; inventory alone does not validate item or
schedule detail. See [accrual intake](../../../docs/accrual-intake.md#quickbooks-opening).
Missing dates or detail block automatic schedules and remain visible in readiness.
Full history reconstruction and basis conversion are outside this workflow.

## Cash opening balances only

Let `cutover` be the first day Slashbooks will keep books and `prior` the calendar
day immediately before it. Collect:

- Chart of Accounts
- Trial Balance as of `prior`, Cash
- Balance Sheet as of `prior`, Cash

Run inventory even though it will show the later comparison reports as missing.
Describe this as an opening-balance set, not a complete backtest set. The downstream
onboarding workflow decides whether and how to import it.

## Optional supporting reports

Collect additional reports only when the business or requested analysis needs them:

- Statement of Cash Flows for `from` through `to`
- A/R or A/P aging at `to`
- invoice, payment, sales-tax, payroll, reconciliation, vendor, or 1099 reports
- Profit and Loss by Month as a secondary monthly control

These reports can help an accountant or future analysis, but the current Slashbooks
QuickBooks inventory does not use them to declare the standard comparison set ready.
Keep optional files distinct in the summary and never present them as substitutes
for missing required slots.

## Period rules

- Use calendar dates, not relative presets.
- Do not infer a fiscal period when the user gave explicit dates.
- If the user asks for a year but the entity uses a non-calendar fiscal year, confirm
  whether they mean calendar year, fiscal year, or a specific date range.
- If `from`, `to`, or `cutover` is unknown, ask before downloading. A wrong period is
  not reusable evidence.
- Confirm that `from <= to` and calculate `prior` as an actual date, including month
  and year boundaries.

## Inventory acceptance commands

After confirming the QuickBooks company in the browser, use that visible company
name as the expected company in the inventory command. Do not replace it with the
Slashbooks entity name unless the owner confirmed they are the same name.

For a full comparison:

```sh
scripts/books qb inventory <entity-path>/ingestion/quickbooks --company=<confirmed-qbo-company> --from <YYYY-MM-DD> --to <YYYY-MM-DD> --basis <cash|accrual>
```

For opening balances only:

```sh
scripts/books qb inventory <entity-path>/ingestion/quickbooks --company <confirmed-qbo-company> --cutover <YYYY-MM-DD>
```

The full command must reject an export from another company, a report with different
dates, or an accrual-basis report where Cash was required. The opening command checks
the Chart of Accounts plus the cash-basis, prior-day opening evidence selected by the
cutover date.
