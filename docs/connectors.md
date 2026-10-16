# Connectors

Bank-movement intake and supporting-document intake serve different purposes.
For accrual books, use [the provider-neutral supporting document contract](accrual-intake.md)
for any connector, MCP response, export, or email attachment. Bank activity alone
does not determine revenue, service periods, or settlement. Generic journals and
external support cover documented accounting work beyond packaged recipes; the
engine validates the effects and authority.

A connector is how Slashbooks gets your financial data in. Connectors are
read-only: they pull transactions from a source and hand them to Slashbooks in a
normalized format. Nothing is ever sent back to the source.

A connector is the Slashbooks side; a provider is the outside service it talks to
(Stripe, Mercury, BankSync).

## What ships today

- **BankSync** for live bank and card feeds across many institutions. See
  [BankSync setup](banksync.md).
- **Stripe** for balance transactions and payouts, pulled with your API key. See
  [imports](imports.md).
- **Mercury** for operating, credit, and treasury account activity. See
  [imports](imports.md).
- **CSV** for Amex activity exports, and as a fallback for any account you cannot
  connect live. See [imports](imports.md).

This list grows over time, and you are not limited to it (see Custom connectors
below).

## Feeds vs direct connectors: breadth vs fidelity

A bank feed sees a Stripe payout as one net deposit: gross sales minus fees minus
refunds, collapsed into a single number. A direct connector pulls from the source
itself, so it can reconstruct the detail the feed throws away.

Stripe balance transactions retain gross, fees and net amounts. The raw local
download preserves this evidence; it does not itself post separate revenue and
fee journals. The cash-only [Stripe accounting workflow](stripe-accounting.md) uses complete
linked evidence and explicitly approved revenue rules to post those components
through processor clearing. Bank payout arrivals clear transit rather than
counting as additional revenue. For accrual books, retain the same source
documents and use generic accounting intake with explicit recognition/schedule
and settlement treatment; the packaged cash recipe does not run on accrual books.

So feeds give you breadth, and direct connectors give you fidelity for the
platforms that matter most.

## Custom connectors: connect to anything

Because Slashbooks is agent-native, you are never blocked waiting for an official
connector. Your agent can build a small connector for any source with an API,
scoped to your company and stored under `ingestion/custom/` (preserved across
plugin upgrades, never committed to the plugin repo).

A connector only fetches data and writes it in the normalized format below. The
same deterministic importer normalizes, deduplicates, and routes it for review,
so a custom connector follows the same basis-specific rules as a built-in one:

- In cash books, trusted counterparties can auto-post; unknown ones go to review.
- In accrual books, rows are staged for linking to items/schedules or explicitly
  established `new_cash_event` treatment; trusted cash categories cannot choose
  recognition or settlement.
- Re-running is idempotent; duplicates are skipped by `id`.
- Every change is recorded in the audit log.

Bring the data in with:

```sh
books ingest <file.json> --entity <company-dir> --source <name>
```

### Normalized transaction format

The input is a JSON array of transactions, or an object with a `transactions`
array. Each transaction has:

- `id` (required) is a stable unique id, used to skip duplicates on re-ingest.
- `date` (required) is an ISO-8601 date, such as `2026-01-15`.
- `description` is the counterparty or memo text.
- `amount` is a signed decimal string; negative means money out. You can instead
  supply `creditAmount` and `debitAmount`.
- `accountId` is matched against the entity's `bank_account_mappings`.
- `accountName` and `accountType` (`checking`, `savings`, `credit_card`) are
  optional fallbacks used when `accountId` is not mapped.
- `pending` is optional; pending transactions are staged, not posted.

A copyable starting point is in
`examples/connectors/custom_connector_template.py`.

### Coverage windows

A connector can also say which dates it returned in full for an account. Add a
`coverage_windows` list to the object form of the input:

```json
{
  "transactions": [],
  "coverage_windows": [
    {"account": "Assets:Bank:Operating-Checking", "from": "2026-01-01", "to": "2026-01-31", "source": "Acme Bank API"}
  ]
}
```

Each window needs `from` and `to` dates and either a ledger `account` under
`Assets:` or `Liabilities:`, or an `accountId` that is in
`bank_account_mappings`. `books ingest` checks the windows before it imports,
and records each one as a `feed_sync` record for `books coverage check` after
the import succeeds. Add a window only for dates the provider returned in
full: no paging limit, no history limit and no pending items.

`books ingest` makes these windows itself for a BankSync download. Each window
starts on the later of the requested start and the first posted transaction,
and ends on the earlier of the requested end and the import date. If the
account has pending items, the window ends the day before the first one.

For an Amex activity CSV, pass the date range that was chosen for the download
to `books connector csv parse --period-start YYYY-MM-DD --period-end
YYYY-MM-DD --output <file>`. The output then carries the window for
`books ingest`.

If BankSync marks an account read as incomplete (`meta.partial`), the download
carries `"partial": true` for that account, and `books ingest` writes no window
for it.

To turn off every window that `books ingest` would write from its input, pass
`--no-feed-window` (the API parameter is `record_feed_window=False` on
`bookkeeping.ingest.coverage_windows`). The flag also ignores `coverage_windows`
in the input. A host that reads a provider in chunks uses it on every chunk,
because a chunk that stops during paging has not read the days that its date
range names. Local use is unchanged: without the flag, the engine writes the
windows as above.

A hosted import does not record windows from the input file. The host writes
the `feed_sync` records itself, in the format in
[tax-handoff.md](tax-handoff.md#statement-record-contract-schema-version-1).
