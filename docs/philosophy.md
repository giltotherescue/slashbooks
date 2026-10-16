# Accounting philosophy

Slashbooks is built around a simple idea: a small business should get faster,
better, and cheaper books from an AI agent it controls, instead of paying for
bloated accounting software and an outsourced bookkeeper.

The agent handles the repetitive work and keeps the books current, while the
owner makes the judgment calls. The books also stay local and private: your
company folder holds the ledger, review queue, audit log, source documents, and
reports, so you can back them up, inspect them, or stop using the tool without
asking a vendor to release your own data.

## Not bloated, not outsourced

Traditional accounting apps feel bloated because they have to serve every kind
of business at once. You end up paying, in clutter and complexity, for features
that have nothing to do with how you operate. Slashbooks instead gives you a
focused foundation shaped to your business and leaves the rest out. When you
need a particular report, chart, or export, you ask the agent and it produces it
on the spot, with no clicking around an app hunting for the right option.

An outsourced bookkeeper has a different problem: they don't know your business
as well as you do, the back-and-forth is slow, and the reports tend to lag the
month they cover. Slashbooks works the other way around. The agent sees all of
your accounts together and recognizes common vendors, so it connects income and
expenses without the constant questions. You can run the monthly close on a
schedule and have current books waiting, and at any time you can ask for an
analysis of anything in the business, with every figure already at hand.

## Cash-basis first

Cash is the default for small owner-operated businesses. Simple single-entity USD
services and SaaS companies can explicitly choose [accrual books](accrual.md),
with recognition, payment, and supporting detail kept separate. The basis is a
company policy, not a report toggle. For the cash workflow:

- Bank and card activity drive the books.
- Owner review handles judgment calls.
- Slashbooks generates reports from a local ledger store.
- Accountants can still review the output before tax filing.

The supported foundation does not calculate payroll, depreciation, tax, inventory
costing, or complex revenue contracts. Externally determined journals and exact
support remain available without expanding those calculations.

## Double-entry underneath

Even though the owner works in plain business language, Slashbooks keeps the ledger
as double-entry accounting. Every posted transaction affects at least two
accounts: money moved from somewhere and went somewhere. That's what lets the
system check whether the books balance instead of treating categories as loose
tags on a spreadsheet.

The ledger is kept in an inspectable SQLite store with double-entry postings,
audit events, and supporting records. Plain-text snapshots and portable bundles
provide inspection and transfer formats. The owner
never has to read or write that format day to day.

## The agent is not the accounting engine

The agent is the interface. It asks questions, explains differences, proposes
categories, and helps the owner work through the close.

The numbers come from deterministic software, not the AI. The software imports
transactions, writes the ledger entries, validates balances, reconciles accounts,
and generates the reports. The AI never invents totals or does the math itself.

## Canonical books and portable records

Local canonical books live in SQLite with retained source evidence. Remote books
use the selected server workspace as their authority. A plain-text snapshot
contains journal entries; a complete portable bundle also preserves supporting
records, evidence, coverage, and period state.
You don't need to open them day to day, but they're there whenever you
or an accountant need to audit exactly what happened, line by line.

## Review before trust

Slashbooks starts conservative. Unknown counterparties and ambiguous transactions
go to the review queue. The owner confirms or corrects them in plain English.

For cash books, repeated confirmations teach Slashbooks how you categorize a given
counterparty. Once a counterparty is trusted, similar future transactions can be
posted with less interruption. A
correction resets trust for that pattern, so the system becomes cautious again
when the owner says it was wrong.

The monthly close is not complete while material review items are still open.
Review is part of the close, not an afterthought.

## Backtest before migration trust

If a company is migrating from QuickBooks, Slashbooks should earn trust before it
replaces an existing workflow. The backtest compares Slashbooks output against
QuickBooks exports over a historical period, surfaces material differences,
and asks the owner to accept or resolve them.

That doesn't make Slashbooks magically correct. It gives you and your accountant
evidence about where it matches, where it differs, and which differences are
judgment calls.

## Local ownership

The default posture is ownership and control:

- Your books live in a folder you choose, on your own machine.
- Your company's books are kept separate from the tool itself.
- Generated accountant exports are files you can hand off yourself.

Slashbooks isn't a privacy guarantee: the AI agent, your bank connection, and
the other tools you set up still see the data they need to do a task. But the
durable accounting system is local files you own, not a hosted ledger you can
only reach through someone else's product.
