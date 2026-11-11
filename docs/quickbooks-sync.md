# QuickBooks Online reference sync

A QuickBooks Online connection can download an initial history and repeat that
sync into Slashbooks. The Python engine accepts provider snapshots; the caller
owns OAuth, pagination, background scheduling, token storage, and disconnect.
These commands do not make network requests or write back to QuickBooks.

## Import a snapshot

```sh
books qb sync-import snapshot.json --entity /path/to/company
```

The version 1 JSON snapshot contains `schema_version: 1`, `provider:
"quickbooks"`, a numeric string `realm_id`, timezone-aware `fetched_at`, ISO
`from`/`to` dates, `accounting_basis` (`Cash` or `Accrual`), and these collections:

- `company_info` and `preferences`: original CompanyInfo and Preferences objects.
- `accounts`, `customers`, `vendors`: original entity arrays, with provider IDs.
- `entities`: transaction type to original transaction array. Purchases, deposits,
  transfers, invoices, bills, payments, bill payments, sales receipts, credit
  memos, refund receipts, vendor credits and journal entries are retained.
- `reports`: original API report objects, with `Header`, `Columns`, and `Rows`.
  Supported keys are `ProfitAndLoss`, `BalanceSheet`, `TrialBalance`,
  `GeneralLedger`, optional `TransactionListByAccount`, `OpeningTrialBalance`,
  and `OpeningBalanceSheet`. Opening reports must end the day before `from`.

Use a home currency that is the company currency and explicit boolean
`CurrencyPrefs.MultiCurrencyEnabled` from Preferences. A company with no entries
takes the QuickBooks home currency (see [company currency](company-currency.md)).
Evidence from other currencies can be saved, but cannot be applied as opening
balances or certified by the existing cash-basis comparisons.

The result is JSON. `success` means the source evidence was saved, **not** that
accounting data was posted or reconciliation passed. `coverage` marks every
required reference report ready, blocked, or missing. `reference_complete` is
false if requested reports are missing or unsafe. Source completeness is separate
from accounting operation support: `opening_supported` and `comparison_supported`
require cash-basis, single-currency books in the company currency. Accrual reports are still retained as
source evidence when structurally valid.
No journal entries are inferred from transaction objects.

`accounts` contains the reviewed-source account catalog, preserving IDs, full
names, hierarchy, active status, account types, and deterministic proposed
Slashbooks account names. Sync never overwrites the local chart of accounts.
Accounts with unknown types, broken hierarchy, duplicate names/IDs, or names
that collide after normalization require correction before import.

Source evidence and typed report references are immutable and content addressed
under `sources/quickbooks/<sha256>/`. `reference_path` is relative to the company
directory so it survives backups and restore. Repeating the same snapshot is
idempotent; changing source data produces new evidence. Corrupted existing
reference files prevent reuse and opening application.

### When saved evidence cannot be used

`sync-import` and `sync-opening` compare the saved evidence of a snapshot with
the evidence that the current engine makes from the same source. When they
differ, the command stops, prints `{"success": false, "error": "...", "code":
"..."}` and exits with 1. Use `code`, not the message text, to choose what to
do:

| `code` | Reason | What to do |
|---|---|---|
| `snapshot_format_outdated` | The saved source (`snapshot.json`) is intact. An earlier engine wrote the files derived from it in an older format: chart accounts without account numbers. Nothing else differs. | Fetch a new snapshot. Its `fetched_at` differs, so it is saved under a new ID. |
| `snapshot_evidence_mismatch` | The saved evidence differs in another way: a digest collision, a changed, missing, extra or damaged file, or a symbolic link. The message stays "Snapshot evidence digest collision or corruption". | Do not use the saved evidence. Find out what changed the folder before you import again. |
| `snapshot_evidence_concurrent` | Another import saved different evidence under the same ID at the same time. | Run the import again after the other import ends. |

Other errors print `success` and `error` only, as before.

## Review financial kinds with an opening chart

To include reviewed financial kinds, set `"version": 2` in the
`qb sync-opening --chart-choice` JSON file. The preview uses QuickBooks account
types for bank and credit card accounts and the Cash on Hand detail type for
cash. When a source type is ambiguous, add a `financial_kinds` object keyed by
QuickBooks source account ID, with one reviewed kind for each reported account.
Review each kind and ledger target before applying the same choice with its
preview hash. The chart, kinds and opening entry commit together. The default
version 1 chart choice keeps its original behavior.

The `sync-import` result tells you which accounts need a review before you
build the chart choice. Each active Assets or Liabilities account in `accounts`
has a `financial_kind` object. Other accounts do not have it.

- `{"required": true, "kind": null, "allowed": ["bank", "cash", "other"]}`: the
  source type does not decide the kind. Add one of the `allowed` kinds for this
  account ID to `financial_kinds`. `allowed` lists only the kinds that the chart
  accepts for the account type: `bank`, `cash` or `other` for an asset, and
  `credit_card` or `other` for a liability.
- `{"required": false, "kind": "bank", "allowed": ["bank"]}`: the source type
  decides the kind (here `bank`). Do not add this account to `financial_kinds`.

The preview uses the same rule. The `financial_kind` object is in the command
result only. The saved evidence (`manifest.json` and
`reference/accounts.qbo.json` under `sources/quickbooks/<sha256>/`) does not
have it, so the evidence of a snapshot that you imported before stays the same.

When a version 2 chart choice has no reviewed kind for one or more of these
accounts, the preview stops. In Python, `quickbooks_sync.chart_manifest` (and so
`sync_opening`) raises `FinancialKindReviewRequired`, a `ValueError`.
Its `account_ids` attribute lists every such account ID in source account order,
not only the first one. Read `account_ids`. Do not parse the message text, which
is for people and can change.

## Compare and backtest

The `.qbo.json` reference files are read directly into the existing canonical
report models. No CSV export is manufactured. Pass the reference directory to
ordinary comparison commands:

```sh
books compare period --entity /path/to/company \
  --qb-folder /path/to/company/sources/quickbooks/DIGEST/reference \
  --from 2026-01-01 --to 2026-01-31
```

Existing readiness checks still require a complete report collection, including
transaction detail, prior-period balances and end-period balances. When General
Ledger provides every canonical detail field and valid account context, the
importer creates a lossless transaction-detail view from that same report. Its
coverage explicitly records `derived_from: GeneralLedger`; it is not an
independent source. `comparison_ready` describes the existing inventory checks
and supported currency/basis. Unsupported
or truncated report layouts are not replaced with guessed totals. General
Ledger normalizes supported date, type, amount and account columns; unfamiliar
columns or missing account detail block that comparison grain. The initial
snapshot importer does not promise complete inventory, payroll, multicurrency,
or every possible transaction-detail layout.

`books compare period` saves its differences, with any decisions on them, in
`reports/migration-confidence/differences.json`. Add `--no-save-differences` to
compare without a write. The command still reads the saved decisions, and its
output is the same.

`books compare period` reads each QuickBooks export file of the reference
folder one time into a private snapshot, and compares only that snapshot. It
returns `reference_digest`, the SHA-256 of the name and the SHA-256 of each
snapshot file, so the digest describes exactly the bytes that were compared.
The folder path is not part of the digest. Paths in the output and the errors
name the original folder. The command removes the snapshot in every path, and
it never changes the original files. If a reference file cannot be read, the
command compares the folder itself and returns no `reference_digest`.

`books compare period` also returns `summary` (`accounts_compared`, `matched`, `differs`, and the same three counts per grain `pnl`, `bs` and `tb`) and a `matched` list of rows with no difference. The cash-basis `tb` grain compares two totals, so it is counted only under `grains.tb`, which adds the reference `total_debits`, `total_credits` and `balanced`.

Importing QuickBooks classifications and reproducing its totals tests reference
fidelity. Independent categorization backtesting requires separate transaction
inputs and a held-out period. Never score historical rule-training examples as
independent predictions.

To score categories in a backtest against a ground-truth file, and to write a
report that others can re-run, see [accuracy-report.md](accuracy-report.md).

### Review a difference (typed request)

`books compare accept-diff --key K` marks a saved difference as accepted. It
binds no amounts, no period and no reviewer, and it keeps its released
behavior. A host that must replay a person's review uses the typed command:

```sh
books compare review-diff --entity /path/to/company \
  --qb-folder /path/to/reference --request review.json
```

The request is version 1. Every field except `reviewed_at` is required, and an
unknown field is refused. Take `period`, `reference_digest` and `difference`
from the read-only compare result (`books compare period --json
--no-save-differences`) that the person reviewed:

```json
{
  "schema_version": 1,
  "period": {"from": "2026-01-01", "to": "2026-03-31"},
  "reference_digest": "sha256:<64 lowercase hex characters>",
  "difference": {"grain": "pnl", "account": "Income:Consulting-Revenue",
                 "ours": "10500.00", "reference": "10000.00",
                 "delta": "500.00", "category": "uncategorized"},
  "reviewer": "person-17",
  "explanation": "The extra payment is a deposit that QuickBooks did not record.",
  "reviewed_at": "2026-11-09T15:04:05Z"
}
```

- Amounts are finite decimal strings, such as `"-12.50"`. They are compared by
  value, so `"500"` and `"500.00"` are the same amount. A JSON number, `NaN`,
  `Infinity` and an exponent are refused.
- `grain`, `account` and `reviewer` have no control character. `reviewer` is
  one line with no leading or trailing space (at most 200 characters).
- `explanation` is not blank (at most 2000 characters). Its only permitted
  control characters are a line break and a tab.
- `reviewed_at` is optional. It is `null` or a time with a zone. The engine
  reads no clock and sets no reviewer. It stores only the values that you send.

The command checks the request before it does anything that can write. An
invalid request takes no lock and changes no file. Then, under the company
write lock (without the `entity.json` currency repair), the command does these
steps:

1. It reads `differences.json`. A missing file is an empty list. No earlier
   `books compare period` is necessary. A file that exists must be a list in
   the exact form that the engine writes, and every row must have the exact
   fields and types of a difference row. Otherwise the command refuses with
   `DIFFERENCES_FILE_CORRUPT`. It never reads such a file as empty, and it
   never drops a field that it does not know.
2. It reads each QuickBooks export file of the reference folder one time into
   a private snapshot, and it computes `reference_digest` from exactly those
   bytes. If the digest is not the request's `reference_digest`, it refuses
   with `REFERENCE_CHANGED`.
3. It compares the period again, read-only, on the snapshot only. It does not
   read the reference folder again, so the digest in the record always
   describes the bytes that were compared. It finds the difference by grain
   and account, and it accepts only when `ours`, `reference`, `delta` and
   `category` are the same as the current difference. It removes the snapshot
   in every path, and it never changes the original files.
4. It reads `differences.json` again. If the bytes changed during the compare,
   it refuses with `DIFFERENCES_FILE_CHANGED` and writes nothing.
5. It writes the file one time (see "The write" below).

Every other writer of `differences.json` (`books compare period` when it saves,
`categorize-diff` and `accept-diff`) also takes the company write lock. Like
the typed review, they do not run the `entity.json` currency repair, as in the
released engine. The read-only compare takes no write lock and writes nothing.

#### The write

The command writes one file,
`reports/migration-confidence/differences.json`, and changes one row in it. It
creates the `reports/migration-confidence` folder when it is missing. The new
file is exactly `json.dumps(rows, indent=2) + "\n"`:

- `rows` is the saved list (an empty list when there was no file).
- When the list has the selected key, the command replaces that row in place.
- When the list does not have the key, the command appends the row at the end.
- Every other row keeps its bytes. The command does not write the other
  current differences that the compare found.

The written row is the row that a saving compare writes for that difference,
with these changes: `status` is `accepted`, `re_opened` is `false`, `note` is
the explanation, `review` is added, and `review_drift` is `[]`. `review` has
`schema_version`, `period`, `reference_digest`, `ours`, `reference`, `delta`,
`category`, `reviewer`, `explanation` and `reviewed_at`. `reference_digest` is
the digest of the compared snapshot, which is the request's digest. The same
inputs (the request, the books, the reference files and the saved file) give
the same bytes. A verifier can replay the write from these inputs.

The lock file `.books.lock` in the company directory is created when it is
missing, as for every engine write. A company that the engine has written to
already has it.

#### The result

The command prints a JSON result and exits 0 when the difference is accepted:

```json
{"schema_version": 1, "status": "accepted", "key": "pnl:Income:Consulting-Revenue",
 "record": {"...": "the written row"}, "review": {"...": "record.review"},
 "review_drift": [], "reference_digest": "sha256:..."}
```

It exits 1 for a refusal, and a refusal writes nothing:
`{"schema_version": 1, "status": "refused", "code": ..., "message": ...}`.
Read `code`, not the message text.

| Code | Meaning |
| --- | --- |
| `REQUEST_INVALID` | The request does not have the version 1 shape, or the file is not JSON. |
| `ENTITY_NOT_FOUND` | The company directory has no `entity.json`. |
| `REFERENCE_FOLDER_MISSING` | The QuickBooks reference folder does not exist. |
| `DIFFERENCES_FILE_CORRUPT` | `differences.json` exists, but it is not a valid list in the exact form that the engine writes. |
| `COMPARE_INCOMPLETE` | The difference was not found, and the compare reported `errors`. |
| `DIFFERENCE_NOT_FOUND` | The compare of the period has no difference with this grain and account. |
| `DIFFERENCE_CHANGED` | An amount or the category is different. `changed_fields`, `current` and `reference_digest` show the current difference. |
| `ALREADY_ACCEPTED` | A different review of the same difference is accepted and current. `accepted_review` shows it. |
| `DIFFERENCES_FILE_CHANGED` | Another write changed `differences.json` during the review. Try again. |
| `REFERENCE_CHANGED` | The reference files are not the files of the request's `reference_digest`. `reference_digest` shows the current digest. Compare again and review the new result. |
| `REFERENCE_UNAVAILABLE` | A reference file could not be read, for example because it disappeared during the snapshot. Compare again. |

An exact retry with the same request returns the same result and writes
nothing. A different reviewer, explanation or time for a review that is still
current is refused with `ALREADY_ACCEPTED`. A new review is allowed when the
difference is open, has only an `accept-diff` acceptance, or has an earlier
review that is out of date.

#### A current review and an out-of-date review

Each `books compare period` keeps `review` on the row and sets `review_drift`
to the reviewed fields that differ from the current difference, in this order:
`period`, `reference_digest`, `ours`, `reference`, `delta`, `category`. A
review is current only when `review_drift` is `[]`.

- `period` is in the list when the compared period is not the reviewed period.
- `reference_digest` is in the list when the reference files are not the
  reviewed files, also when the selected amounts did not change.
- A change that is not material keeps the status `accepted`, but
  `review_drift` is not empty, and the text output adds "changed since review".
- A material change re-opens the difference. For a reviewed difference, the
  material-change check uses the reviewed delta, not the last saved delta, so
  small changes add up.
- `books compare categorize-diff` also updates `review_drift`.

Rows without a typed review keep their released shape, with no `review` or
`review_drift`.

## Category agreement on held-out history

```sh
books qb agreement run --entity /path/to/company [--holdout 3|0.2] [--as-of 2026-06-30] [--json]
```

This answers: "Slashbooks agrees with QuickBooks on X percent of transactions."
It is a separate command, not part of `backtest run`. `backtest run` compares
statements for a period. This command scores one category per transaction.

QuickBooks history stays reference evidence. The command reads the saved sync
snapshot, never posts to the ledger, and never writes `learned-context`. It
writes one result file under `reports/quickbooks-agreement/<snapshot>/` and
prints it.

- Scored: purchases and bills with one account-based line account. Splits,
  item-based lines and other transaction types are listed in
  `excluded_from_scoring`.
- Holdout: `--holdout 3` (default) holds out the as-of month and the two months
  before it, by date. `--holdout 0.2` or `20%` holds out a stable sample chosen
  by a hash of the transaction reference, so a repeat run holds out the same
  transactions. `--as-of` defaults to the latest QuickBooks transaction date and
  drops later transactions. An empty held-out slice, or no history, is refused.
- Leak guard: counterparty precedents come from the training slice only, and the
  two slices never share a transaction. A counterparty seen only in the held-out
  slice gets no suggestion.
- Suggestion: the same order a real suggestion uses. A taught category wins,
  then a learned-context category that is not reset, then the training
  precedent (the most common category; a tie gives none). `suggestion_sources`
  counts each. A taught or learned category is the company's own work, not
  QuickBooks history.
- Comparison: the QuickBooks category is the snapshot account mapping, or an
  approved `compare account-crosswalk` entry. No suggestion is counted apart and
  is never a disagreement. `agreement_rate` is agree over suggested. The amount
  rate weights each transaction by its absolute amount.
- Disagreements are grouped by counterparty and category pair, with `count`,
  `total` and `sample_ids`. A group is `judgment-mapping` when the QuickBooks
  account has no counterpart in the local chart, or Slashbooks suggested an
  account QuickBooks never used. Any other group is `category_disagreement`.

The result is deterministic for the same snapshot, holdout, crosswalk and
learned context.

## Review opening balances

```sh
books qb sync-opening snapshot.json --entity /path/to/company --cutover 2026-01-01
books qb sync-opening snapshot.json --entity /path/to/company --cutover 2026-01-01 --apply
```

The first command saves the source evidence and previews the exact balanced
opening lines after running the same accounting constructor and validator used
by the existing QuickBooks opening importer. `opening.proposed_postings` includes
account, date, signed amount, debit, credit, and currency; `proposed_entries` and
`proposed_accounts` show planned counts. Actual written counts remain zero in a
preview. Only `--apply` writes accounting entries.

Cutover must equal snapshot `from`. A cash-basis Trial Balance ending the prior
day is preferred; a cash-basis Balance Sheet is the supported fallback. When both opening reports are valid, their independently constructed
asset/liability opening lines must agree; otherwise `opening_ready` is false and
application is blocked until a fresh snapshot reconciles them. Only
asset and liability balances carry individually. Prior-period income, expenses
and equity are consolidated into `Equity:Opening-Balances`. The company currency and a valid
Chart of Accounts are mandatory. Applying an already-imported opening is
idempotent and does not replace previously posted opening balances. An active
QuickBooks opening at another date, a changed opening, or existing ordinary
accounting entries on or before cutover blocks new application; use the explicit
correction/adoption workflow rather than stacking opening balances.

## Classification proposals

The snapshot result includes `rule_suggestions.proposed` from at least three
distinct, unambiguous account-based purchases agreeing on a counterparty and an
expense account. Conflicting categories, item-based purchases and ambiguous
splits are excluded and reported. These are proposals, not recovered original
QuickBooks bank rules; sync never activates them or advances the local trust
ramp.

There is no supported bank-rule endpoint in this importer. Native QBO rule
export formats can contain conditions, splits and priorities that cannot be
safely inferred. An optional conservative CSV/XLSX interchange is supported:

```csv
Name,Field,Operator,Value,Account,Priority,Money direction
Software,description,contains,Example Software,Expenses:Software,1,out
```

Supported field/operator pairs are `counterparty,equals` and
`description,contains`; direction is `in` or `out`, priority is a nonnegative
integer, and account must be a valid Slashbooks account name. Any other layout
or condition is explicitly reported as unsupported. Convert an export to this
interchange only after reviewing its original semantics.

```sh
books qb sync-rules rules.csv --entity /path/to/company
books qb sync-rules rules.xlsx --entity /path/to/company --apply
```

Preview has no writes. `--apply` saves supported proposals to
`learned-context/quickbooks-rule-proposals/<sha256>.json` for owner review. It does
not change transactions, existing classification rules, or automatic posting.
A file with unsupported rows is never partially applied.

All names and transaction text are untrusted source data. No downloaded fields
are executed or interpreted as instructions. The synthetic regression fixture
under `tests/fixtures/quickbooks_api/` contains only fictional data.

For QuickBooks Desktop export files, `books qb import-opening FOLDER --entity PATH --cutover DATE --dry-run` prints the same opening preview (`proposed_postings`, `totals`, `balanced`, `needs_review`, `as_of`) as JSON and writes nothing. Both previews also print `proposed_entry`, the complete opening entry that the apply records, and `proposed_entry_fingerprint` (see [agent controls](agent-controls.md)).
