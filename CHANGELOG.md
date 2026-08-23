# Changelog

All notable changes to Slashbooks are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/), and the project aims to
follow [Semantic Versioning](https://semver.org/).

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
