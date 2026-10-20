# BankSync setup

BankSync is the easiest way to get live bank and card activity into Slashbooks.

*Slashbooks is an independent project and is not affiliated with, endorsed by, or
sponsored by BankSync. BankSync is a separate commercial product
([banksync.io](https://banksync.io)); you supply your own account and API key.*

## BankSync account setup

1. Create a BankSync account and choose a plan that includes the banks and cards
   you need.
2. Link the business checking, savings, and card accounts inside BankSync.
3. When BankSync asks where to send your data, you can skip that step. Slashbooks
   reads from the BankSync API directly; you don't need to configure a feed,
   webhook, warehouse, or destination.
4. Save your BankSync API key in a file named `.env` inside the company books
   directory. The file should contain one line:

```env
BANKSYNC_API_KEY=paste-your-key-here
```

You can also ask Claude Cowork, Claude Code, or Codex to create this file after
you paste the key. The `books` command reads `./.env` from the current directory,
so you don't have to re-enter the key in every shell session. The company
`.gitignore` created by `books entity init` ignores `.env` by default. Do not
commit it.

If you use a different environment variable name, pass it with
`--api-key-env <NAME>`.

## Verify connected accounts

BankSync publishes a supported-bank directory and lets you search for banks
inside the BankSync app before connecting. Slashbooks does not currently search
that directory itself; it verifies the banks and accounts that are already
connected to your BankSync workspace.

List connected banks:

```sh
books connector banksync banks
```

List accounts for a connected bank:

```sh
books connector banksync accounts --bank-id <bank-id>
```

Before closing books, map each connected account ID in `entity.json`:

```json
"bank_account_mappings": {
  "acct_checking_123": "Assets:Bank:Mercury-Checking",
  "acct_savings_456": "Assets:Bank:Mercury-Savings",
  "acct_card_789": "Liabilities:CreditCard:Amex"
}
```

Use `Assets:Bank:*` for bank/checking/savings accounts and
`Liabilities:CreditCard:*` for credit cards. Slashbooks has a fallback based on
BankSync account names and account types, but explicit mappings are the intended
setup because they line up the feed accounts with opening balances and the chart
of accounts.

## Download activity

Download normalized activity for all connected banks in a date range:

```sh
books connector banksync download \
  --from 2026-01-01 \
  --to 2026-01-31 \
  --output ~/Documents/books/acme-co/ingestion/banksync-2026-01.json
```

Filter by bank name when needed:

```sh
books connector banksync stats --bank-name "Chase" --from 2026-01-01 --to 2026-01-31
```

Omit `--bank-name` to include every connected bank. If you omit dates, the CLI
defaults to the previous 30 days. The default scope is `self`; use `--scope
family` only if your BankSync setup uses that scope.

## Validate a new BankSync setup

Run the validation probe for new BankSync accounts:

```sh
books connector banksync validate \
  --from 2026-01-01 \
  --to 2026-01-31 \
  --output ~/Documents/books/acme-co/ingestion/banksync-validation.json
```

That probe checks the practical ingestion assumptions Slashbooks relies on: stable
IDs, historical depth, pending/posted behavior, and page-boundary completeness.

## Switching bank data providers

A company can move from one bank data provider (BankSync, Plaid, Stripe
Financial Connections or another) without a second cash account and without the
same transaction in the books twice. Four rules make this safe.

**One ledger account per bank account.** `bank_account_mappings` in
`entity.json` maps each provider account ID to a ledger account. The value is
the account name, or an object that also records who the account is:

```json
{
  "bank_account_mappings": {
    "acct_old": "Assets:Bank:Operating-Checking",
    "plaid_1": {
      "ledger_account": "Assets:Bank:Operating-Checking",
      "provider": "plaid",
      "provider_account_id": "plaid_1",
      "institution_id": "ins_3",
      "institution_name": "Chase",
      "last4": "0021",
      "account_type": "checking",
      "currency": "USD",
      "cutover_date": "2026-03-01"
    }
  }
}
```

The flat form still works and is still written when no identity field is given.
Record the identity with `books entity bank-account-map`:

```sh
books entity bank-account-map <company-dir> --feed-account-id plaid_1 \
  --account Assets:Bank:Operating-Checking --provider plaid \
  --institution-id ins_3 --institution-name Chase --last4 0021 \
  --account-type checking --currency USD --cutover-date 2026-03-01
```

**Find the old account for the new connection.** When the new provider gives the
same bank account a new ID, ask for a proposal:

```sh
books bank match-account --entity <company-dir> --provider plaid \
  --provider-account-id plaid_1 --institution-name Chase --last4 0021 \
  --account-type checking --currency USD --json
```

The result `status` is `proposal` (one ledger account matches on institution,
last four digits, type and currency), `already_mapped`, `ambiguous` or
`no_match`. A proposal carries the `bank-account-map` command to run. The
command changes nothing: a person confirms the proposal first, and only then is
the new ID mapped. The old ID stays mapped, so old rows keep resolving.

**New rows have a provider namespace.** `books ingest` writes `<provider>:<id>`
as the source ID of each connected-feed row, such as `banksync:abc` or
`plaid:abc`. `--source banksync`, `--source plaid` and a BankSync download name
the provider; `--feed-provider NAME` names any other. Rows already in the books
keep their old IDs. An old BankSync ID `abc` and `banksync:abc` are the same row
in every duplicate check, so re-running a download never posts a row twice.

**Overlap goes to review, never to the books.** Every connected-feed import
checks each row against posted entries and queued review items on the same
ledger account. A row is held in "To check" when the date, amount and
description all match, or when the amount matches within three days and the
other row came from a different source. Two purchases of one amount from the
same feed are not held against each other. See `books queue duplicates`.

**Set a cutover date.** `cutover_date` is the last day the old source owns. A
feed import skips rows on or before it and prints how many it skipped. Pass
`--backfill` to import them on purpose. Coverage windows start the day after
the cutover date.

Every connected feed needs a mapping. `books ingest` stops, and posts nothing,
when a row's account ID is not mapped, instead of opening a new
`Assets:Bank:<name>` account.
