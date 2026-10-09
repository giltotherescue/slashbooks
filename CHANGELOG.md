# Changelog

All notable changes to Slashbooks are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/), and the project aims to
follow [Semantic Versioning](https://semver.org/).

## [0.4.0] - 2026-10-08

### Added

- One plugin now works with local books or with Slashbooks Cloud. Onboarding
  explains both choices and asks where the books should live. Local books stay
  free, open source and unchanged.
- `books cloud login` connects a company folder to Slashbooks Cloud with
  browser approval. The agent never receives a password or key, and the
  endpoint defaults to `https://slashbooks.co`. Without a company ID, the user
  chooses the company in the browser, and the CLI uses a standard folder for it.
- Ordinary `books` commands and skills run against cloud books when the folder
  is linked. A cloud error never falls back to local books.
- Shared business profile, configuration and notes with revision-checked
  updates, saved receipts and safe retries for uncertain cloud writes.
- `books cloud migration export` creates a verified private snapshot of local
  books, and `books cloud migration import` adopts it into an empty cloud
  company without changing the local books.
- `docs/cloud.md` describes local and cloud books, and
  `docs/hosted-migration.md` describes the move to the cloud.
- The wheel now includes the onboarding templates, so an installed package can
  create a company without the plugin checkout.

### Fixed

- CSV imports keep their confirmed ledger account mapping, and a changed or
  conflicting mapping stops the import instead of posting to the wrong account.
  Bank CSV files are typed as checking accounts, not credit cards.
- BankSync downloads stop with an error on a repeated cursor, conflicting
  page flags or a page or row limit, instead of returning a partial download.
- Adding an account now requires a valid audit chain and records an audit
  event. A currency conflict stops the change.
- Ledger writes that must not interleave now take an immediate SQLite write lock.

## [0.3.2] - 2026-08-24

### Added

- `/books-feedback` drafts a privacy-conscious, implementation-ready handoff
  for Slashbooks developers from bookkeeping feedback.
- QuickBooks account crosswalks let an owner identify presentation-only chart
  hierarchy differences without hiding economic differences.
- Declared source coverage dates now produce named missing-start or missing-end
  exceptions and block migration certification when a known gap overlaps the
  requested period.
- The review queue now has a read-only grouped summary with proposed treatment,
  count, total, date range, and sample counterparties.
- Reviewers can now propose and approve a direct balanced split from one staged
  transaction, including a visible liability effect and exact reusable split
  templates. No interim single-category entry is posted.
- Native transfer pairing now proposes equal-and-opposite staged bank/card rows
  by amount, account, counterparty evidence, and date tolerance. Confirmation
  posts an audited pair with both source records and source-dated clearing legs
  when settlement dates differ. Unmatched transfer-like rows remain named
  timing-or-missing-source exceptions.
- Explicitly approved related-entity policies now record due-from, due-to, and
  direction-specific migration fallbacks. Each related transaction still needs
  its own queue proposal and is never learned as a routine vendor category.

### Changed

- Books workflows continue through clear, safe, deterministic steps and pause
  only at a material decision or approval boundary.

### Fixed

- QuickBooks backtests now compare one effective source entry per transaction,
  so superseded classifications and their reversal entries do not inflate
  transaction counts or unmatched-item reports.
- Internal Mercury cash-bucket transfers now match the equivalent QuickBooks
  bank row only when the date, amount, and mapped cash account agree.
- Transfer and split confirmations recover safely when the ledger commit
  succeeds before review-queue cleanup, without posting duplicate activity.
- Both sides of a transfer remain durable source IDs, even if staging recovery
  rebuilds its seen-ID state.
- Related-entity policies and QuickBooks account crosswalks require explicit
  approval provenance. Stale related-entity proposals stop when the approved
  policy changes.
- Existing source declarations keep their original configuration shape, and
  crosswalks cannot point to accounts outside the local chart.
- Corrupt split-template files now fail closed instead of being overwritten.

## [0.3.1] - 2026-08-23

### Fixed

- Accept current QuickBooks Trial Balance exports with the
  `Account Name,Debit,Credit` header, while retaining compatibility with the
  older `Full name,Debit,Credit` form. Cash-basis evidence using either header
  now passes QuickBooks readiness validation.

## [0.3.0] - 2026-08-23

### Added

- `/books-qbo-fetch`, a browser-independent workflow that collects and validates
  the exact QuickBooks Online report set needed for opening balances, historical
  comparison, migration, backfill validation, or backtesting.
- Signed-in, signed-out, MFA/security-challenge, changed-navigation, virtualized
  report-list, missed-download-event, and manual-download paths for QBO report
  collection across Codex, Claude, agent-browser, Playwright, and equivalent
  browser integrations.

### Changed

- The main books router, QuickBooks onboarding, backtest workflow, README, and user
  guide now direct owners to `/books-qbo-fetch` when QuickBooks source reports are
  missing or incomplete.
- QuickBooks collection now requires explicit dates and cash-basis reports, keeps
  one coherent active evidence set, and uses deterministic inventory validation
  before any downstream import or comparison.

### Fixed

- General Ledger parsing now maps fields by the exported header, including current
  QBO layouts that insert a `Distribution account` column before transaction dates.

## [0.2.0] - 2026-08-22

### Added

- Stable BankSync account-ID mappings, direct download ingestion, source-period
  integrity checks, grouped queue review, and public correction commands.
- Preview-first repair for unsafe legacy QuickBooks opening entries.
- An explicit duplicate-candidate decision command and complete test-suite CI.

### Changed

- QuickBooks openings now carry only Assets and Liabilities, with one
  `Equity:Opening-Balances` offset. Prior-period income, expenses, and individual
  equity balances never enter the new period.
- Accountant exports now fail closed when review work is staged, reconciliation
  evidence is absent or internally inconsistent, or a resolved reconciliation's
  underlying balances change.
- Migration confidence now requires complete reference files, successful
  comparisons, no invalid openings, and no unresolved material differences.

### Fixed

- Prevented prior-year QuickBooks P&L balances from appearing as current-period
  activity at cutover.
- Made opening repair atomic so a failed reversal cannot leave a replacement
  opening partially applied.
- Prevented connected-bank display-name changes and recovered provider IDs from
  silently creating duplicate cash activity.

## [0.1.0] - 2026-06-18

Initial public release of Slashbooks: agent-native, cash-basis bookkeeping that
runs in Claude Cowork, Claude Code, Codex, and other AI agents. The books live in
local plain-text files you own.

### Added

- Company onboarding (`/books-onboard`): business profile, chart of accounts,
  starter files, and per-entity settings, including a configurable operating
  currency and jurisdiction context.
- Bank and card ingestion through BankSync, plus direct provider downloads for
  Stripe and Mercury.
- CSV imports for American Express activity exports, with one-time account
  mapping and boundary handling.
- Transaction review queue (`/books-review`) with a trust model that learns from
  owner confirmations and resets on corrections.
- Monthly close workflow (`/books-close`) that pulls activity, auto-posts trusted
  counterparties, pauses for review, and reconciles balances.
- Setup checkup (`/books-checkup`) for mappings and close readiness.
- Plain-English questions (`/books-ask`) answered from deterministic reports.
- Dashboards and formatted reports (`/books-dashboard`).
- QuickBooks migration and backtesting (`/books-backtest`) against historical
  exports.
- Accountant export (`/books-export`) with sanity checks, CSV files, and an
  optional Excel workbook.
- Double-entry plain-text ledger, audit log, and local-first data ownership.
- Apache-2.0 license, NOTICE, and a trademark policy.
