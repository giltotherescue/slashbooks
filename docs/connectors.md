# Connectors

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

The Stripe connector is the clearest example. Pulling from Stripe directly
captures gross revenue and Stripe fees separately, instead of only the net amount
that lands in your bank. That matters, because fees are a deductible expense and
gross revenue is what your real margins are built on.

So feeds give you breadth, and direct connectors give you fidelity for the
platforms that matter most.

## Custom connectors: connect to anything

Because Slashbooks is agent-native, you are never blocked waiting for an official
connector. Your agent can build a small connector for any source with an API,
scoped to your company and stored under `ingestion/custom/` (preserved across
plugin upgrades, never committed to the plugin repo).

A connector only fetches data and writes it in the normalized format below. The
same deterministic importer then does the accounting, so a custom connector
behaves exactly like a built-in one:

- Trusted counterparties auto-post; unknown ones go to the review queue.
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
