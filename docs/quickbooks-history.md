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


## Bank rows that clear late

A deposit in transit or an outstanding check can reach the bank days or weeks after
QuickBooks recorded it. Each QuickBooks bank or card leg stays *unmatched* until a
person matches a bank row to it with `resolve-buffer --decision history-match`
(a durable decision). A bank or card row on a mapped account is held for a person
when:

- it is dated up to the history end (`inside_period`), or
- it is dated after the history end, up to `HISTORY_BANK_LAG_DAYS` (45) days after it,
  and an unmatched leg on the same account has exactly the same signed amount and is
  dated from 45 days before the row to 3 days after it (`unmatched_history_leg`), or
- it is dated within three days after the history end (`settlement_window`), because a
  split or combined settlement can have a different amount.

Any other row after the history end follows the normal path at once. A row held for
`unmatched_history_leg` or `inside_period`, or with a journal on the account within
three days, is never released by time. `books qb buffer-decisions` shows for each
held row `hold_rule`, `history_matches` (journals within three days, any amount) and
`candidate_legs`: the unmatched legs of the same amount and direction, each with
`source_id`, `account`, `date`, `amount`, `type` (the QuickBooks transaction type),
`doc_number`, `name` and `days_before_row`. A `history-match` decision must name one
of the `candidate_legs`, so its account, amount and direction are checked, and a leg
matches one row only. A released row has the status `released-to-intake`: it goes
through normal intake, which can still hold it for categorization.

`books qb release-buffer` reads decisions and ledger entries once for the whole call,
releases the due rows in one intake pass, and keeps going when a row fails. Its result
is `{"released": [...], "needs_review": [...], "errors": [{"source_id": ..., "error": ...}]}`.

Known limits: a bank row that is only part of a QuickBooks leg (a split or combined
settlement) cannot be matched, because a match needs an equal amount; release it
with `--decision release` only after checking. Reports for ranges before the opening
date show the prior year as one lump. A history entry has no correction or reversal
path. One history import per company.


## Matched rows, copies, undo and rows before the history

**A matched bank row stands in the duplicate pool.** When a person matches a bank row to a
QuickBooks leg, that bank row (account, signed amount, date, description, source ID) is kept as
handled. A later row on the same account that the ordinary duplicate rule treats as the same bank
row, from another source, is its recognized copy and is not posted and not held again: the same date,
amount and description, or the same amount within three days (a statement for a feed row, or the
reverse). Intake counts it in `matched_to_history` (and in `skipped_duplicate`); a statement import
returns `matched_to_history` for the statement and a `matched_to_history` count. Each item has
`source_id`, `duplicate_of` (the matched row), `account`, `date` and `amount`. Only one copy from each
other source is recognized; see "A real second row that looks like a matched one" below.

**Undo.** `books qb reverse-buffer --entity BOOKS --source-id ID [--reason TEXT] --json` reverses a
buffer decision. It writes a durable audit event (`qbo-buffer-decision-reversed`). Reversing a
`history-match` makes the leg unmatched and the row held again. Reversing a `release` is refused
while the row is posted or waits in the review queue: reverse the posted entry the normal way (or
withdraw the queued item) first. The engine does not reverse a posted entry here, so the ledger never
changes behind the books' back. The command is a write that the preparation integration does not
list, so an agent cannot call it; a host must keep it person-only.

**A released row** ("This is new") does not match any leg. It is not offered as a candidate again,
and the next row of the same amount is held while the leg is unmatched.

**Direct ledger writes.** A person's journal is the person's own act and is not blocked. A bank row
reaches the ledger only through intake (feeds, statements, `ingest`, context release and the buffer
release, all `import_transactions`) or through a queue that intake filled (confirm, split, transfer,
Stripe clearing). A test lists every direct writer so a new one is reviewed.

**Before the history starts.** A bank row dated before `history_from` is before the books: the opening
balance already includes it. Feed rows are reported in `before_opening` (not posted, not held), and a
statement that has such a row follows the ordinary `before_opening_balance` refusal.

Known limit: an amount that differs from the QuickBooks leg (a tip, a netted fee) is not held after
the first three days.


## A real second row that looks like a matched one

A copy is recognized only once for each other source. From how the ordinary duplicate rule pairs
rows, a matched bank row has at most **one** recognized copy from **each other source**: a feed row has
one statement copy, a statement row has one feed copy, and each copy is kept durably with the matched
row, its source, date, amount and description (`qbo-history-copy` audit events). The same source ID
is always the same row, so a re-sent copy stays recognized.

A row that pairs with a matched row but is not its one copy from that source is **not** skipped:

- a second row from a source that already has its copy (a second statement row of the same amount and day), or
- a row from the same source as the matched row under a **new source ID** (the bank re-sent the row, or a
  second real row).

It is held for a person with `hold_rule: "possible_second_row"` and never releases by time. The person
answers with `resolve-buffer --decision release` ("This is new: add it": the row posts through intake
exactly once, even inside the history period) or `--decision copy [--original-id ID]` ("Already in my
QuickBooks history": the row is kept as a recognized copy). The accepted cost: a bank that re-sends the
same row under a new ID on the same feed now needs one answer for that row.

`buffer-decisions` returns, for such a row, `hold_rule: "possible_second_row"` and `matched_original`:
`source_id`, `account`, `date`, `amount`, `description`, `history_source_id` and `leg` (the QuickBooks
journal: `source_id`, `account`, `date`, `amount`, `type`, `doc_number`, `name`). `candidate_legs` lists
other unmatched legs of the same amount. The intake result has `possible_second_rows` (each with
`source_id`, `matched_original`, `account`, `date`, `amount`, `description`) and the held row counts in
`duplicate_candidates`; `matched_to_history` lists only the recognized copies. A statement import returns
the held row in `held` with `match_rule: "quickbooks_history_possible_second_row"`.

Reversing the match of a bank row (`reverse-buffer`) ends its copies: every recorded copy and every
`copy` decision for it is held for a person again (`reopened_rows`), and a statement sent again is held.

A statement that has rows before the history start is refused with the true reason: the starting
balance comes from the QuickBooks history, so upload a statement that starts on or after the start date.
A statement opening cannot replace it (`record-opening --replace` is refused with
`opening_exists_other_source`).
