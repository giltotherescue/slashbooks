# Financial statements

The `books report` commands print the statements a buyer, lender or accountant expects. The engine
computes every amount with `Decimal` math. A web page, a skill or a spreadsheet only displays the numbers.

| Command | Statement |
| --- | --- |
| `books report pnl --from DATE --to DATE` | Profit and loss |
| `books report balance-sheet --as-of DATE` | Balance sheet |
| `books report cashflow --from DATE --to DATE` | Statement of cash flows (indirect method) |
| `books report trial-balance --as-of DATE` | Trial balance |
| `books report general-ledger --from DATE --to DATE` | General ledger |

Each command takes `--entity PATH`, `--format text|json` and `--output FILE`. The default output of
`pnl`, `balance-sheet`, `trial-balance` and `general-ledger` did not change. The flags below are opt-in.

## Statement structure: `--structure`

`books report pnl` and `books report balance-sheet` with `--format json --structure` keep every existing
field and add `schema_version: 2` and a `structure` object. `structure.lines` is the statement in print order.
Each line has:

| Field | Meaning |
| --- | --- |
| `type` | `section` (header, no amount), `account`, `computed` (a line the engine derives: current fiscal year net income or retained earnings), `subtotal` (a section total or Gross Profit) or `total` (a statement total) |
| `key` | A stable machine name: the ledger account for an `account` line, otherwise a name such as `total_revenue`, `gross_profit`, `net_income`, `total_assets`, `total_liabilities`, `total_equity`, `total_liabilities_and_equity` |
| `label`, `indent` | Display text and nesting level |
| `amount` | Decimal string with two places. Not on `section` lines |
| `account` | The ledger account. `account` lines only |
| `section` | The `key` of the section that holds the line |

```json
{"type": "subtotal", "key": "gross_profit", "label": "Gross Profit", "amount": "1500.00", "indent": 0}
{"type": "total", "key": "total_liabilities_and_equity", "label": "Total Liabilities and Equity", "amount": "6930.00", "indent": 0}
```

- The P&L has these rows: `total_revenue`, then (only when a cost-of-sales account exists) `total_cost_of_sales` and
  `gross_profit`, then `total_expenses`, then `net_income`. The engine finds cost-of-sales accounts by name only: an expense account whose
  second name part is Cost of Sales, Cost of Goods Sold, COGS or Cost of Revenue. An account with another name is an
  ordinary expense, so Gross Profit is absent when no account has such a name. In `structure`, `total_expenses`
  does not include cost of sales. The older `sections` field keeps every expense in one total.
- The balance sheet structure adds `balanced` and `difference` (assets minus liabilities and equity).
- With `--format text`, `--structure` changes nothing.

## Retained earnings and net income

The books have no closing entries. The balance sheet and the trial balance close earlier fiscal years when the report
is made, in every view (cash, recorded and accrual), as QuickBooks and Xero do:

- **Balance sheet.** Equity shows the equity accounts, then "Retained earnings from prior fiscal activity" (income less
  expenses of all earlier fiscal years; only when it is not zero), then "Net Income (current period)" (income less
  expenses from the first day of the current fiscal year to the as-of date). In `--structure` their keys are
  `retained_earnings_prior` and `net_income_current_period`. Owner contributions, draws, distributions and opening
  balances stay account lines. Total equity is the same as one line for all earnings.
- **Trial balance.** Income and expense accounts show the current fiscal year only. Earlier years are one computed row,
  "Retained earnings from prior fiscal activity", with `key: "retained_earnings_prior"` and `computed: true` in
  `meta.raw_rows`. Total debits still equal total credits; for a company with more than one fiscal year they are lower
  than the life-to-date totals, as in QuickBooks.
- **Fiscal year.** The year follows `fiscal_year_start` (`books entity set-fiscal-year`, default January 1). A company
  opened from an imported summary (`books accrual migration`) keeps the fiscal start of that summary. A change to the
  fiscal year changes only the split in later reports (and so the trial balance rows and totals), never an entry,
  a balance sheet total or total equity.
- **Metadata.** When a balance sheet shows the retained line, `meta` has `fiscal_start`, `cumulative_net_income`
  (all earnings to the as-of date) and `current_fiscal_net_income`. The accrual view always has them. A trial balance
  whose income or expense rows moved to the current fiscal year has `meta.fiscal_start`, even when earlier years net to zero. A company with one fiscal year of activity gets the same output as before.
- **Profit and loss.** It was always the period that you ask for, so it does not change.

## Statement of cash flows

`books report cashflow --from DATE --to DATE` is the indirect method. It starts from net income and adds the
effect on cash of each balance sheet change. On accrual-enabled books it starts from the net income of
the recorded books, whatever the default profit and loss view is. The cash forecast in `books forecast` is a different report.

The engine puts each account in a group from its account `subtype`, the same value that `books accrual accounts`
reports. A cash book with no accrual records uses the account-name prefix.

| Group | Accounts |
| --- | --- |
| Cash (the statement total) | bank and cash accounts |
| Operating | net income, receivables, prepaid, payables, accrued liabilities, deferred revenue, customer deposits, credit cards, and the depreciation add-back (the change in accumulated depreciation) |
| Investing | fixed assets |
| Financing | equity accounts (owner contributions, draws) and loan, note, mortgage, debt and line-of-credit liabilities |
| Needs classification | every other account that changed: a payment processor or transfer balance, an other asset, an other liability with an unclear name, retained earnings |

The engine does not guess. A "Needs classification" group is always visible and always counted in the net change in
cash, so the statement still ties. A credit card is working capital, not cash: a card charge is an expense in net
income and a larger card balance in the operating group. Opening balances are a starting point, never a cash flow (see below).

Noncash asset acquisitions, such as equipment bought on account, financed directly by a loan, or
contributed by an owner, are excluded from activity subtotals and listed separately under
**Noncash investing and financing activities** (`noncash_events` in JSON). A later bank payment
explicitly allocated to a recorded equipment bill is investing; a mixed equipment/expense bill
splits each payment by its recorded lines, with cumulative cent rounding. If a credit, writeoff
or offset changes a mixed capital bill, its payment is shown under Needs classification until its
remaining capital and operating purpose is reviewed; the original ratio is not reused. The engine does not
invent a purchase-purpose link for an unsupported opening payable or an unlinked payment.

Definition of cash: cash means bank and cash accounts. Credit card balances are liabilities in operating working
capital. The accrual cash view counts a card charge as paid on the charge date, so the two reports differ on purpose.
The JSON has this text in `cash_definition`, and the text report prints it.

Ledger integrity check (`reconciliation`): beginning cash plus the net change must equal the bank and cash accounts at
the end date, and the engine reads that total again straight from the postings. Both comparisons use cents. This is
not an outside proof: net income comes from the same postings, so the check catches an unbalanced or damaged ledger.
It does not catch a missing transaction or an account in the wrong group, because a wrong group moves an amount between
two groups and leaves the net change the same. If it fails, `reconciliation.ties` is `false`, `difference` and
`cash_accounts_difference` show the amounts, and the command exits with code 1. An asset account that is named like cash
(for example `Assets:Checking`) but is not a bank or cash account is listed in `warnings` with code
`possible_cash_account`.

```json
{
  "kind": "cashflow",
  "schema_version": 2,
  "method": "indirect",
  "from_date": "2026-01-01",
  "to_date": "2026-01-31",
  "sections": [{"label": "Operating activities", "rows": [{"label": "Net income", "amount": "4900.00", "indent": 1}], "total": "5300.00", "total_label": "Net cash from operating activities"}],
  "totals": {"net_income": "4900.00", "net_cash_operating": "5300.00", "net_cash_investing": "-2000.00",
             "net_cash_financing": "12000.00", "needs_classification": "-300.00",
             "net_change_in_cash": "15000.00", "beginning_cash": "0.00", "ending_cash": "15000.00"},
  "reconciliation": {"beginning_cash": "0.00", "net_change_in_cash": "15000.00", "ending_cash_per_statement": "15000.00",
                     "balance_sheet_cash": "15000.00", "check": "ledger_integrity", "difference": "0.00",
                     "cash_accounts_total": "15000.00", "cash_accounts_difference": "0.00", "ties": true,
                     "cash_accounts": [{"account": "Assets:Bank:Checking", "beginning": "0.00", "ending": "15000.00"}]},
  "warnings": [],
  "cash_definition": "Cash means bank and cash accounts. Credit card balances are liabilities in operating working capital.",
  "needs_classification": [{"account": "Assets:Processors:Stripe", "label": "Change in Assets › Processors › Stripe",
                            "subtype": "other_asset", "cash_effect": "-300.00", "reason": "other asset: operating, investing or a clearing balance is not known"}],
  "structure": {"version": 2, "lines": ["..."]},
  "meta": {"basis": "cash"}
}
```

### Opening balances

An imported opening entry (QuickBooks opening or accrual opening) is part of the beginning balances, never a cash
flow. An opening entry dated on the `--from` date is counted in the beginning balances. An opening entry dated after
`--from` and on or before `--to` makes the beginning balances unknown, so the command refuses and gives the date to
start from. An equity account whose name says opening is never placed in financing; it goes to "Needs classification".

## Comparative periods: `--compare previous|prior_year`

`--compare` works on `pnl`, `balance-sheet` and `cashflow`, in text and JSON. The engine computes the second period
and joins the two by line. JSON output keeps the usual fields, adds `schema_version: 2`, `prior_totals`, a
`comparison` object, and puts the joined lines in `structure.lines`.

Prior period:

- P&L and cash flow, `previous`: the period just before. A range of whole calendar months moves back by the same
  number of whole months (a month, a quarter or a year). Another range moves back by its length in days.
- P&L and cash flow, `prior_year`: the same dates one year earlier.
- Balance sheet, `previous`: the same day one month earlier. `prior_year`: one year earlier.
- A month-end date stays a month-end date (2028-02-29 becomes 2027-02-28).

Fields on each non-section line:

| Field | Meaning |
| --- | --- |
| `amount`, `prior_amount` | Current and prior amounts, exact to the cent. A line in only one period shows `0.00` in the other |
| `change` | `amount - prior_amount` |
| `change_pct` | `change / abs(prior_amount) * 100`, or `null` when the prior amount is zero |
| `pct_of_income`, `prior_pct_of_income` | P&L only. The line divided by total revenue, times 100, for each period. `null` when total revenue is zero |

Rounding rule: amounts are never rounded. Each percentage is rounded one time to two decimal places, half away
from zero. The rule is also in `comparison.rounding`.

When the prior period has no data, `comparison.prior.has_data` is `false`, `comparison.prior.note` says why, and
every prior, change and percent-of-prior field is `null`. The current period is reported in full. "No data" means no
ledger entry in the prior range (or on or before the prior date for a balance sheet), or a prior period before the
imported coverage of an accrual company.

```json
{"comparison": {"mode": "prior_year", "prior": {"has_data": true, "note": null, "from_date": "2025-01-01", "to_date": "2025-01-31"},
                "rounding": "Amounts are exact cents. Percentages are rounded once to 2 decimal places, half away from zero (ROUND_HALF_UP)."},
 "prior_totals": {"net_income": "530.00"}}
{"type": "subtotal", "key": "gross_profit", "label": "Gross Profit", "amount": "1500.00", "indent": 0,
 "prior_amount": "700.00", "change": "800.00", "change_pct": "114.29", "pct_of_income": "75.00", "prior_pct_of_income": "70.00"}
```

## Accrual books

The same commands work on accrual books. The cash flow statement uses the changes in receivables, payables,
prepaid, deferred revenue, customer deposits and accrued liabilities, found through the account `subtype`. Accounts
that the engine cannot place go to "Needs classification". These are approximate:

- An other liability or other asset without a recognized name is not placed. It is shown, not guessed.
- Sales tax and payroll liabilities have no recognized name, so they go to "Needs classification".
- Retained earnings and prior-period equity moves go to "Needs classification".
- A gain or loss on the sale of a fixed asset is not split from the sale proceeds.
- A period that starts inside an imported fiscal summary is refused with the first available date (the cutover). The P&L accepts such a period; a cash flow statement cannot, because the opening balances are its starting point. In `--compare`, a prior period like that is reported as having no data.


## Correct a statement for the same account and period

Use `books statement list --entity PATH` to compare saved statements. It includes
`replaced_by` and a `selection_revision`. Choose the correct imported statement:

```sh
books statement select --entity PATH --statement-id CORRECT_ID \
  --expected OLD_ID --expected CORRECT_ID --expected-revision REVISION
```

Pass every saved statement ID for that account and exact period from the review.
The command refuses stale reviews and closed periods. It atomically saves the
choice and its history without changing original files, transaction intake or
ledger entries. Retrying the same choice is safe. Selecting an earlier statement
again reverses the choice. A later upload remains active until separately reviewed.

Import new corrected files through the usual duplicate check first. Selection
never repairs incorrect recorded transactions; correct those separately. A
reconciliation using a replaced document must be run again with the current one.

## Reporting goals and report categories

A reporting goal records what the owner wants the reports to show, for example
profit for each service line or cash on hand. The engine stores and validates the
goals. No entry, category, rule or report changes because of them. An agent reads
the goals and proposes categories or moves, and a person approves the changes.

### The `reporting_goals` key

`entity.json` keeps the goals in one key:

```json
{"reporting_goals": {"version": 1, "goals": ["profit_by_line", "cash_runway"], "lines": ["Installs", "Repairs"]}}
```

| Code | What the owner wants to see |
| --- | --- |
| `profit_by_line` | Income and direct costs for each service or product line |
| `profit_by_job` | Profit for each job, client or project |
| `cash_runway` | Cash on hand and how many months it lasts |
| `spend_vs_last_year` | Spending by category against the last year |
| `sales_tax_owed` | Sales tax collected and not paid yet |
| `owner_pay` | What the owner took out of the business |
| `recurring_revenue` | Subscription or retainer income each month |
| `basics` | A standard profit and loss and balance sheet |

- `goals` lists codes from the table in the owner's order, with no repeats (at most 8).
- `lines` lists the lines that the owner named. Lines are allowed only with
  `profit_by_line` or `profit_by_job`.
- Each line has 1 to 40 characters: letters, digits, spaces and
  `& ' . , / ( ) + -`. The command collapses repeated spaces. Two lines that differ
  only in letter case are the same line.

`bookkeeping.reporting_goals.reporting_goals(config)` returns the validated value,
or `None` when the key is absent. It raises `ValueError` when the stored value is
not valid. The key travels in the migration bundle with the rest of `entity.json`,
and it does not change the report fingerprints of the bundle.

### Record the goals: `books entity set-reporting-goals`

```sh
books entity set-reporting-goals PATH --goal profit_by_line --goal cash_runway --line Installs --line Repairs
books entity set-reporting-goals PATH --goal profit_by_line --goal cash_runway --line Installs --line Repairs --apply --plan-hash HASH
books entity set-reporting-goals PATH --clear --apply
```

- Without `--apply` the command shows a preview and writes nothing. The preview
  shows `effects.from` and `effects.to`, and `entries_changed` is 0.
- A new value replaces the old value. `--clear` removes the key.
- `--apply --plan-hash H` refuses with `stale_preview` when the company changed
  after the preview. When the company already holds the requested value (for
  example an exact retry of the same `--apply --plan-hash H`), the command gives
  `already_applied` and writes nothing.
- An apply writes the audit event `reporting-goals-set` with the old value, the new
  value, the plan hash and a unique `operation_id`. If the apply stops after the
  file write, a new database connection looks for that `operation_id`. The command
  puts the original bytes of `entity.json` back (also when the stop comes during the
  rename or the directory sync) only when the lookup proves that the
  event is absent and the file still holds the bytes that the command wrote. It
  checks the bytes again directly before the rename. In all other cases the file
  stays as it is.
- The apply holds the company write lock that every engine writer of `entity.json`
  takes, from the first read to the commit and any recovery. Directly before it
  replaces `entity.json`, it compares the file with the bytes that it read. If the
  file changed, the apply stops with `stale_preview` and writes nothing. Editors
  that change `entity.json` without the company lock are not supported: the byte
  checks narrow that window but cannot close it.
- `entity.json` is the state. When it holds a value that no `reporting-goals-set`
  event records (after a stop before the commit, or after a hand edit), the preview
  shows the warning `unrecorded_change`. An apply of that same value without a plan
  hash writes nothing. An apply with the plan hash of that preview writes the event,
  marked `confirmed_by_apply: true`. A wrong plan hash is refused with
  `stale_preview`. The plan hash covers the record hash of the last
  `reporting-goals-set` event, so a used plan hash cannot confirm a later change. An apply of a new value writes a normal event.
- A stored value that is not valid gives the warning `invalid_stored_value`, and the
  getter refuses it. Give a new value with `--goal`, or `--clear`, to replace it.
- Line names have no count limit. Each name has 1 to 40 characters. The complete
  normalized stored value must fit 65,536 bytes when serialized as compact UTF-8
  JSON (`ensure_ascii=False`, separators `,` and `:`). Larger values are refused
  before any write; lines are never truncated.
- Error codes: `unknown_goal`, `repeated_goal`, `too_many_goals`, `no_goals`,
  `lines_need_line_goal`, `reporting_goals_too_large`, `invalid_line`, `repeated_line`,
  `nothing_to_set`, `clear_with_values`, `stale_preview`, `ledger_integrity`.

### Add a report category: `books entity account-add --category`

`books entity account-add PATH --account ACCOUNT --category` adds an income or
expense category. With `--category` the command accepts only these shapes:

- `Income:<Line>`
- `Income:<Group>:<Line>` (one level under an income group)
- `Expenses:<Name>`
- `Expenses:Cost-of-Sales:<Line>` (a cost-of-sales group, found by name as
  `--structure` finds it)

The command refuses all other shapes, and a name with a control character such as
a final newline, before any write. The usual account checks also apply. Without `--category` the command accepts every valid account name, as
before. A new account has no postings, so no report amount changes. To move entries
from earlier months into the new account, give an open date on or before the first
entry, for example `--open-date 2026-01-01`. For an account that exists, an earlier
`--open-date` moves its open date back.

### Move entries to another category: `books ledger reclassify`

```sh
books ledger reclassify --entity PATH --from-account Income:Consulting --to-account Income:Installs --source-id ID --source-id ID
books ledger reclassify --entity PATH --from-account Income:Consulting --to-account Income:Installs --source-id ID --source-id ID --apply --plan-hash HASH
```

The command moves the posted entries that you name by source ID from one income or
expense account to another. Both accounts must be income or expense accounts, and
the target account must be in the account catalog. For other accounts, use
`ledger reclassify-bank` or `ledger correct`.

- Without `--apply` the command shows a preview and writes nothing. `moves` lists
  each entry with its date, payee, amount and tags, and the new source ID of its
  replacement.
- Each move writes a reversal and a replacement. The original entry stays. The
  replacement keeps the date, payee, narration, flag, tags, links and metadata, and
  gets a new source ID and `correction-of`. The reversal keeps the payee, tags and
  links, so a total by payee or by tag stays correct.
- One ledger transaction writes all the moves, or none.
- The source ID of an entry that moved before also finds its replacement. An entry
  that is already in the target account goes to `already_moved`. When all entries
  are already moved, the apply returns `already_applied` and writes nothing.
- An entry in a locked month refuses the whole request with `period_locked`. A
  locked month is a month in `closed_periods`, a date on or before `closed_through`,
  or a closed accrual period. `refs` lists the source IDs. A change in a locked
  month is a question for the accountant.
- A `closed_through` that is not a `YYYY-MM-DD` date, or a `closed_periods` value
  that is not a list of `YYYY-MM` months, refuses the request with
  `invalid_period_state` before any move.
- When every entry is already in the target account (for example an exact retry of
  the same `--apply --plan-hash H`), the command gives `already_applied`.
- Posting metadata stays on both the reversal and the replacement.
- Other error codes: `not_category_account`, `same_account`, `unknown_account`,
  `account_not_open` (the action gives the `account-add --open-date` command),
  `unknown_entry`, `entry_reversed`, `repeated_entry`, `not_in_from_account`,
  `managed_accrual_entry`, `too_many_entries` (more than 1000), `stale_preview`,
  `ledger_integrity`.
- With `--work-out`, the move is a `corrected` work event.

See [caller controls](agent-controls.md) for which of these commands change the books.
