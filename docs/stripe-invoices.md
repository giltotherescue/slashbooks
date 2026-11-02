# Stripe invoice imports

`books business preview --entity PATH --input FILE` accepts `document.import`.
Apply the unchanged plan with `books business apply`. Both are network-free.
The engine neither creates nor sends a provider invoice and never chooses an
income account from a name, description or product identifier.

```json
{
  "operation_id": "synthetic-import",
  "action": "document.import",
  "source": {
    "provider": "stripe",
    "account_id": "acct_synthetic",
    "livemode": false,
    "invoice_id": "in_synthetic",
    "evidence_id": "stored-evidence-id",
    "source_hash": "canonical packet SHA-256"
  },
  "party_id": "reviewed-customer",
  "control_account": "Assets:Receivable:Trade",
  "line_mappings": [{"line_id": "il_synthetic", "account": "Income:Reviewed-Service"}],
  "processor_account": "Assets:Processors:Synthetic",
  "fee_account": "Expenses:Reviewed-Fees",
  "payment_sources": [{"payment_id": "inpay_synthetic", "source_id": "stripe:acct_synthetic:txn_synthetic", "mode": "existing"}]
}
```

All accounts must exist and be explicitly selected under current authority.
Add `tax_account` to each taxed line mapping. `payment_sources` contains every
paid invoice-payment identity once; use an empty array for an unpaid invoice.
`processor_account` is required for payments; `fee_account` is required for a
nonzero fee. Money, dates, quantities and tax amounts cannot be overridden in
this request.

## Immutable source packet

Store a normal intake envelope whose `observed_facts.stripe_invoice` contains:

- `schema_version:1`, `source:"stripe_invoices"`, `api_version:"2025-06-30.basil"`.
- `account:{id,livemode,default_currency,timezone}` with a verified IANA zone.
- One `invoice`, including complete `lines:{data,has_more:false}` and
  `payments:{data,has_more:false}`. Capture issue-time `customer_name`,
  `customer_email` and `customer_address` where provided.
- `customer`, `credit_notes:{data,has_more:false}`, `charges:[]`,
  `balance_transactions:[]`, `refunds:[]`, `payment_intents:[]`.
- `coverage` with exactly `invoice_lines_complete`,
  `invoice_payments_complete`, `credit_notes_complete`,
  `payment_sources_complete`, `refund_sources_complete`, all boolean `true`.

`source_hash` is `canonical_hash` of this exact stable packet. Do not include
collection timings, metrics, metadata, taxpayer identifiers, client secrets or
signed download URLs. The evidence remains immutable when later payments are
captured under another hash.

Use the pinned Basil fields: invoice lines have `taxes` and
`pricing.unit_amount_decimal`; invoice payments have
`status_transitions.paid_at` and a charge or payment-intent reference. Payment
proof includes the captured successful charge, `amount_captured`, its balance
transaction, and the linked payment intent when applicable. A provider-paid
flag alone is not money evidence.

The recognition date is `effective_at` when present, otherwise
`status_transitions.finalized_at`, in the verified account timezone. `created`
is discovery time and does not establish recognition. Cash uses the actual
balance-transaction date, preserving the existing Stripe UTC source convention.
If that UTC payment date precedes the invoice's local issue date, the import
holds with `settlement_precedes_origin` for accounting review. It never shifts
the cash source date or the issued date to force a settlement.

Python reconciles explicit minor-unit amounts, discounts, inclusive/exclusive
tax and header totals. Fractional unit prices remain in `source_pricing` and
must reconcile to the provider's exact cent line amount. No rate is inferred.
`calculation_basis:"provider_minor_units"` marks these imported lines.

## Identity and existing records

Account, live/test mode and invoice ID identify the document. Reconnection
versions and request IDs do not create new invoices. New records expose
`external_source:{provider,account_id,livemode,invoice_id,source_hash}` and
`provider_managed:true`. A host must not treat such an invoice as a new provider
send. `provider_billing_snapshot:{name,email,address}` preserves the invoice's
issued billing facts; mutable Customer data is not a substitute.

An explicitly reviewed existing document match supplies
`match:{document_id,expected_revision,issued_snapshot_hash}`. The engine checks
the original financial fields and account offsets. Its origin, issued hash,
party snapshot and existing payment history remain unchanged. A matched local
document has `provider_managed:false`; a host must additionally verify its
authoritative provider binding before automatic matching.

Existing recorded payments require exact source, amount, date, fee, party,
item allocations and recognition proof. Matching adds no cash or settlement.
An existing cash sale can instead be reclassified into receivables settlement:
the original processor/fee journal stays unchanged and only its supported income
offsets are reversed into the control account. Partial source allocations use
the engine's cumulative cent allocation. Exact balance-transaction claims cause
the ordinary Stripe cash planner to skip this owned settlement.

## Results and boundaries

Preview includes `details.document_import` with `ready`, `matched` or `held`,
`blockers`, `document_id`, `party_id`, `external_source`, `payment_matches` and
the canonical `document`. `details.document` also contains that document for
ordinary review rendering. `payment_matches` distinguishes `settled` from
`matched`; a matched ordinary payment retains its `business_payment_id`.
`requires_adjustments` is a sealed boolean: a new incoming credit or void
against an already recorded canonical document requires correction authority.
Initial historical import, existing-source settlement and a fresh exact match
do not widen that requirement. Original-plan replay retains its original flag.
Hosts must derive authorization from the trusted engine plan, not caller fields.

Apply commits origin, settlements, supporting records, source claims and its
receipt atomically. The receipt's `details.document_import.status` is `imported`
or `matched`. Same-operation replay returns its original receipt; another
request for the same provider identity cannot create another recognition or
cash source. Later snapshots can append only proved new settlements/credits.
Changed issued facts or regressed history require review.

Supported cases include USD open, partial and paid invoices, explicit discounts
and taxes, exact existing-source reclassification, complete unpaid credits
allocated to original invoice lines, and an unpaid void with its evidenced
void date. A void keeps the original provider total even when Stripe retains
that amount in its raw remaining field; canonical collectible balance is zero.

Holds have no financial effects and cannot be applied. They include incomplete
pages or money relationships, missing mappings, foreign currency, drafts,
uncollectible/write-off decisions, negative/proration lines, customer balances,
out-of-band payments, paid/mixed credit/refund histories, voided credit notes,
and inclusive-tax discounts without an explicit net breakdown. Closed periods
and pre-cutover history retain existing reopen/opening requirements. An
unmatched local credit/refund or outstanding balance also holds; history is
never rewritten to force agreement with Stripe.

After canonical commit, ordinary pending-source cleanup may still need retry.
Inspect the original receipt and replay the original plan to finish that cleanup;
never create another payment. A hosted publisher must publish its workspace and
provider mapping only after successful complete import proof.
