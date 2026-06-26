# How to do your books with Slashbooks

This guide is for a founder, owner, or operator who wants to use Slashbooks for a
simple cash-basis company. If that's you, read on.

Slashbooks works best when each company has its own directory, bank and card data
comes from BankSync or known CSV exports, and you review unfamiliar transactions
before they become trusted rules.

**Your routine, in short:** set up once with `/books-onboard`, run `/books-close`
each month, ask `/books-ask` anytime, and run `/books-export` at tax time. The
rest of this guide explains each step.

## One-time setup

Create or choose a parent folder for company books, such as
`~/Documents/books`. Each company gets its own subfolder inside that parent,
such as `~/Documents/books/acme-co`. Do not use the `/books` plugin source
folder as the company books folder.

Open Claude Code, Claude Cowork, or Codex in the parent folder, enable the
`books` plugin, and say:

> I want to set up books for this company here.

If you just want to try Slashbooks, say:

> I want to try the Northstar demo books.

The agent will show the onboarding questions with fictional Northstar Metrics
LLC answers, then create synthetic SQLite-backed books with sample income,
expenses, owner draws, learned counterparty rules, and three transactions
waiting for review in a `northstar-demo` company subfolder. It is safe to
delete and recreate.

The agent will route to `/books-onboard` and ask for:

- Legal business name
- Legal structure, such as sole proprietorship, LLC, S corporation, partnership,
  or whatever the owner knows
- Business type, currently `consulting` or `saas`
- What the company does
- Main vendors and contractors
- How you take money out, in plain language; Slashbooks will explain draws vs
  distributions rather than assuming you already know the terms
- The date Slashbooks should start tracking from
- Fiscal year
- Bank accounts and cards
- CSV fallbacks for canceled, unsupported, disconnected, or intentionally
  unconnected accounts
- Commingling rules for personal/business overlap

The setup creates a local company folder with:

```text
entity.json
trust-policy.json
ledger.sqlite
staging/
review-queue/
learned-context/
ingestion/
  quickbooks/
reports/
```

That folder is also where Slashbooks keeps what it learns:

- `business-profile.md` is the human-readable background and accountant context.
- `entity.json` stores structured settings and mappings.
- `trust-policy.json` controls when repeat transactions can be trusted.
- `ledger.sqlite` stores the books, account catalog, and audit history.
- `learned-context/` records usual categories learned from review decisions.
- `review-queue/` holds transactions waiting for owner judgment.

When you need a plain ledger file for inspection or another tool, generate a
Beancount snapshot from the ledger store. The Beancount file is an export, not
the source of account definitions.

If you're starting fresh, setup can stop there. If you're migrating from
QuickBooks, export the needed QuickBooks reports and run `/books-backtest` before
trusting Slashbooks for closes.

## Connect transaction sources

For live bank and card data, set up BankSync and link the business accounts. You
do not need to configure a BankSync feed or destination; Slashbooks reads from
the BankSync API. See [BankSync setup](banksync.md).

If you use Stripe or Mercury, Slashbooks can also pull data directly with your
API key, which is handy for Stripe revenue and Mercury banking. See the provider
download steps in [CSV, QuickBooks, and file imports](imports.md).

For old, unsupported, disconnected, or intentionally unconnected accounts, keep
the original CSV exports in a local folder and map each file once. See
[CSV, QuickBooks, and file imports](imports.md).

## Check your setup

Before your first close, run `/books-checkup` to confirm your setup, account
mappings, and close readiness. It points out anything that needs attention, such
as unmapped accounts, before you rely on the numbers. You can run it anytime you
want to make sure things are still in order.

## During the week

You don't need to work with Slashbooks every day.

Useful light-touch habits:

- Keep receipts and supporting documents wherever you normally store them.
- Avoid mixing personal and business spending when you can.
- Download CSVs before access disappears for canceled accounts, unsupported
  accounts, or accounts you do not want to connect through BankSync.
- Ask `/books-ask` simple questions when you want a quick read on spending or
  revenue.

Example:

```text
/books-ask how much did we spend on software this month?
```

## Monthly close

At the end of each month, open the company directory and run:

```text
/books-close
```

The close should get the month current:

- Pull new BankSync transactions
- Parse any CSVs you provide
- Auto-post transactions from trusted counterparties
- Pause for review when unfamiliar transactions need owner judgment
- Reconcile account balances
- Save a session summary

You usually don't need to think of `/books-review` as a separate step after the
close. The close flow will tell you when review is required and guide you through
it before the books are fully closed.

Use `/books-review` directly when you intentionally want to resume or work
through the queue outside a close session:

```text
/books-review
```

Review is where you confirm or correct queued transactions. Once a counterparty
has been confirmed enough times, Slashbooks can handle similar future transactions
with less interruption.

If something looks wrong, correct it in review instead of editing ledger files by
hand. The agent should translate plain-English corrections into deterministic Slashbooks actions.

## Quarterly or occasional checks

Use `/books-ask` for routine questions:

```text
/books-ask what was Q1 revenue?
/books-ask how much did we pay contractors this quarter?
/books-ask what were owner draws this year?
```

If you're migrating from QuickBooks, or want a confidence check over a
historical period, run:

```text
/books-backtest
```

That compares Slashbooks output against QuickBooks exports and helps surface
category or balance differences before you rely on the new system.

If you don't already have the QuickBooks exports, ask the agent to walk you
through them. It should create and use `ingestion/quickbooks/` inside the company
folder. For a migration/backtest, Slashbooks expects exports for Chart of Accounts,
Trial Balance, Balance Sheet, Profit and Loss, General Ledger, and Transaction
Detail by Account, with cash-basis reports where QuickBooks offers the basis
selector.

## Tax time and accountant handoff

At year end, run:

```text
/books-export
```

The export flow runs sanity checks first. It'll stop or warn if there are open
review items, unreconciled balances, uncategorized transactions, suspicious
personal/business categories, or other issues to resolve before handoff.

When checks pass, it generates accountant-ready outputs under the company
directory, including CSV exports and an optional Excel workbook when the workbook
dependency is installed.

Give your accountant the generated export. Don't share the plugin source repo.

If your accountant wants to understand how Slashbooks produces the numbers, send
them [For accountants](for-accountants.md).

## What not to do

- Do not keep company books inside the `/books` plugin source directory.
- Do not commit ledgers, bank exports, QuickBooks exports, generated reports, API
  keys, or entity directories to a public repo.
- Do not treat Slashbooks as tax, legal, payroll, inventory, or accrual accounting
  advice.
- Do not bypass the review queue for material or unfamiliar transactions.
