# Cash schedules

`books forecast` builds a deterministic cash schedule for up to 13 rolling weeks.
It reads the ledger and an explicit assumption file. It does not post entries,
predict sales, extrapolate a P&L, or verify a bank balance.

The baseline is **recorded cash as of** the supplied date: all recorded postings
through that date in the selected USD bank/cash accounts. Transfers between
selected accounts net to zero. Future-dated ledger entries are excluded and
flagged. Missing opening balances, unimported activity and incomplete
reconciliation can change that baseline. Selecting an account does not prove its
cash is unrestricted or available; processor clearing, transit, receivables and
accounts named restricted are rejected.

## Saved assumptions

This synthetic example schedules an outstanding customer balance, a payment,
and a seven-day receipt delay. Save it in the company workspace, not the plugin.

```json
{
  "version": 1,
  "currency": "USD",
  "cash_accounts": ["Assets:Bank:Checking"],
  "items": [
    {
      "id": "customer-balance",
      "label": "Remaining project payment",
      "direction": "receipt",
      "group": "revenue",
      "expected_date": "2026-02-06",
      "amount_remaining": "1200.00",
      "confirmed": true,
      "already_settled": false,
      "evidence_refs": ["signed-contract-1"],
      "author": "Example owner",
      "date": "2026-01-31"
    },
    {
      "id": "software-renewal",
      "direction": "payment",
      "group": "overhead",
      "expected_date": "2026-02-10",
      "amount_remaining": "80.00",
      "confirmed": true,
      "already_settled": false,
      "evidence_refs": ["renewal-notice-1"]
    }
  ],
  "scenarios": [
    {"id": "late-receipts", "label": "Receipts seven days later", "receipt_delay_days": 7}
  ]
}
```

Every amount is a positive decimal string in cents; direction supplies its sign.
Receipts use `group: revenue`. Payments use `cogs`, `overhead`, `tax`, `debt`,
`owner` or `other`. Supply source, author and date only when known. References are
provenance, not independent confirmation that a payment will occur.

The base includes only confirmed, unsettled future **remaining** amounts.
Settled items, unconfirmed pipeline, overdue dates and dates beyond the horizon
remain visible but excluded. If an evidence reference is an already recorded
source ID, the item is excluded unless it explicitly states
`amount_basis: remaining_after_recorded_receipts`. Use that field for a genuinely
outstanding balance after a recorded deposit, never to repeat the received money.
The engine cannot prove external documents or detect duplicate economic events
with different assumption IDs; the owner/accountant must review that scope.

Each scenario shifts confirmed receipt dates only, by 0–365 days; payments do not
move. Receipts shifted beyond the horizon remain listed. No probabilities or
automatic ledger writes are involved.

## Run and return

```sh
books forecast --entity ./example-company --input ./cash-assumptions.json --as-of 2026-01-31 --weeks 13 --format json --output ./cash-schedule.json
books forecast --entity ./example-company --input ./cash-assumptions.json --as-of 2026-01-31 --weeks 13 --format csv --output ./cash-schedule.csv
```

The ordinary remote CLI transports the assumption input and generated report.
The command is classified as a business read; requested report artifacts can
still be published. The JSON includes exact as-of/horizon/accounts, baseline
warnings, weekly receipts/payment groups, assumptions and ledger digests,
exclusions and scenarios. CSV neutralizes spreadsheet formulas in untrusted
text while retaining numeric cash columns. Keep saved assumptions alongside the
report so another agent can explain or revise the same schedule later.
