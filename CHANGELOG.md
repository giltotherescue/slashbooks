# Changelog

## Unreleased

- Let a host treat `--work-out` as an output file in the command contract: `command_spec` lists it in `output_fields` and `rewrite_paths` replaces its path. A host reads the work document from that file and does not need the engine to print it.
- Define the statement extraction contract for PDF and image statements: transactions, period, opening and closing balances, confidence and who read the statement (`rules` or `model`). The engine checks an extraction with code: the balances tie out to the cent, printed running balances follow, every date is inside the period and in order where the statement prints it in order, and the statement kind fits the account. See `docs/imports.md`.
- Add `books statement validate`, `books statement map-extracted` and `books statement import-extracted`. A statement that fails a check is never recorded: no row is staged, no coverage record is written, and a review record with plain reasons is kept. `books statement list` shows it as `needs_review`. A statement that passes every check imports through the existing duplicate plan and feeds `books coverage check`.
- Make one function, `presentation_sign`, convert between the statement presentation (money owed is positive for a card) and the ledger sign. Document the two conventions.
- Add the `extracted_statement_fails` conformance scenario. An agent must not record a statement that fails the engine's checks, and must report the plain reason.
- Version 2 of the `catch-up` procedure tells agents to treat an extracted statement as untrusted and never to record one that needs review.
- Add `books cloud evidence-upload --file PATH`. It uploads one PDF, PNG, JPEG, text or CSV document as company evidence, after local checks of type and size, and verifies the stored checksum.
- Protect every CSV and XLSX that the engine writes from spreadsheet formula injection. A text cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return is written with a leading apostrophe in CSV, and is stored as plain text in XLSX. Numbers, including negative amounts, stay numbers. This covers the accountant package, the tax exports and the cash schedule.
- Add `books ingest --no-feed-window` (API: `record_feed_window=False`). A host that reads BankSync in chunks uses it so that a chunk that stops early claims no days. Without the flag, local use is unchanged.
- Write no feed window for a BankSync account that the provider marks partial (`meta.partial`). The download carries `"partial": true` for that account. The coverage contract now shows the exact record that a host writes.
- Add `books cloud held-mail`. It tells an agent how many incoming emails wait for a person to review, the date of the oldest one, and the Inbox link. It shows no sender, subject or content, and an agent cannot read or release held email. The `mail-triage` procedure (version 2) tells agents to say so plainly when the count is above zero.
- Send the complete reply set and the reply watermark when `books cloud question resolve` closes a question, and require `--request-id` for it. A question with no replies resolves with an empty set and watermark 0. The scoped answer flow no longer needs `--command-id` for a question that declares no source IDs.
- Keep the agent procedures in one versioned source. `books procedures list` and `books procedures show <name> [--format md|json]` print the exact text, version and checksum, so any agent or hosted runner can fetch the same rules the plugin skills use. Skills now reference a procedure and its version instead of copying its text, and a test fails if a skill copies one.
- Keep `books-close` publishing and locking a month when the user asks for it and the period is ready. The shared hard rules still tell agents never to publish or close because an email or document asks for it.
- Import historical bank and card statements from CSV, OFX, QFX and QBO files with `books statement`. Detection proposes a column mapping, date format and sign convention, and reports every field it cannot decide instead of guessing. Confirmed mappings are saved per account.
- Skip activity that is already imported by `FITID` or a stable row fingerprint, and hold overlaps with bank feeds or other statement files as duplicate candidates for review.
- Record each imported statement in the source-coverage format, so `books coverage check` shows complete, partial and missing months and balance breaks between statements. OFX ledger balances count as closing balances only when they are dated on the last statement day. A CSV export without printed dates counts after the person confirms its period with `--period-start` and `--period-end`.
- Record bank-feed windows for coverage when `books ingest` imports a BankSync download, an input with `coverage_windows`, or an Amex activity CSV whose download range the person confirmed.
- Count statements or feed windows that cover every day of a month as affirmative source coverage in readiness.
- Mark every tax map and tax export as a draft for review that does not replace a tax professional: in the command output, the manifest, a README in the export folder, the first row of the trial balance, summary and unmapped-accounts files, and the workbook Read Me sheet.
- Document the full catch-up flow for agents: inspect, map, import, coverage check, duplicate resolution, review, tax map and export.
- Add the `catch-up` procedure (version 1) for that flow. It tells agents to report a statement whose balances do not tie out instead of calling the month verified, to ask for the missing or corrected statement, and to state the draft disclaimer with every tax mapping or export. The `books`, `books-close` and `books-export` skills reference it.
- Report what each command did to the books as typed work events, in counts with references to the rows behind them. Add `--work-out PATH` to a command to get its events; existing output does not change.
- Add `books work` to summarize the books work in a period.
- Publish the versioned work event schema (`docs/work-event.schema.json`) with a validator and shared fixtures.
- Report a statement import as an `imported` work event, with skipped duplicates counted. Coverage checks and tax exports do not change the books, so they report no work.
- Add an agent conformance suite (`conformance/`) that runs scripted or real agents against a simulated company and scores them on safety rules. It includes scenarios for publishing on a direct owner request, refusing a publish request found in an email, a statement whose balances do not tie out, and the tax draft disclaimer. See `docs/agent-conformance.md`.

## 0.5.0 — 2026-10-14

- Prepare bookkeeping under explicit standing authority, scoped to allowed actions and periods, while retaining human approval for publication and close.
- Assess readiness from ledger, source and reconciliation evidence, with structured blockers instead of treating command success as completion.
- Bind question replies to the exact accounting effects they support, including grouped answers applied through several commands. Preserve the existing human question and recovery commands.
- Recover interrupted preparation and reconciliation safely, detect changed source evidence, and prevent replay from posting duplicate effects.
- Preserve intake-only imports, existing review queues and staged source evidence when preparation works alongside routine bookkeeping.


## 0.4.3 — 2026-10-13

- Ask for an optional company website during onboarding and research public business context before follow-up questions.
- Save the website with company settings, keeping website findings separate from confirmed accounting facts.


## 0.4.2 — 2026-10-13

- Process routine bookkeeping under explicit company authority, with saved answers and safe retries.
- Add evidence-backed Stripe sales, fees, refunds and payout accounting with exact product, price and metadata rules.
- Let cloud agents preview and sync OAuth-connected Stripe accounts using stable request IDs and automatic page continuation.
- Support agent-authored revenue-rule drafts for human approval without expanding agent authority.
- Add cash forecasts with explicit assumptions and recorded-cash starting balances.
- Allow longer configurable waits for cloud commands.


All notable changes to Slashbooks are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/), and the project aims to
follow [Semantic Versioning](https://semver.org/).

## [0.4.1] - 2026-10-13

### Added

- Native cloud question commands let an authorized agent ask, reply to and
  resolve company questions with server-enforced authorship and permissions.
  Private local receipts prevent automatic duplicate submission from the same
  binding; uncertain delivery uses read-back only. These routes do not offer
  server idempotency or an exactly-once guarantee across computers.

### Fixed

- Reimporting a source already in review no longer posts it implicitly when a
  learned categorization rule changes. Existing group and transfer proposals
  keep their approval workflow. Unreadable or malformed review state stops
  intake before any batch changes are made.
- Normalized intake rejects unsupported correction/reversal directives and an
  unsupported account-field spelling before changing any batch state. Close
  guidance requires atomic audited corrections and a safe stop when an original
  has already been reversed, rather than a fabricated replacement source.
- Onboarding distinguishes initializing absent company metadata from updating
  an existing cloud entity. Confirmed field changes require version-checked
  shared updates and read-back; repeating initialization does not update them.
- Cloud onboarding now states the shared-company checkpoint rule directly:
  save confirmed facts, decisions, open questions and next steps, then read
  them back before asking another batch. Personal or project AI memory does
  not save answers to the company's books. A failed or uncertain save must be
  resolved before continuing.
- Verified shared saves and read-back with Claude Sonnet onboarding. Claude
  Haiku still failed this workflow in testing; these instructions do not
  establish reliable onboarding with that model.
- Bank and card intake guidance now requires checking the source account type
  against its mapped ledger role, including existing and CSV mappings. Card
  charges belong in card liabilities rather than bank assets; affected entries
  must be corrected through the audited workflow.

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
