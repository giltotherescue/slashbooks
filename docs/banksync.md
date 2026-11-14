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
    "acct_old": {
      "ledger_account": "Assets:Bank:Operating-Checking",
      "provider": "banksync",
      "provider_account_id": "acct_old",
      "institution_name": "Chase",
      "last4": "0021",
      "account_type": "checking",
      "currency": "USD"
    },
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

**Add identity to an old mapping first.** A company that mapped its accounts
before this release has flat entries, such as `"acct_old": "Assets:Bank:Operating-Checking"`.
A flat entry has no identity, so `match-account` cannot match it and returns
`no_match`. Run `bank-account-map` again for the old ID with the same account and
the identity. It keeps the account and adds the fields:

```sh
books entity bank-account-map <company-dir> --feed-account-id acct_old \
  --account Assets:Bank:Operating-Checking --provider banksync \
  --institution-name Chase --last4 0021 --account-type checking --currency USD
```

`--last4` accepts a mask such as `****0021` and keeps the last four characters.

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
feed import never posts a row dated on or before it. Each such row goes to "To
check" (`books queue duplicates`), either matched to the row it repeats or marked
`before_cutover_not_in_books`, so a late row that the old source never sent is
seen by a person and not lost. Pass `--backfill` to import these rows in the
normal way. Coverage windows start the day after the cutover date. Remove a
cutover date with `books entity bank-account-map ... --clear-cutover-date`.

**Keep earlier rows as history only.** When the books already hold the account's
history through the cutover date from an earlier source, a person can set
`books entity bank-account-map ... --cutover-date YYYY-MM-DD --history-before-cutover context_only`.
A posted feed row on or before that date is then stored for audit in
`staging/context-history.json`. It is never posted, and it makes no "To do" or
"To check" item and does not block a period close. A later sync of the same row
is skipped. `books queue context-history --json` gives one summary per account
(count, dates and a message); add `--account ACCOUNT --json --limit N` to list
the rows. The default, `review`, is the behavior in the paragraph above. If the
cutover date was set too late, move it earlier and run
`books queue context-release --account ACCOUNT --after YYYY-MM-DD`: the stored
rows after that day go to the normal import path with the duplicate check on.
`books queue context-release --source-id ID` releases one row on purpose. It
refuses a row that matches a posted entry or a queued item (the result names the
matched entry) unless `--force` is given. A release never leaves a half-done
state: it first sets aside a row dated before its account's opening balance
(`refused`, reason `before_opening`), then imports the rest, and marks a stored row
`released` exactly when it is posted, queued or held. The summary keeps `count`
(stored source IDs) and `distinct_count` (different date, amount and description):
a provider that issues new IDs for the same transaction is stored once for each
ID, and the message says so. A stored row that a later sync sends down the normal
path (after you move the cutover earlier, or set the mode back to `review`) is
marked `released`, and an `inactive` summary entry says that the setting no longer
covers the rows still stored. `books queue context-adopt` moves rows that an
earlier import already held in "To check" into context history. A hold marked
`before_cutover_not_in_books` is a row that the books never had and may be real,
so it stays in "To check" unless `--include-unmatched` is given; the result counts
`moved_by_match_rule` and `left_by_match_rule`. A context row still uses up the book
entry that it matches in the duplicate check, so a later row is judged exactly as in
`review` mode. A pending row on or before the date is still skipped, and a row after
the date follows the normal rules. `staging/context-history.json` has a
`schema_version`. It is read in full once for each ingest and rewritten once for each
ingest that adds rows; a two-year history of a busy account is a few thousand rows
(a few megabytes at most), which is the size this design expects.

Every connected feed needs a mapping. `books ingest` stops, and posts nothing,
when a row's account ID is not mapped, instead of opening a new
`Assets:Bank:<name>` account.

**Moving from a generic or Mercury ingest to BankSync.** Rows imported by a
generic `books ingest` (or from Mercury) keep their raw IDs and have no provider.
When the same ledger account then gets a BankSync feed, only an exact same-day
match (date, amount and description) is held against those rows. Set a
`cutover_date` on the BankSync mapping, the last day the earlier source owns, so
that every earlier BankSync row goes to review and is not posted.
