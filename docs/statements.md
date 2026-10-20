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
| `type` | `section` (header, no amount), `account`, `computed` (a line the engine derives, such as current-period net income), `subtotal` (a section total or Gross Profit) or `total` (a statement total) |
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
