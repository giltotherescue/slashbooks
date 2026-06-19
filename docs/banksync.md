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
