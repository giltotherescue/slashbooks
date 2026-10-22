# Changelog

## Unreleased

- Onboarding completion record. New `books entity onboarding-complete PATH --via agent|meeting|staff [--on YYYY-MM-DD]` writes `onboarding_completed` (`on`, `via`) to `entity.json` only. It changes no ledger data. A valid first record is kept: a second run reports `already_complete` and writes nothing. `--on` defaults to the machine date (`date.today()`); the engine keeps no company time zone. A date after today (the later of the machine date and the UTC date) is refused. The books-onboard skill now runs it with `--via agent` when onboarding is done, and with `--via meeting` after it applies the facts of a completed setup meeting. A host such as Slashbooks Cloud reads the record to know that a company finished onboarding, by any path. A missing or malformed value means not recorded. Inside Slashbooks Cloud, only the setup meeting, staff and a person's own connected agent can record completion; Slashbooks' own hosted automation cannot. Outside Cloud, `--via agent` works as written. See `docs/agent-controls.md`. No company golden output changed.
- Tax assumptions and an always-on tax method view. The option `--tax-view on|off` of `books entity set-tax-method` is removed. Once a person records a tax method (cash or accrual), `books tax map`, `books tax export`, the workbook and the handoff follow it. With no tax method recorded, every tax output is exactly what it was before. An old `tax_method_view` key in `entity.json` is ignored, never written, and dropped when the method is set. A script that still passes `--tax-view` gets the refusal `tax_view_removed` and nothing changes. A company that recorded a tax method with the view off now gets the tax view. The tax export gets a "Tax Assumptions" sheet (right after the Read Me), `tax_view.assumptions` in the export and the tax map, `tax-assumptions-*.csv` and a short Read Me section. They list the settings in effect, how each item is handled (prepaid expenses, customer deposits, deferred revenue, opening receivables and payables, card charges, bad debts, depreciation, accrued liabilities, inventory, tax year), counts of missing inputs, and what the preparer decides. They apply no tax rule. New flag `method_change_possible`: the tax method is cash and the prior return method is accrual or unknown. It is a flag only and the export is not refused. The "Fixed Assets" header "Placed In Service (Purchase Date)" is now "Purchase Date", and the sheet notes that the placed-in-service date is not recorded. The books-export skill and `docs/tax-handoff.md` are corrected: passing the 12-month test only removes the capitalization requirement, the election out is made on the return, a trust, escrow, card or refundable deposit item needs the preparer's decision, and the de minimis safe harbor needs written procedures and an election.
- Retained earnings in every view. The balance sheet and the trial balance now close earlier fiscal years when a report is made, in every view. Before, only the accrual balance sheet did; the trial balance had no split in any view. The balance sheet shows "Retained earnings from prior fiscal activity" (earlier fiscal years, only when it is not zero) and "Net Income (current period)", which is now the current fiscal year in every view; before, in the cash and recorded views it was all earnings since the books began. The trial balance shows income and expense accounts for the current fiscal year and one computed row "Retained earnings from prior fiscal activity" (`key` `retained_earnings_prior`, `computed` true in `meta.raw_rows`). One shared rule picks the fiscal year: the fiscal start of an imported opening summary, else `fiscal_year_start`. No closing entry is written. Entries, period locks, total assets, total equity and the profit and loss do not change. For a company with more than one fiscal year the trial balance does change: income and expense rows show the current fiscal year, the computed row is added, and total debits and total credits are lower by the same amount (they still agree). A trial balance whose income or expense rows moved has `meta.fiscal_start`, even when the earlier years net to zero. Labels and structure keys are unchanged. A company with activity in one fiscal year gets byte-identical output: the cash-view metadata keys (`fiscal_start`, `cumulative_net_income`, `current_fiscal_net_income`) appear only with the retained line. In text output, the label column widens to the longest label only when the retained line is shown, so the amounts stay in one column. The accountant package uses `meta.cumulative_net_income` for its net income tie-out when present. `books compare period` now compares the retained line with the QuickBooks Retained Earnings row (plus the account it maps to) and the current year with QuickBooks Net Income, instead of listing them as mapping differences, but only when the books cover the year: Net Income when the books start on or before the current fiscal year start, Retained Earnings when the books start on the first day of a fiscal year and do not start with a cash QuickBooks opening (that opening carries every earning before the cutover in `Equity:Opening-Balances`; an accrual opening carries it in its retained earnings account). A company that started its books inside a fiscal year keeps those lines as mapping differences, as before, because its earlier earnings are in an opening equity account. The cash trial balance comparison (total debits and total credits) now uses the fiscal-year totals, the same convention as the QuickBooks trial balance; and the accrual trial balance comparison adds the computed row to the account that the QuickBooks Retained Earnings row maps to; the `qb_match` test balance sheet was corrected to agree with its own trial balance. Migration bundle report fingerprints keep the earlier layout, so bundles made by an earlier version still validate. The tax handoff is unchanged. No company golden output changed. A host that stored a published statement keeps it; a host that re-renders a month of a company with more than one fiscal year shows the new equity lines. See `docs/statements.md`.
- `books accrual enable` now opens the accrual starter accounts that a new accrual company gets (receivable, payable, prepaid, deferred revenue, customer deposits, accrued liabilities, equipment, accumulated depreciation and depreciation), dated the tracking start, in the same ledger transaction as the tracking start. Before, a cash company that turned accrual on had no receivable or payable account, so the opening items had no account to use. The preview lists the accounts in `effects.accounts_to_open` (covered by `plan_hash`), and the receipt and the `accrual-enabled` audit event list them in `accounts_opened`. An existing account with the same name is left alone. An account that clashes (other letter case, the same name under another account type, or another currency) stops the preview with `starter_account_clash`. `entries_changed` stays 0 and no cash-view report changes; the new accounts have no postings and appear only in account lists (the routine category list and `books tax map`, with 0.00). A company that turned accrual on before this change gets the missing accounts from its next `books accrual opening` preview and apply (`accounts_to_open` in the opening plan). The human messages of `accrual enable` now say "start date"; the JSON field names and codes are unchanged. No company golden output changed.
- Prepaid tax treatment: the engine no longer applies the 12-month rule. The tax outputs use only a `tax_treatment` that a person recorded with `books accrual schedule tax-treatment`. With no recorded value, the cash view for tax keeps the prepaid on the balance sheet as the books record it (`prepaid_capitalized` with `treatment_source` `not_recorded`) and the flag `prepaid_tax_treatment_not_recorded` lists the schedule. The flag `prepaid_expensed_by_default` is removed. The books-export skill now tells the agent how to propose a value under the 12-month rule (Treas. Reg. 1.263(a)-4(f)) for the preparer to confirm. Only the tax outputs of a company with the tax-method view on change, and the readiness note text for a cash-method filer. The shared `accrual schema` golden changed only in the `tax_treatment` description.
- Balance sheet and trial balance of a cash-default company that keeps accrual records now print "Accounting basis: as recorded (cash view default)" and add `meta.basis_label`; `meta.basis` stays `cash`. The workbook cover says "As recorded (cash view default)" for such a company with accrual records from the first day. Every other company keeps its output.
- Docs: `docs/cloud.md` describes turning accrual on later, the default view and the inferred company currency for cloud companies.
- Docs: the README, guides, skills and the public site now describe what Slashbooks does today: one ledger with a default report view, turning accrual on later for a cash company, one company currency (USD by default, no conversion), the tax method view and tax facts, and which settings an agent previews and a person applies (new "Settings a person applies" section in `docs/agent-controls.md`). The site copy no longer says USD only. A note in recipe 19 that said accrual records need the accrual default view is removed: the view never gates a write.
- Company currency. Each company keeps its books in one base currency, not only USD. Slashbooks does not convert currencies, keep exchange rates or compute exchange gains. `entity.json` gets `currency` (ISO 4217; absent means USD, so every existing company and every golden output is unchanged) and `currency_source` (`default`, `inferred` or `set`). New `books entity set-currency PATH --currency CODE [--apply] [--plan-hash H]` (preview first, audit event `currency-set`, person-only, allowed only while the company has no entries; it moves the account catalog to the new currency). New read-only `books entity infer-currency --input FILE [--input FILE ...]` prints the proposed currency with its evidence. The first import of a company with no entries that names a currency sets it (`currency_source: "inferred"`, audit event `currency-inferred`): OFX `CURDEF`, a bank CSV currency code, symbol or column, the Stripe balance currency, the QuickBooks home currency and BankSync row currencies. An import with more than one currency, or another currency than a company with entries or a person-set currency, stops with `unsupported_currency` and names both currencies. Every hard-coded USD check in imports, the review queue, Stripe, QuickBooks, accrual intake, journals, schedules, items, opening, migration and support, cash schedules, corrections and the migration bundle now uses the company currency. Report text and `books ask` print money with the company symbol (`€1,234.50`, or `CHF 1,234.50` when no symbol is defined). Tax outputs stamp `currency` and add the flag `tax_mapping_is_us` when the company currency is not USD; nothing is blocked. A recorded USD gives the same outputs as an absent key; `set-currency USD` records the person's choice so a later feed in another currency is refused. Only the write step adopts a currency: the importer in the same ledger transaction as the postings, a statement file once under the write lock (a file with no row to import is refused with `nothing_to_adopt`), and the QuickBooks opening apply in the same transaction as the opening entry; previews, sync-import and the Stripe plan only check. For a company with entries, a Stripe balance transaction in another currency stays unresolved and the other rows are staged. The ledger writer refuses a posting in another currency than the company currency. A currency change commits the ledger first; a read uses the ledger currency in memory, and the next write repairs entity.json. QuickBooks snapshot evidence does not count as an entry. QuickBooks and accrual opening report amounts accept only the company currency's own sign. A hosted capture whose entity.json is older than the ledger currency reports `company_settings_out_of_date`. This version supports two-decimal currencies only: JPY, KRW, KWD and other zero- or three-decimal currencies are refused with `unsupported_currency_decimals`. `books accrual schema` and `books accrual capabilities` accept an optional `--entity` that shows the company currency; without it their output is unchanged. A bank CSV with a plain `$` in a non-dollar company is refused. See `docs/company-currency.md`.
- Add an audited fiscal-year setting; use it for prepaid tax defaults and annual earnings. Preserve mixed prepaid assets and guard tax-policy writes against invalid ledger integrity and standing-authority changes.
- Tax method view: record facts for the tax preparer and list missing inputs. New `books entity set-tax-profile PATH [--prior-return-method cash|accrual|unknown|none] [--entity-type sole_proprietor|single_member_llc|partnership|s_corp|c_corp|nonprofit|other|unknown] [--apply] [--plan-hash H]` (audit event `tax-profile-set`). New `books accrual schedule deposit-kind` and `books accrual item deposit-kind --entity PATH --id ID --deposit-kind advance_payment|refundable [--apply] [--plan-hash H]` (audit event `deposit-kind-set`). In the cash view for tax, a deposit recorded as refundable stays a liability until it is applied or forfeited; unset means advance payment, income on receipt. The tax export adds `tax_view.tax_profile`, `tax_view.deposits` (with "assumed advance payment" when no kind is recorded), `tax_view.fixed_assets` (purchase date, cost, account, vendor, book depreciation; no tax depreciation), the "Fixed Assets" and "Deposits" sheets, and the flags `deposit_kind_not_recorded`, `deposit_refundable` and `prepaid_tax_treatment_not_recorded`. Readiness adds the note `deposit_kind_missing`. All writes are person-only. The engine applies no tax rule to these facts; the agent checklist is in the books-export skill. Without the tax-method view every output is unchanged.

## 0.7.0 — 2026-10-21

- Fix Claude Desktop and Cowork marketplace compatibility by moving the bundled CLI launcher from `bin/books` to `scripts/books`. The Python-installed `books` command and skill-local wrappers keep working. Packaging tests now reject a top-level `bin/` directory.

- Tax outputs can follow the company's tax method. New `books entity set-tax-method PATH --method cash|accrual [--tax-view on|off] [--apply] [--plan-hash H]` records `tax_method` and `tax_method_view` in `entity.json` (preview first, audit event `tax-method-set`, person-only). When `tax_method_view` is true, `books tax map`, `books tax export`, the tax Read Me, `README.txt`, `manifest.json` and the tax workbook use the cash view derived from the accrual records for a cash-method filer, and the accrual view for an accrual-method filer. They stamp `tax_method` and `view`, the Basis line names the method, and `tax_view.flags` lists depreciation not tax-adjusted, deferred revenue on receipt, bad debt not deductible, accrued liabilities excluded and the difference from the accrual view. The view never falls back: a range that the view cannot cover is refused with the reason. Prepaid schedules accept `tax_treatment` (`expense_when_paid` or `capitalize`; new `books accrual schedule tax-treatment`), with a 12-month-rule default when it is absent. Readiness adds the note `schedule_tax_treatment_missing` for a cash-method filer. Without the flag, every output is unchanged: no company golden output changed. The shared `accrual schema` and `accrual capabilities` goldens list the new field and command. The defaults need review by a tax professional. See `docs/tax-handoff.md` ("Tax method view").
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
