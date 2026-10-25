# How to do your books with Slashbooks

This guide is for a founder, owner, or operator who wants to use Slashbooks for a
simple cash-basis company or single-entity services/SaaS accrual books.
Cash is the default. Accrual separates income and expenses from their payments;
it also tracks what customers owe, unpaid costs, and amounts paid in advance. A
cash company can turn accrual on later from a date and keep its cash history.
Each company keeps its books in one currency, USD by default; see
[company currency](company-currency.md).
The [accrual task guide](accrual.md) explains what to ask your agent and what the
result should show. Developers can use the [supporting document reference](accrual-intake.md), and
[accrual recipes](accrual-recipes.md) has a tested request for each pattern.

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
- Legal structure, such as sole proprietor/sole trader, LLC, corporation,
  limited company/Ltd, partnership, LLP, nonprofit/charity, trust, or whatever
  the owner knows
- Business type starter catalog, currently `consulting`, `saas`, or
  `services_and_subscriptions`
- What the company does
- Main vendors and contractors
- How you take money out, in plain language; Slashbooks will explain draws vs
  distributions rather than assuming you already know the terms
- The date Slashbooks should start tracking from
- Fiscal year
- Whether the existing books use cash or accrual accounting; for new books, the
  agent explains the choice before setup
- The currency the books are kept in, if it is not USD; the agent can usually
  read it from the first bank or QuickBooks file
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
API key, which retains Stripe source records and Mercury bank activity.
The packaged Stripe revenue recipe is cash-only; accrual uses generic supporting
document intake, recognition, and settlement treatment. See the provider
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
- In cash books, record eligible transactions from trusted counterparties
- In accrual books, match payments to existing bills or customer balances, record
  the month's share of prepaid costs and customer prepayments, and review other
  adjustments supported by documents
- Pause for review when unfamiliar transactions need owner judgment
- Reconcile account balances
- Save a session summary

For accrual books, you can supply an unpaid contractor bill, an annual software
agreement, or the payroll provider's report and ask the agent to include it in
the right month. The agent should explain what changes and whether the related
payment is already recorded. It should ask for missing facts, such as service
dates or which bill a payment covers, rather than ask you for internal IDs or
accounting input files.

A useful close update is specific: “July is prepared except for the payroll
provider's supporting report. The software allocation and customer receipts are
recorded.” Preparing the month is separate from locking or publishing it; those
actions use your company's established permissions.

You usually don't need to think of `/books-review` as a separate step after the
close. The close flow will tell you when review is required and guide you through
it before the books are fully closed.

Use `/books-review` directly when you intentionally want to resume or work
through the queue outside a close session:

```text
/books-review
```

Review is where you confirm or correct transactions that need judgment. In cash
books, repeated confirmations can reduce interruptions for similar transactions.
In accrual books, the agent still checks whether a payment belongs to an earlier
bill, a customer balance, a prepaid service, or a new expense.

If something looks wrong, correct it in review instead of editing ledger files by
hand. The agent should translate plain-English corrections into deterministic Slashbooks actions.

## Quarterly or occasional checks

Use `/books-ask` for routine questions:

```text
/books-ask what was Q1 revenue?
/books-ask how much did we pay contractors this quarter?
/books-ask what were owner draws this year?
```

For accrual books, also ask:

```text
/books-ask which customers still owe us as of July 31?
/books-ask which unpaid balances are overdue?
/books-ask how much software cost remains prepaid?
/books-ask what needs attention before July can close?
```

The agent should distinguish expenses incurred from cash paid. Overdue amounts
require actual due dates; an unknown date stays unknown. Reports also explain
which historical periods are available, especially after moving existing books
from another system.

If you're migrating from QuickBooks, or want a confidence check over a
historical period, run:

```text
/books-backtest
```

That compares Slashbooks output against QuickBooks exports and helps surface
category or balance differences before you rely on the new system.

If you don't already have the QuickBooks exports, ask the agent to walk you
through them with `/books-qbo-fetch`. It should use `ingestion/quickbooks/` inside
the company folder, collect the exact periods needed for opening balances and the
comparison, and validate the set before `/books-backtest` continues.

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
- Do not treat Slashbooks as tax, legal, payroll-calculation, inventory-costing, or complex revenue-recognition
  advice.
- Do not bypass the review queue for material or unfamiliar transactions.


For a large review history, read bounded JSON pages with
`books queue list --entity PATH --json --offset 0 --limit 25`. The response
includes `total_count` and `next_offset`; continue until `next_offset` is null.
The same options work with `books queue duplicates --json`. Keep the company
unchanged during a scan, or start again. These reads never post or change queue
status. Without page options, the existing full result is unchanged.
