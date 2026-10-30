# Commercial records

The `business` commands maintain USD customers, vendors, invoices, bills and
actual payment facts. They use the same ledger and supporting item allocations
as the accounting commands. They never connect to an invoice provider, file a tax
return or deliver a recipient statement.

## Command contract

```
books business schema
books business records --entity PATH --resource parties|documents|payments|tax_preparations [--id ID] [--party-id ID] [--as-of YYYY-MM-DD]
books business preview --entity PATH --input FILE
books business apply --entity PATH --input FILE
books business receipt --entity PATH --id OPERATION
books business import-preview --entity PATH --file CSV --operation-id ID
books business tax-preview --input FILE
books business tax-export --input FILE --output DIR
books business tax-recipient --input FILE --id PARTY --output PDF
```

`preview` accepts a strict action union, with `operation_id` on every request.
`apply` receives the entire unchanged preview. Both are network-free. Only apply
changes canonical company state. Exports write requested artifacts and do not
change books or imply filing/delivery. A host must deny `tax-recipient` through
its generic agent bridge and expose it only through a protected, explicit human
export operation. Its input contains a frozen preparation and a transient
`protected_profile`; never log or persist this input in ordinary command history.

The public remote command contract refuses this privileged command. A local
person may invoke it directly using a protected input file. Tax preparation and
masked CPA export take the pure module input/result described in
[tax1099.md](tax1099.md).

Read output is `{schema_version:1,resource,records:[...],ledger_digest}`. Each
record has immutable `id` and integer `revision`. Edits require the exact
`expected_revision`; creation requires zero. Historical accounting labels stay
unchanged when a stable party is associated later. Missing commercial tracking
returns an empty read result with `tracking_required:true`.

## Start tracking

Preview `{operation_id,action:"tracking.enable",tracking_start:"YYYY-MM-DD"}`.
An authorized person confirms the tracking start, which retains the existing
historical/opening protections. Setup preserves the default report basis, all
historical entries and period locks. Cash companies can issue invoices and track
bills without changing their cash default. The app can call this “Start invoices
and bills”; it need not expose a separate accrual feature switch.

Setup takes a sealed ledger backup and upgrades canonical storage explicitly.
Schema 3 adds versioned `business_parties`, `business_documents`,
`business_payments` and `business_tax_preparations` tables. Original schema 1/2
readers retain their original digest shape. New records, accounting entries,
allocation mutations and operation receipts commit through the same atomic
writer. Older engines refuse schema 3 instead of silently losing records.

## Parties

`party.save` takes `id,expected_revision,display_name,roles` and optional
`legal_name,email,address,terms_days,external_ids`. Roles contain `customer`,
`vendor` or both. Address fields are `line1,line2,city,region,postal_code,country`.
External identities are exact `{namespace,account_id,object_id}` records, unique
across parties. Similar names/emails produce duplicate suggestions, never an
automatic merge.

`party.archive` takes `id,expected_revision`. Archiving retains balances, history
and reporting responsibilities. `party.link_legacy` takes
`id,expected_revision,item_ids,reason`; it associates exact existing item IDs and
preserves their original counterparty labels. It refuses reassignment to a
different party.

`party.import` takes `rows`, each with the party.save fields. CSV import accepts
`id,display_name,roles` plus optional `legal_name,email,terms_days`. Separate two
roles using `customer|vendor`. CSV is translated to a normal reviewable atomic
import request. IDs are explicit; imported names do not decide identity.

Taxpayer identification values are forbidden throughout the ordinary business
request/result/state. W-9 evidence and encrypted full identity belong in a
protected host store. The engine consumes confirmed classification facts; it
does not decide a person's tax status.

## Documents

`document.save` takes `id,expected_revision,kind,party_id,date,due_date,currency,
control_account,lines`, plus optional `reference,evidence_ids`. Kind is `invoice`
or `bill`. Each line requires `description,quantity,unit_price,account`; optional
fields are `discount_amount,tax_amount,tax_account,tax_category,
classification_verified`. Quantity is a positive exact decimal string with at
most six fraction digits. Monetary inputs are decimal strings to cents. Extend
each line and round half up to cents, subtract explicit discount and add explicit
tax. The engine returns all line totals and document totals. It never chooses a
sales tax rate.

Draft saves post no financial entries. `document.issue` takes
`id,expected_revision` and optional `number`. It freezes the document and party
snapshot, retains immutable supporting evidence and creates one receivable or
payable item. Invoice numbers are unique. Provider send acceptance and delivery
attempts belong to the host and cannot change accounting/payment state.

`document.credit`, `document.writeoff` and `document.void` take
`id,expected_revision,date,reason,evidence_ids`, with applicable `amount` and
`counter_account`. Paid amounts require an explicit refund, not deletion. Closed
periods remain binding. Corrections preserve the issued snapshot and operation
history. The normal records read derives open, partially paid and paid states
from allocation history rather than trusting provider status.

## Payments

`payment.record`, `payment.link` and `payment.refund` use:

```json
{
  "operation_id": "synthetic-payment-op",
  "action": "payment.record",
  "id": "synthetic-payment",
  "party_id": "synthetic-vendor",
  "date": "2026-01-15",
  "currency": "USD",
  "direction": "pay",
  "gross_amount": "1200.00",
  "cash_amount": "1200.00",
  "federal_withholding": "0.00",
  "payment_method": "bank_transfer",
  "source": {"mode": "new", "source_id": "synthetic-bank-event", "bank_account": "Assets:Bank:Checking", "amount": "-1200.00"},
  "allocations": [{"item_id": "synthetic-bill", "amount": "1200.00", "tax_category": "nonemployee_compensation", "classification_verified": true}],
  "evidence_ids": ["stored-evidence-id"]
}
```

Gross equals allocation totals and cash plus federal withholding. Direction is
`pay` or `receive`. Rails are `bank_transfer`, `check`, `cash`, `card`,
`payment_network` or `unknown`; network qualification requires the separate
explicit boolean `network_qualifies_1099k`. Missing tax classification remains
`unknown` and blocks tax readiness.

`payment.record` settles linked items using the existing ledger item primitives.
For a direct payment without a bill, omit item_id and supply `counter_account`.
Already recorded bank payments use `payment.link` with source mode `existing`;
linking payment facts cannot pretend an unpaid bill was settled. The exact
payment source cannot become a second tax payment under a different ID.

Optional `fee_amount,fee_account` and `withholding_account` describe explicit
bank/payment components. Bank movement equals signed cash less fees. The engine
validates gross processor payment, fee and net cash against posted components
before reclassifying an existing multi-posting source. A timeout is not evidence
of a new payment; retry the same operation and inspect its receipt.

`refund_of` identifies the original payment and preserves its source history.
Refunds cannot exceed the original payment. An explicit correction/credit and
refund are separate facts; a provider refund alone does not prove that the books
have been corrected.

## Accounting and permissions

Invoice/bill accounting, aging, allocations and cash recognition remain engine
calculations. Explicit sales tax and other non-income/expense lines never become
cash P&L income or expense. Fees remain expenses alongside gross settlement.

Hosts must classify the action before authorization: read, ordinary commercial
write, protected setup/accounting decision, protected tax classification/review,
and external send are distinct authorities. Passing a strict JSON action must
never grant arbitrary journal, file, table or provider access. The generic bridge
must preserve the user's grant and trusted preparation/evidence authority; it
cannot add adjustment powers just because a commercial workflow calls an
accounting primitive.

## Read pages and tax preparation versions

All record queries accept `--limit 1..250` (default 100) and `--cursor TOKEN`.
Responses include `total` and `next_cursor`; continue until the cursor is null.
A cursor binds the exact ledger digest and query filters. Changed state or
filters require restarting the query, preventing skipped/repeated records from
being mistaken for a complete result. Date-filtered document/payment queries
omit records dated after `--as-of`. Party balances and aging are engine results.
Additional read resources are `aging`, `outstanding` and `payment_candidates`.

Canonical payments contain the exact recognition allocation needed to refund
income, expense and explicit tax separately. `payment.record` accepts an optional
`excess:{id,account}` when allocations include a remainder without an item ID.
The remainder becomes a party-linked open credit. Apply it with
`credit.apply:{operation_id,date,credit_item_id,target_item_id,amount,reason,
evidence_ids?}`. Stable party IDs must match even if their display name changes.
An already applied overpayment credit must be corrected at its target before a
refund can consume that original credit again.

`tax.prepare` takes `id,tax_year,payer_profile,recipients,coverage,
coverage_evidence_ids` plus optional `rule_version,supersedes`. It gathers all
canonical vendor payments and vendor refunds itself; a caller cannot supply a
selected payment subset. Customer receipts and customer refunds are excluded
from vendor payment preparation. A confirmed full-year/opening coverage claim
needs supporting evidence. Unlinked recorded outgoing cash or possible vendor
refund/correction sources remain visible and block readiness. Link actual bank
payments that predate commercial setup using `payment.link`; do not replay them.

Tax snapshots are immutable preparation versions in `business_tax_preparations`.
`tax.review` takes `id,expected_revision,preparation_hash`; an authorized person
reviews the exact ready version. Changed payment facts or new unresolved cash
sources require a new version. `supersedes` retains the old version with a
before/after hash and totals summary; it never claims a filed correction.
Export the exact frozen `preparation` through `tax-export` or the protected
recipient-copy operation. A preparation/report does not imply government filing
or recipient distribution. Recipient templates, supported years and preparation
limits are documented in [tax1099.md](tax1099.md).

Issued document credits, voids and write-offs expose the canonical correction in
`preview.details.business_correction` and the committed receipt's matching
`details.business_correction`. It binds document ID, operation ID, action,
resulting document revision, issued snapshot hash and exact gross amount.
`lines` preserves every original zero-based line index with separate `net_amount`
and `tax_amount`, including zero shares. Corresponding `net_minor_units`,
`tax_minor_units` and gross `amount_minor_units` are exact integer strings.
Repeated accounts and partial-cent cumulative rounding never require a host to
infer line identity from journal account names. An explicit counter-account
correction has `lines:null`; it must not be presented as an allocated invoice
credit without further supported evidence.
