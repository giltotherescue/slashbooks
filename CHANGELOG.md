# Changelog

## 0.7.0 — 2026-10-21

- Fix Claude Desktop and Cowork marketplace compatibility by moving the bundled CLI launcher from `bin/books` to `scripts/books`. The Python-installed `books` command and skill-local wrappers keep working. Packaging tests now reject a top-level `bin/` directory.

- Review fixes for imports, tax exports, statements and recovery.
  - Tracking start. One rule for each kind of output. Journal outputs (the general ledger, the statement of cash flows, the accountant package and the tax handoff) never refuse because of the accrual tracking start, whatever the default view and whatever the range. Before the start the journal is the cash books, so a period that ends before the start is a plain cash period and is labelled cash (a prior-year general ledger is always available). View outputs (the profit and loss, the balance sheet, the trial balance and `books ask`) refuse a range or an as-of date before the start in the accrual view, with the coverage error that names the start and the `--basis cash` command; a report with an explicit `--basis accrual` refuses too. In the package, the Cover and the Summary sheet show the same basis text, the Summary says that the part before the start is cash basis, and the accrual sheets start on the tracking start. See `docs/accrual.md`.
  - Structure and compare. `books report pnl --structure` and `--compare` now use the default view when `--basis` is not given, like the plain report. Before, they showed the recorded books with no view label (a cash-default company with accrual records showed -400.00 where the plain report showed 0.00). This was already so on main before this branch. The output names the view.
  - Cash flow. The statement of cash flows starts from the net income of the recorded books, removes noncash asset purchases (equipment bought on account, financed by a loan or contributed by an owner), and lists them under "Noncash investing and financing activities" (`noncash_events` in JSON). A payment of a mixed equipment and expense bill goes to "Needs classification" when a credit or write-off changed the bill.
  - Tax handoff. The Read Me sheet and `README.txt` now show the right basis. A company that tracks accrual for selected accounts only, and has recorded accrual items, gets one label in the Read Me sheet, `README.txt` and the cover of the accountant package: "Cash basis, with accrual tracking for selected accounts from <date>. Book balances as recorded, not a tax cash-basis view; the preparer adjusts." Its `accounting_basis` in `manifest.json` is "accrual" for a period that ends on or after the tracking start. A period that ends before the start is a plain cash period with the plain cash label. Turning tracking on alone changes no export. A tax handoff for a period that crosses the tracking start is not refused. When accrual rules apply to every transaction the basis is accrual whatever the default view is. The Read Me shows a short "Accounting coverage" line (basis, cutover date, summary period, whether historical detail is available). The full `accounting` block (account chart, hashes) is only in `manifest.json`, which also has the new keys `accounting_basis` and `accounting`. A large chart can no longer pass the Excel cell limit. The golden tax files change for these two lines only.
  - Tie-out and balance checks. An opening balance is no longer counted two times for the first statement of an account, so some tie-out results change from `differs` to `matches`. `books balance check` has a new status `not_aligned` (with `reason` `duplicate_account_id` or `balance_dates_not_aligned`) when one provider account appears twice or the balances have different dates. Balance, coverage and readiness checks use a shared lock: they need no write access to the company folder and do not block each other. A damaged ledger file gives the error code `ledger_unreadable` instead of a traceback. The import check uses the posting's own account and the sign of each source amount, so a bank account mapping that changed after a posting no longer hides it, and it follows reversal and correction lineage through every source ID spelling.
  - OFX, QFX and QBO. Every transaction amount and balance inside a bank or card statement must be a finite number with at most two decimal places, or the file is refused with `malformed_ofx` before any row is imported. An empty `AVAILBAL` amount is skipped, and an investment file still returns `unsupported_statement`.
  - Stripe held rows. `stripe_apply` now also returns `unresolved` (every held row with its reason) and `needs_review` (the cleanup rows, each with `source_id`, `reason` and `detail`). A held row has `review_hold_reason: "cleanup_needs_review"` in Review, and confirming it names that reason. A person repeats the apply with the original evidence and policy, or dismisses the row with the new `stripe_dismiss_held(entity, source_id, note=...)` after checking the posted entry. The dismissal refuses a source whose posted entry was reversed, an audit chain that is not intact, and a source that still has an actionable queue item (the error names the item). See `docs/stripe-accounting.md`.
  - Stripe. `stripe_plan` returns a new `source_capture_digests` field. `stripe_apply` stores an immutable finalization receipt with the entries, so a local failure after the write can be finished by repeating the same evidence and policy. If a posted source has no matching receipt, or its evidence changed, only that row is reported as unresolved with the reason `cleanup_needs_review` and stays in Review; the rest of the batch still posts.
  - Accrual. An opening schedule starts recognition on the later of the cutover date and the first service date, and the opening preview applies the same service-date checks as a new schedule. An `equal_month` opening with a mid-month `service_start` was accepted before and is now refused. The message names `actual_day`, the method for service that starts partway through a month.
  - Ask-first limit. Corrections check both the original and the replacement journal, adjustments check the total debits of each entry, and a new or growing managed item or schedule is checked in full. An amount that cannot be read gets the ask-first refusal. See `docs/standing-policy.md`.
  - Known limit. The ask-first limit sizes each entry and each managed record on its own. A limit on the total debits of one policy run is not part of this change, so a large catch-up of many entries that are each below the limit can pass. It needs a run-level total in the policy boundary and is left for a later change.
  - Confirmations. A confirmation that was interrupted before this upgrade and is finished after it does not teach a category (`learn` is stored in the receipt); the entry is still recorded. A new confirmation with `learn` turned on teaches as before.
  - API. `bookkeeping.accrual.journals.journal_totals` is the public helper for debit, credit and difference totals. The private name `_journal_totals` stays as an alias.
- Fix the Vendor 1099 sheet threshold. It used $600 for every year. The federal threshold for Form 1099-NEC and the threshold-based boxes of Form 1099-MISC is $2,000 for payments made after December 31, 2025 (Public Law 119-21, section 70433), and stays $600 for earlier payments. The sheet now compares each vendor's net payments in each calendar year with that year's threshold and flags the vendor if any year meets it; a report that spans two years shows the per-year totals in Review Notes. The amounts are one table in `reports/workbook.py` (`_1099_THRESHOLDS_BY_YEAR`), with IRS sources in a comment. The `vendor_1099_threshold` setting in `entity.json` still overrides every year. The column header now shows the 2026 amount, so the golden export headers changed. The amount may be adjusted for inflation from 2027; the table needs a new row when the IRS publishes it.
- Make a switch of bank data provider safe. A `bank_account_mappings` value can be an object that carries the ledger account and the account identity (`provider`, `provider_account_id`, `institution_id`, `institution_name`, `last4`, `account_type`, `currency`, `cutover_date`); the flat `{provider ID: ledger account}` form is still read and still written when no identity field is given. `books entity bank-account-map` takes the identity as options. New `books bank match-account --entity PATH ... [--json]` proposes the existing ledger account for a new provider account when institution, last four digits, type and currency all match; it never changes a mapping. BankSync `normalize_account` keeps `institutionId` and `institutionName` when BankSync sends them. New feed rows get the source ID `<provider>:<id>` (`banksync:abc`, `plaid:abc`); an old raw BankSync ID and its prefixed form count as the same row in the seen, posted, queued, pending-link and accrual-ownership lookups, so a held payment cannot post twice, and no existing ledger row changes. A host-prepared snapshot keeps its own IDs. A `cutover_date` makes `books ingest` send feed rows dated on or before it to "To check" (matched, or `before_cutover_not_in_books`) instead of posting or dropping them; `--backfill` imports them normally, `--clear-cutover-date` removes the date, and coverage windows start after the cutover. A held row stays held on every later sync under either ID spelling, and a review decision under one spelling is reused for the other. A settlement or schedule funding that names the other spelling of a staged row is refused with `staged_source_id_differs`. `--last4` accepts a mask such as `****0021`. Every connected feed (`--source banksync`, `--source plaid`, or `--feed-provider NAME`) now needs a mapping for each account and is checked against posted entries and queued review items with the statement duplicate plan: same date, amount and description, or the same amount within three days from another source, goes to "To check" and is never posted. A generic `books ingest` keeps its name fallback. One work-event golden changed: the held BankSync duplicate names `banksync:feed-legacy-001`. See `docs/banksync.md` ("Switching bank data providers").
- Agent controls, review fixes. Propose and confirm now enforce the lower of the company and standing-policy ask-first limits inside the trusted boundary; `"ask_first_above": null` means no limit; `stripe_plan` reports `above_ask_first_amount` and `stripe_apply` never records such a row. With `SLASHBOOKS_FORBID_SELF_APPROVAL=1`, a missing `SLASHBOOKS_ACTOR` refuses with `invalid_actor`, and an actor with leading or trailing whitespace is invalid. A `.env` file cannot set `SLASHBOOKS_ACTOR`, `SLASHBOOKS_FORBID_SELF_APPROVAL` or `SLASHBOOKS_ENFORCE_ASK_FIRST`. Work-event `amounts` leave out `amount` for accrual journals and multi-line entries, and an oversized value no longer breaks command capture.
- Work events carry the amounts that the engine recorded. Each `categorized`, `recorded`, `corrected` and `reconciled` event can have an optional `amounts` list of rows with `type`, `id`, `source_id`, `accounts` and Decimal strings (`amount`; for a reconciliation `ledger_balance`, `source_balance` and `discrepancy`). A transfer pair gives one row with its absolute amount and both accounts. At most 200 rows; `amounts_truncated` marks a longer list. `amounts` is not part of the fingerprint, so existing fingerprints do not change, and the schema, validator and golden events are updated. Counts, breakdown, accounts and subjects are unchanged, `summarize` and the text output still show no money. See `docs/work-events.md`.
- Add an ask-first amount: a limit above which a person decides. Set `ask_first_above` in `trust-policy.json` or on the standing policy (`StandingPolicy.ask_first_above`, optional; a policy without it keeps its fingerprint). `queue.eligible_for_autopost` holds a row above the limit even when a learned category matches, so the importer sends it to Review with `review_hold_reason: "above_ask_first_amount"`; a routine plan reports a rule match above the limit as unresolved with the same reason. A row with an unreadable amount is held (fail closed). With `SLASHBOOKS_ENFORCE_ASK_FIRST=1`, or inside the trusted preparation boundary, `queue propose*`, `confirm*` and `correct` refuse an item above the limit (`above_ask_first_amount`, exit code 1, no write). New public functions `queue.amount_for_item`, `ask_first_above`, `amount_is_above_ask_first` and `txn_is_above_ask_first`. Existing outputs are unchanged when no limit is set. See `docs/agent-controls.md`.
- Record who made a suggestion. When `SLASHBOOKS_ACTOR` is set (an opaque string, at most 200 characters), every `queue propose*` command stores it as `created_by` on the queue item. With `SLASHBOOKS_FORBID_SELF_APPROVAL=1`, `queue confirm`, `confirm-group`, `confirm-split`, `confirm-transfer` and `correct` refuse (`self_approval`, exit code 1, no write) when the actor equals `created_by`. Items without a creator are unaffected. New `books queue list --json` prints the full items; the text output is unchanged. See `docs/agent-controls.md`.
- Every report output can be produced on either view, and the default comes from the default view. `books report balance-sheet`, `trial-balance`, `general-ledger`, `books export` and `books compare period` take `--basis cash|accrual` (like `report pnl`). A company that keeps accrual records shows the derived cash view as its cash profit and loss, also when the default view is cash (`books report pnl` and `books export` name the default view; a library call with no view still returns the recorded books, which the cash flow statement needs to tie to the bank); a company with no accrual records has one view, the recorded books. The accrual view of a company that turned accrual on later refuses a period that starts before the tracking start and names that date. Balances have no cash view: a cash request for a balance statement is refused, and a cash export has the P&L and General Ledger sheets only (asking for another sheet by name is refused). Every export cover and every non-default output names its view. `books compare period` on a non-default view reports `view`, compares profit and loss only on the cash view, and does not save dispositions. `--basis` with `--structure` or `--compare` is refused when it names a non-default view. `books ask` reads plain wording ("cash net income", "accrual revenue") for a company that turned accrual on later and holds accrual records or has the accrual default view; answers of existing companies do not change. New `profit_and_loss_by_month` never mixes views silently across the tracking start and says that the cash view is unavailable inside an imported QuickBooks summary period. Migration bundles carry the default view, the capability and the tracking start together and refuse a combination they cannot carry. The tax handoff README no longer calls a mixed company's whole export "Cash basis book balances": once accrual records exist and the period ends on or after the tracking start it says "Cash basis, with accrual tracking for selected accounts from <date>. Book balances as recorded, not a tax cash-basis view; the preparer adjusts." Fix the cash view: the opening of tracked items on the tracking start moves no cash and no longer shows as "Unclassified cash movements". See `docs/accrual.md` ("Every output on either view").
- Turn accrual on inside an existing company and choose the default report view. `books accrual enable preview --entity PATH --tracking-start DATE` and `enable apply --input PLAN` (a person's write; the preview is read-only and an agent cannot apply) upgrade the ledger with a sealed backup, turn the accrual capability on, record `accrual_tracking_start` in `entity.json` and the ledger, and write an audit event and a receipt (`books accrual receipt --id ...`). An exact repeat returns `already_applied`, an interrupted apply is finished by repeating it, and a date in a closed period, a different later date, a stale or edited preview, and a company created on accrual are refused. No entry, report, export, answer or period lock changes and the default view stays cash; only the ledger fingerprint changes, so a readiness package made earlier must be made again. `books accrual opening` now also works inside that company after the step: leave out `lines` and `cutover`, list the open invoices, bills and prepaid or deferred balances, and the engine dates one entry on the tracking start, balances it to `Equity:Opening-Balance-Adjustments` (created if missing), ties every control account to its rows and refuses a closed period. `books entity set-view PATH --view cash|accrual [--apply]` changes only the default view (refused for `accrual` while accrual is off; never changes an entry, lock, file or published statement; audit event `default-view-set`). The accrual view now refuses a period that starts before the tracking start and names the first available date; the cash view never does. `accrual schema` and `accrual capabilities` list the new commands. See `docs/accrual.md` ("Turn on accrual for an existing company") and recipe 19.
- `books backtest run` can write a reproducible accuracy report. Add `--truth PATH` (a CSV with `source_id,account`) and it writes `accuracy-report.json` and `accuracy-report.md` to `--report DIR` (default: `reports/migration-confidence/`). `--seed N` records a synthetic seed and `--json` prints the report. The report has a versioned schema (engine version and commit, seed, thresholds, n, category agreement with a confusion table, coverage, P&L line variance, matched count) and a "what this does not measure" block. A percentage is never printed without its n and the coverage split of compared, pending, missing and not comparable rows. New `compare_categories` joins the truth file to the effective ledger postings and splits auto-posted from pending rows. A seeded synthetic company generator in `tests/fixtures/synthetic/` makes bank rows, truth and a matching QuickBooks export. See `docs/accuracy-report.md`.
- Fix the migration confidence summary: the Blocking Issues list now prints under its own heading. It was nested in the source-coverage exceptions block and printed only when a coverage exception existed.
- Fix the books movement in a statement period: a posting dated on the first day of the period was left out of `period_tieout`. Coverage `tieout` results, the `statement_tieout_differs` readiness warning and statement checks can change for statements with activity on their first day; a previous `differs` for that reason is now `matches`.
- Add `books import check IMPORT_ID --entity PATH [--stored] [--format json|text]`, a read-only check of a statement import against the books. It returns a schema-versioned result with `kind: statement`, an overall `status` (`matches`, `differs`, `partial` or `not_checkable`), and the checks `count`, `debits`, `credits`, `closing` and `opening`, each with `source`, `books`, `difference` and a `detail_code`. The row-count identity is rows in the file = recorded + queued + duplicate + pending skipped. It reuses the tie-out sums, counts queued rows as held, and returns `not_checkable` (never a guess) for foreign currency, files with no balances, an unconfirmed period and overlapping statements (`not_aligned`). Every `books statement import` now runs the check at the end and stores the result in `ingestion/statements/checks/<import_id>.json`; each statement entry has `import_check`. The remote contract classifies the command as a read. See `docs/imports.md`.
- Add a derived cash-basis view for accrual books. `books report pnl --basis cash` shows the profit and loss as if
  income and expense were recognized when money moves: an invoice or bill is income or expense when cash settles it
  (split across its lines, pro rata, with cent-exact residuals), a prepaid or deferred schedule on its funding or
  receipt date, a customer deposit on the receipt date until it is applied to an invoice, and depreciation as
  recorded. Accrued estimates, write-offs and credit offsets have no cash-view effect. Opening receivables and
  payables show on their own lines. Every cash view ends with a tie-out (accrual net income minus cash net income
  equals the reconciling items, to the cent); if it does not tie, the report fails and shows no figures. The text,
  JSON and file output say "Cash basis (derived from accrual books)", and the JSON `meta.method` lists the rules.
  Entries that may be non-cash (a hand-built accrual in an unregistered account) are included as recorded and listed
  under "Entries that may not be cash". The view starts at the cutover. It writes nothing and never changes a statement or a lock.
  `--basis accrual` is the default on accrual books and fails clearly on cash books. `books ask` answers a cash-basis
  question on accrual books from this view instead of refusing it. See `docs/accrual.md` ("Cash view").

- Add core financial statements for hosted and local use; see `docs/statements.md`. `books report pnl` and `books report balance-sheet` take `--structure` (with `--format json`): it adds `schema_version: 2` and a `structure` object with section, account, subtotal and total rows, including Gross Profit when cost-of-sales accounts exist and Total Liabilities and Equity. Default output is unchanged.
- Add `books report cashflow --from DATE --to DATE [--format json|text]`, the statement of cash flows by the indirect method. Accounts are grouped by the account `subtype` into operating, investing and financing; an account with an unclear group goes to a visible "Needs classification" group and is never guessed. A ledger integrity check compares beginning cash plus the net change with the bank and cash accounts (read again from the postings, compared in cents). On a difference the result shows it and the command exits with code 1. An asset account named like cash that is not a bank or cash account gives a warning. Cash means bank and cash accounts; credit card balances are liabilities in operating working capital. A period that starts inside an imported fiscal summary is refused, and an imported opening entry is a starting balance, never a cash flow.
- Add `--compare previous|prior_year` to `books report pnl`, `balance-sheet` and `cashflow`. The engine computes the prior amount, the change, the change percent and (P&L) each line as a percent of total revenue, with `Decimal` math and one rounding rule (two places, half away from zero). A prior period with no data is stated, and its fields are null.
- The remote contract classifies `books report cashflow`, `--compare` and `--structure` as reads.
- `books compare period` adds `summary` (accounts compared, matched and different, overall and per grain, with trial balance totals and `balanced`) and a `matched` list. `books qb import-opening --dry-run` previews the opening postings from export files and writes nothing.
- Add `books qb agreement run --entity PATH [--holdout 3|0.2] [--as-of DATE] [--json]`. It scores Slashbooks's suggested category against the QuickBooks category on a held-out slice of QuickBooks history (the latest three months by default, or a fraction). Counterparty precedents come from the training slice only, so a counterparty seen only in the held-out slice gets no suggestion. The result gives agreement by count and by amount, agreement per account, disagreements by counterparty (`judgment-mapping` or `category_disagreement`), and no-suggestion items counted apart. It posts nothing, never writes learned context, and saves one result file under `reports/quickbooks-agreement/`. The remote contract classifies it as a read command. See `docs/quickbooks-sync.md`.
- Add tie-out proof. Every `books statement import` entry now has a `tieout` (opening balance plus the rows
  against the closing balance, in statement presentation) and `counts.to_import`. `--require-tieout` stops a
  mismatch before any write with `needs_review` and exit code 4. The default stays a warning.
- `books coverage check` adds a `tieout` to each statement and each month: the books against the statement
  closing balance, with `waiting_rows` for staged rows. A month is checked only when one statement is exactly that
  month, otherwise `not_aligned`. Readiness adds the warning `statement_tieout_differs`. It is a warning, not a blocker.
- Add the read-only command `books balance check`. It compares a bank-reported balance with the books balance for
  the mapped account on the same day, and reports `matches`, `waiting_in_review`, `differs`, `bank_newer`,
  `unmapped` or `no_bank_balance`. See `docs/imports.md`.
- A person can teach a category for a payee. The counterparty record keeps a `taught` instruction
  (`set_taught`, `clear_taught`, `taught_for`). A taught category changes what agents suggest. It never
  records, confirms, or changes the trust ramp.
- New read command `queue taught`. `queue propose` and `queue propose-group` refuse a different
  category for a taught payee. A standing routine rule that disagrees with a taught category is held for a person.
- `categorize-record` procedure is version 2. New work event kind `taught`.
- Reserved: an optional `accrual` object on a counterparty record. Only its shape is checked (migration import). It is never written here.
- Save accrual rules. A person's decision about a recurring supplier or customer, or about a kind of cost,
  now lasts. A rule only changes what is proposed: it never records, approves or grants authority. New
  commands: `books accrual rules list`, `set-preview`/`set-apply`, `clear-preview`/`clear-apply` and the
  read-only `match`. A supplier or customer rule is the `accrual` object on its counterparty record (the
  `taught` category is not changed). A company rule, such as "software at 1,000 or more is prepaid",
  is stored in `learned-context/accrual-rules.json`. A supplier rule wins over a company rule, and the most
  specific company rule wins. `match` explains why and returns a draft schedule or item request for the
  normal preview. Rules use the preview, plan hash, receipt and `already_applied` replay of other accrual
  commands. An agent may propose a rule (`source: agent`) but cannot replace or clear one a person set.
  Accrual companies' `queue propose` adds the matching rule to the item's `context` and an `accrual_rule` stamp.
  Remote contract: `rules list`, `match`, `set-preview` and `clear-preview` are reads; `set-apply` and
  `clear-apply` are writes and are not available to unattended preparation. See `docs/accrual.md` and recipe 17.

## 0.6.0 — 2026-10-16

Accrual books arrive as an opt-in. Cash books stay the default and existing cash companies are unchanged. Accrual covers one company, in USD, for services and SaaS businesses.

- Add `books accrual` for accrual companies: unpaid bills and customer invoices, payments matched to what is owed, deferred revenue and prepaid expenses, and straight-line depreciation with an accumulated depreciation account and optional salvage value.
- Spread an amount over dates by exact days, whole calendar months, or a dated list for usage-based or uneven patterns. Service dates include both the first and last day, so a year starting January 15 ends January 14.
- Keep overpayments and deposits as open customer or supplier credits, and apply a credit to an invoice or bill. Split one bill across several expense accounts.
- Accrue an estimate that reverses itself on a date, or keep it open and true it up to the actual bill. Write off a bad debt. End a subscription early and release the unearned balance to a refund, credit or write-off.
- Require supporting detail only for accounts that have items or schedules. Any other asset or liability account is ledger-only and never blocks a close. A plain journal needs a supporting document or a stated estimate basis.
- Start accrual books from scratch, from a cash-basis company (`books accrual opening draft`), or from QuickBooks reports. New accrual companies get a starter chart with receivable, payable, prepaid, equipment, accumulated depreciation, accrued liability, deferred revenue and customer deposit accounts. List accounts with `books accrual accounts`.
- Preview every change before it posts. Re-applying the same preview returns the original receipt (`already_applied`). `books accrual receipt` shows a saved receipt. Reusing one source version with different facts returns `intake_conflict` with the steps to fix it.
- Report receivables and payables aging, outstanding balances, schedules, adjustments, support and reconciliation. State the accounting basis and dates on every report and on every `books ask` answer. Refuse a cash-basis question on accrual books and point to the general ledger for bank movements.
- Make source coverage additive: a new range joins the earlier ones, and a gap blocks only the periods that span it.
- Give readiness blockers and closed-period errors the exact next command. Explain how a person reopens a period, and that an agent cannot. Report a likely sign mistake when a bank or card balance is exactly opposite the ledger.
- Add accountant exports for accrual books: aging, schedules, outstanding balances, support, reconciliation and accrual adjustments. Excel output needs `pip install 'agent-books[xlsx]'`; without it, export writes CSV files.
- Add accrual recipes (`docs/accrual-recipes.md`): a tested request for each common pattern, with its commands and the resulting ledger entries.
- Describe the package and plugins as cash books plus opt-in accrual, and use "supporting document" in place of "evidence" in user-facing text.

Also in this release:

- Add `books queue duplicates --entity PATH --json`, a read command that lists held duplicate candidates without changing them. Each candidate has `source_id`, `match_rule`, `created_at`, `held` and `existing` (`account`, `date`, `amount`, `description`, and `source_id` for the existing row). The remote contract classifies it as a read command.
- Add `account_number`, `return_type` and `ultratax_code` to each account in `books tax map --format json`. The number and code are the recorded values, or `null` when none is recorded.
- Add `row_count` to `books statement inspect` output for CSV, OFX and QFX files.
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
