# Accrual books

Slashbooks supports opt-in accrual bookkeeping for a single USD entity with
simple services or SaaS activity. Cash remains the default. Accrual records
income and expenses when earned or incurred, then records payment separately.
This is ordinary accrual accounting, not governmental modified-accrual accounting.
It is not a complete invoicing, purchasing, payroll, inventory, tax, or complex
revenue-contract system.

The Python engine calculates amounts, dates, allocations, statements, and
reconciliation. An owner, bookkeeper, accountant, or explicitly authorized agent
chooses the supported treatment. No separate accountant approval role is required.
Missing facts remain visible rather than becoming a guessed journal.

## Choose the basis once

For new local books outside the plugin repository:

```sh
books entity init ~/Documents/books/example-services --name "Example Services" --basis accrual --business-type consulting
```

For Cloud, choose Accrual when creating the company and initialize through its
binding with the same basis. Existing cash companies keep their basis. Changing a
report selector or editing metadata does not convert populated books. Source and
destination basis must match for migration. An incompatible basis, future schema,
or unsupported capability fails closed.

Accrual writes require capability version 1 and the upgraded canonical store.
Disabling accrual writes preserves supported read/report/export access. Keep the
upgrade backup and use compatible readers and writers when restoring; a journal
snapshot alone does not contain schedules, items, allocations, or evidence.

## Discover, preview, apply

```sh
books accrual capabilities
books accrual schema
books accrual journal preview --entity . --input context/adjustment.json > context/adjustment-preview.json
books accrual journal apply --entity . --input context/adjustment-preview.json
```

Run these from the company directory or remote binding. Input and output paths
are ordinary binding-relative or absolute paths. `schema` describes evidence and
all request shapes. Open the mapped accounts before the first effective date with
`books entity account-add . --account Expenses:Software --open-date 2026-01-01`.

A journal request, using accounts in that company's catalog, can be:

```json
{
  "id": "service-estimate-july",
  "date": "2026-07-31",
  "description": "July service cost awaiting final provider statement",
  "estimate_basis": "Documented July usage and contracted unit price; replace when the statement arrives.",
  "postings": [
    {"account": "Expenses:Software", "amount": "100.00", "currency": "USD"},
    {"account": "Liabilities:Accrued-Expenses", "amount": "-100.00", "currency": "USD"}
  ]
}
```

Debits are positive; credits negative. Use exact decimal strings or integer
`minor_units`, never floating-point numbers. Use stored `evidence_ids` instead of
an estimate when source evidence is available. An estimate must disclose its basis.

Review dated postings, evidence, warnings, and supporting effects. Apply the
complete unchanged preview. Changed evidence, account mappings, permissions,
period locks, or books require a fresh preview. Retrying the same committed intent
returns its receipt; a new ID is not a safe way to recover an uncertain result.
Uploaded text, proposed treatment, and an `approved` field grant no authority.

## Recognition schedules

Prepaid expenses and deferred revenue retain the original service interval,
recognized amount, remaining amount, evidence, and funding origin. Schedule end
dates are **exclusive**. `actual_day` uses actual days; `equal_month` requires
whole calendar months. The engine allocates exact cents and places the residual
in the final installment. Inspect the preview's installments and rounding rule.

Use `accrual schedule preview` and `apply` with the schema's `schedule` request.
`origin.mode: new` records evidenced funding with `funding_account` and `date`.
`existing` links a posted source, with an explicit `reclass_account` when needed;
it does not record cash again. Migrated schedules are created by opening migration.

```sh
books accrual schedule due-preview --entity . --through 2026-07-31 --operation-id recognize-july > context/recognition-preview.json
books accrual schedule due-apply --entity . --input context/recognition-preview.json
books accrual report --entity . --kind schedules --as-of 2026-07-31
```

Use `schedule change-preview` / `change-apply` for cancellation, supersession, or
refund. Preserve prior recognition and history. A refund distinguishes remaining
principal from amounts already recognized; a new schedule must not reclaim the
same funding or recognition. Missing original dates or recognition detail requires
external support and blocks automatic scheduling.

## Outstanding items and settlement

`accrual item preview` / `apply` carries receivables, payables, accruals, and credits.
A new item names its explicit counter account and evidence; an existing item links
its posted origin. Original and remaining amounts are positive. Credits require
an explicit debit/credit direction. These records support the control account;
they are not a customer invoice or vendor bill application.

Use `item settle-preview` / `settle-apply` to allocate an evidenced signed bank
movement to one or more items. Allocation amounts are positive and cannot exceed
available payment or item balances. Existing movements are linked or reclassified;
new movements post once. Receipts against earned revenue reduce receivables rather
than recording revenue again. Use `item adjust-preview` / `adjust-apply` for an
explicit credit or writeoff with its counter account and evidence.

Choose `settlement_strategy: direct` or `reverse` for an obligation. Do not both
settle and reverse it. Reverse-strategy items use `item reverse-preview` /
`reverse-apply`; journal reversals and corrections retain links to their original
intent. Inspect scheduled journal reversals with:

```sh
books accrual journal due-reversals --entity . --as-of 2026-08-01
books accrual report --entity . --kind outstanding --as-of 2026-07-31
```

Bank import stages accrual activity for treatment; learned cash categories cannot
automatically decide recognition. Link existing obligations or schedules first.
Only an explicitly established new current-period cash event uses
`--accrual-treatment new_cash_event` on ordinary queue confirm, confirm-split,
confirm-group, or correct. Provider text cannot supply that authorization.

## Beyond the built-in recipes

Use generic evidenced journals for externally determined payroll, depreciation,
tax-related adjustments, or other balanced accounting effects. Slashbooks does
not calculate payroll, depreciation, tax, or a complex revenue standard for you.
Keep the provider's calculation and maintain dated supporting balances with
`accrual support preview` / `apply`.

A support request contains `operation_id`, `account`, `as_of`, `currency`,
`evidence_ids`, and `details` rows containing `id`, signed `balance`, and optional
`description`. It has no financial posting. `mode: full` supports the whole control;
`residual` must identify the exact `managed_support_ids` also included. Combined
support must equal the posted control balance. Same-day replacement names the
current snapshot in `supersedes`; old snapshots remain available for historical
as-of reports. Support cannot replace missing financial entries or double-own a
managed balance. A balanced generic journal alone does not resolve missing support.

## Reports, coverage, and close

```sh
books report pnl --entity . --from 2026-07-01 --to 2026-07-31 --format json
books report balance-sheet --entity . --as-of 2026-07-31 --format json
books accrual report --entity . --kind reconciliation --as-of 2026-07-31
books accrual report --entity . --kind adjustments --from 2026-07-01 --to 2026-07-31
books readiness --entity . --period 2026-07 --as-of 2026-08-02
books export --entity . --from 2026-07-01 --to 2026-07-31
```

Reports identify basis, dates, opening summary coverage, and unresolved issues.
Outstanding balances are reconstructed as of the requested date even after later
settlements. Exports retain supporting schedules/items, allocations, adjustments,
evidence links, and control reconciliation. A successful report is not a ready close.

A July 1 cutover can carry January–June as one reconciled summary: July P&L excludes
that summary; January–July P&L includes it exactly once. A range covering only part
of the summarized period is unavailable, not zero. At a fiscal-year boundary, a
verified closed position carries prior earnings in retained equity; the new-year
P&L starts fresh. For a midmonth cutover, a verified reconciled opening can
support readiness for cutover through month end. The package explicitly reports
`activity_from`, `activity_to`, `partial_post_cutover`, and
`imported_summary_through`. Source opening must equal the verified prior-day
ledger balance, and coverage/activity/reconciliation must prove that exact
remaining interval. This does not create unavailable pre-cutover monthly detail.

Readiness also checks due recognition/reversals, unresolved settlements, control
support, source coverage, evidence integrity, and periods. Fix the concrete blocker
or retain a provisional result. Final close/reopen remains a separate authorized
operation. Local operators can use the schema's period preview/apply contract with
an exact readiness package; standing adjustment authority cannot close or reopen.
Cloud uses its existing publication/locking workflow and fresh exact readiness
packages, not generic period commands.

## QuickBooks cutover

Use [the same-basis opening workflow](accrual-intake.md#quickbooks-opening) for a
full mapped trial balance, reconciled balance sheet and fiscal YTD P&L, plus item,
schedule, or external detail. No balancing plug hides missing equity or support.
Full historical reconstruction and cash-to-accrual conversion are separate work;
changing QuickBooks' report toggle is not evidence of the company's accounting policy.
