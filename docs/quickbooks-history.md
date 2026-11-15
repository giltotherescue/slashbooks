# Native QuickBooks history (staged evidence)

`books qb sync-transactions MANIFEST --entity BOOKS` previews an optional native
ledger import. `--apply --plan-hash HASH` records the reviewed plan. Preview
returns `success`, `ready`, `blockers`, per-account `controls`, counts,
`open_date_changes` and a hash. Apply rechecks the ledger and every evidence hash.
The operation performs no network requests. The caller must collect complete,
immutable raw provider evidence before invoking it.

The manifest uses `format: slashbooks-qbo-history-v1`, `environment` (`sandbox` or
`production`), numeric-string `realm_id`, inclusive ISO `history_from` and
`history_to`, `accounting_basis` (`Cash` or `Accrual`), `currency: USD`, a complete
one-to-one `account_map` from source IDs to engine accounts, and `cutovers` mapping
every Bank/Credit Card target to `history_to`. An empty account map requests the
engine's deterministic complete source-chart mapping; empty/omitted cutovers derive
from the source Bank/Card identities and the selected end date. The preview
returns the resolved mapping and cutovers for owner review. Source and target financial roots
must agree. Other collection metadata, including `job_id`, is accepted. Optional
`expected_source_counts` freezes the per-type counts selected by the owner. Each
expected count must match the collected unique source records (`Account` counts
the full chart); differences produce `source_count_changed` blockers before any
recording. The reviewed hash and audit receipt bind those expectations.

`files` lists relative `path`, SHA-256 `sha256`, and `kind`. Each file is bounded
to eight MiB; finalization is bounded to 64 MiB total and currently reads those
bounded files in memory. Symbolic links and paths outside the manifest directory
are refused. Supported kinds:

- `accounts`: raw `QueryResponse.Account` pages covering the complete source chart.
- `entities`: raw `QueryResponse.<entity_type>` pages, with `entity_type` set.
  Customer, Vendor, Item, Class and Department pages are retained as versioned
  reference evidence; they never qualify as GL transaction identities or create
  operational objects. Empty reference pages are valid.
- `general_ledger`: raw GeneralLedger report, with `account_id`, `from`, `to`.
  Partitions must cover every account and every day exactly once.
- `opening_trial_balance`: raw TrialBalance at the day before history starts.
- `closing_trial_balance`, `balance_sheet`: raw reports at history end.
- `profit_and_loss`: raw report over the exact history period.

All reports require explicit matching basis, dates and USD currency. GL requires
column metadata `ColKey` date (`tx_date`), type (`txn_type` or `tx_type`), debit
(`debit`, `debit_amt` or `debt_amt`) and credit (`credit` or `credit_amt`). A
transaction type cell must retain its stable source ID; account-section headers
must retain source account IDs. Account-filtering alone does not substitute for
missing account identity. Signed Amount reports are unsupported. Missing columns,
unknown shapes, report truncation, gaps, overlaps, malformed values and unknown
source IDs refuse finalization. Report monetary cells require exact decimal
strings or integers; floating-point JSON values are refused before posting. Beginning balances and report totals never become
activity. Display labels such as Sales Receipt and Bill Payment (Check) normalize
to the corresponding source entity identity. Source dimensions remain attributed
to each posting as the raw row, without interpretation.

The supported transaction evidence entities are Purchase, Deposit, Transfer, JournalEntry,
Invoice, Payment, Bill, BillPayment, SalesReceipt, RefundReceipt, CreditMemo,
VendorCredit and CreditCardPayment. Their documents establish stable identities
and versions, never inferred accounting legs. Each GL group must balance across
all account partitions. Cash reports supply cash effects; unpaid documents with
no report effect remain evidence only. Accrual history does not create operational
open items or settlement allocations. Cross-basis derived reports are outside
this migration's guarantee.

Opening TrialBalance produces one balanced journal on H-1, including every
nonzero account. There is no additional opening on history end. Per-account
opening plus GL movement must equal ending TrialBalance. Income/expense period
movement must equal ProfitAndLoss. Ending balance-sheet accounts must equal
BalanceSheet. Its explicit derived Net Income leaf must equal the closing
TrialBalance's net income, and the complete balance sheet must balance.

QuickBooks closes each fiscal year into Retained Earnings when it makes a
report, so a trial balance dated on a year end still holds that year's income
and expense and a later one does not. The engine follows its own year-end rule:
it writes no closing entry, the opening posts the trial balance as it is, and a
statement closes earlier years into retained earnings when it is made. The
checks expect the same roll. The fiscal year start is the company
`fiscal_year_start` setting (`MM-01`, default `01-01`) and must match the
QuickBooks company. Income and expense that the opening holds, and movement
dated before the fiscal year of the history end, are expected in the one
Retained Earnings account (sub type `RetainedEarnings` or named `Retained
Earnings`). The preview shows the amount as `closed_into_retained_earnings`.
When a year is closed and there is not exactly one such account, it blocks
with `retained_earnings_account_unclear`. A wrong fiscal year setting does not
tie and blocks with `control_mismatch`. A file named in the manifest that is
missing gives an `evidence_file_missing` blocker.

Only empty mapped accounts qualify in v1. Existing entries, openings or pending
review work block import; the engine does not replace or reverse them. Empty
accounts may have their opening dates moved to H-1 as an explicit part of the
reviewed plan. Entries, date changes, audit receipt and coverage ownership commit
atomically. Exact source replays are idempotent even under a new collection job, and
even when running account balances or customer, vendor and item records have
changed since (they never post);
changed source versions, voids or deletes require an explicit correction workflow.
A void first encountered without any GL effect remains versioned source evidence
with `no_source_report_effect`; it does not create a journal entry.
Native source IDs include environment, realm, source type and source ID.

Ownership reserves all mapped accounts through history end even when activity
and balances are zero. Feed and statement rows in that period wait in review.
For conservative settlement protection, every Bank/Credit Card row through three
days after history end also waits; this can hold legitimate distinct rows. A
shared final write guard prevents later categorization from bypassing the hold.
Normal opening workflows cannot stack another opening on the owned balances.
The three calendar days are a conservative settlement-date buffer, not a
provider guarantee or a measured upper bound. It includes T+1, T+2 and T+3;
T+4 is outside this extra buffer. The cutoff compares the transaction date,
not elapsed wall-clock time, so waiting does not release an existing hold.

Owners can inspect held source ID, account, transaction date, amount,
description and match rule through the duplicate review queue. Confirming a
row as duplicate closes its review item without posting. Confirming it as
distinct refuses with an explicit migration-correction error and leaves the
item visible. There is no supported correction/release API in this patch for
posting a legitimate distinct owned row. Automatic coalescing/replacement and
that correction workflow remain outside v1; this is an acceptance limitation,
not evidence that an owner can complete every held-row review.

Tests use synthetic QBO-shaped reports and source entities, with actual native
Cash and Accrual reports checked after posting. They do not establish that every
live QBO configuration returns the required columns and identities. A provider
that omits them is visibly unsupported; there is no guessed fallback.


## Bank intake buffer decisions

Transaction dates through the inclusive bank cutover plus three calendar days
are eligible for ownership holds. Each row waits 72 elapsed hours from its first
persisted `created_at`; reimport does not restart that timer. Historical rows
before the cutover use the same decision workflow. This elapsed-time policy is
separate from transaction-date eligibility.

`books qb buffer-decisions --entity BOOKS --json` lists open rows with
`held_for_quickbooks_history`, `hold_until`, and `history_matches` (journal source
ID, account, date, amount and description). Possible matching requires a journal touching the same bank account within
three transaction days, regardless of amount. Suggestions show its actual signed
bank posting total; split and aggregate settlements can differ from the bank row. These are suggestions,
not automatic equivalence findings.

`books qb resolve-buffer --entity BOOKS --source-id ID --decision history-match
--history-source-id JOURNAL_SOURCE_ID --json` preserves the selected actual history
journal identity and suppresses this incoming source ID on future imports.
`--decision release` records a source/account/date/amount-scoped permission and
runs the retained normalized transaction through ordinary intake. Categorization,
accrual treatment and other safeguards still apply; pending work appears in its
normal review queue. A released bank leg cannot authorize another owned bank leg.
Retries preserve one receipt and produce at most one source entry.

`books qb release-buffer --entity BOOKS --json` releases rows with no suggested
history match after their deadline, but only rows outside the history period
(the three settlement days after it, or dates before it). A row inside the
period always waits for a person, because QuickBooks owns the books there and
the row may be a gap in QuickBooks. Rows with plausible matches remain in review.
The host must schedule this operation; the engine performs no background work.
Ordinary duplicate review cannot dismiss these rows: use the buffer commands.
Decisions are durable hash-chained ledger audit events, with retained input and
selected history journal source ID. File display status follows the receipt and
normal intake, so interrupted releases can be retried safely.
