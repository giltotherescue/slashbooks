# Stripe accounting

This packaged processor-accounting recipe is cash-basis only. It fails closed
for accrual books. Stripe records can still feed the same generic
[accounting intake](accrual-intake.md) as any provider: review journals,
prepaid/deferred schedules, items, and settlements without treating a payout as
another sale. A product or invoice label alone does not prove recognition policy.

The Stripe accounting planner separates gross sales, refunds, processor fees,
processor balance, payout transit and actual bank deposits. A payout is a transfer
of funds, not another sale. The balance transaction plus Stripe account owns the
financial source ID; charge, invoice, product, price and payout objects provide
supporting records without creating duplicate financial roots.

## Approve the setup once

Use an explicit historical opening date to prepare separate processor,
payout-transit, payment-processing expense and contra-revenue refund accounts.
Preparation adds audited catalog accounts only. It does not supply opening
balances, approve revenue rules, or post journals. Processor and transit accounts
cannot reuse another bank/card source's mapping.

The owner/accountant approves the Stripe account and test/live mode, the four
account roles, and exact revenue rules. A rule matches one price ID, product ID,
or metadata key/value on a named object. Multiple matching rules are ambiguous;
there is no precedence or broad fallback to subscriptions. Service revenue and
subscription revenue can have separate approved accounts. Metadata on different
objects is not interchangeable, and a current snapshot does not prove historical
business intent.

The hosted adapter stores that approval separately from provider descriptions
and agent-written files. Eligible exact matches then post without per-row
approval. Missing or conflicting evidence stays held. No Stripe metadata,
charges, refunds or payouts are changed by this workflow.

## Capture and apply

Capture complete read-only evidence for each bounded cohort: account/mode,
balance transactions, linked charges/refunds/payouts, payment intents, invoice
payments, complete invoice or Checkout lines, and referenced prices/products.
Coverage describes enrichment completeness for that cohort; a cursor can continue
the period. Preview totals retain explicit currency minor units. Disjoint preview
cohorts can be combined; overlapping balance transaction IDs are rejected.

The engine stages captured processor sources in the company currency without learned auto-posting,
then recomputes an exact plan against the current ledger, catalog, approved
policy and closed period. Application requires the current plan digest, staged
source identities and the ordinary atomic ledger writer. Hosted publication also
checks company revision and current policy/actor authority. A changed source,
mapping, policy or financial head needs a fresh plan. Replaying the same posted
roots adds no financial effect and does not teach counterparty rules.

Supported postings are:

| Captured event | Financial effect |
| --- | --- |
| Settled supported sale | Processor balance increases by net; fees are expense; exact approved revenue allocations total gross |
| Supported settled refund | Processor balance decreases; approved contra-revenue and any fee adjustment remain separate |
| Processor fee or fee credit | Processor balance and processing expense move in opposite directions; credits reduce expense |
| Minimum payout balance hold/release or Connect reserve | Processor balance moves to/from the explicitly approved reserved-balance asset account; no income or expense |
| Payout | Processor balance decreases; payout transit increases by the amount sent; any payout fee is a separate expense |
| Exact captured bank arrival | Bank balance increases; payout transit decreases; no income |
| Dispute (funds withdrawn, reinstated, or a dispute fee) | Never posts: unresolved with `dispute_needs_review`, no entry and no cash movement |
| Sale or refund whose identity, booking or category is uncertain | Never posts: unresolved with a reason, no entry and no cash movement |

Since round 9 nothing posts to a holding account. `Income:Uncategorized:Stripe`
("Uncategorized Stripe activity") only still appears in ledgers where an earlier
build wrote held entries; see "Entries an earlier build held" below.

### Which invoice a charge belongs to

The engine reads every place Stripe names a charge's invoice: `charge.invoice`,
`payment_intent.invoice` (captured, or expanded on the charge, a Checkout session
or an invoice payment), a modern invoice payment, a Checkout session's `invoice`,
and inline or expanded invoice objects.

One copy per object, or nothing. An object (an ID with at least one other field)
may appear more than once in a capture: in its own array and expanded at any
depth inside another object. Every copy is written in canonical typed JSON
(sorted keys; `1`, `true`, `1.0` and `null` all differ; every field counts,
descriptive text included; `metadata` exactly as given), with each object
expanded inside it replaced by its ID and checked as a copy of its own. When the
copies of one ID are not byte-identical in that form, every movement whose
closure reaches that ID stays unresolved with no journal entry
(`conflicting_object_copies`), so the order copies are merged in can never
matter. A row or expansion with only an ID (`{"id": "in_..."}`) is a reference,
not a copy. A wrapper in another currency never supplies the copies it
expands; a same-books wrapper does. A wrapper from the other live/test mode (for
example a dispute in other books that expands the charge) makes every movement
whose closure reaches it unresolved (`livemode_mismatch`, see below).

#### Capture contract for connectors

The connector that builds the capture (for Clozy, the Worker) must meet this
contract, or the movements that reach a mismatched object stay unresolved:

- **Which copies.** Every object with an `id` and at least one other field is a
  copy: each row of a top-level array (`balanceTransactions`, `charges`,
  `refunds`, `invoices`, `payouts`, `disputes`, `prices`, `products`,
  `paymentIntents`, `invoicePayments`, `checkoutSessions`) and each object
  expanded inside another at any depth, in any field and in any array element
  (for example `charge`, `latest_charge`, `payment_intent`, `invoice`,
  `balance_transaction`, `price`, `product`, `customer`, `source`, `transfer`,
  invoice lines, or a dispute's `balance_transactions` list), plus each row of
  `connectEvidence.transfers` and `connectEvidence.transferReversals`. Each
  top-level array holds one row per ID (a repeated ID rejects the whole
  capture). Expansions inside a wrapper from another mode or currency are
  copies too: that wrapper never lends evidence, but its copies must still
  agree.
- **Which fields.** All of them. Every copy of one ID must carry the same set of
  fields with the same values and JSON types, after normalization. The only
  normalization is this: any value that is an object with an `id` (in any
  field, or as any array element, whatever the field is called) is replaced by
  that ID string and checked as a copy of its own, so an expanded `customer`
  and the string `"cus_..."` compare equal; a list keeps its order and its
  field name. Nothing else is normalized: no trimming, case folding, number
  conversion or dropping of `null` fields. Key order does not matter; a missing field differs
  from `null`, `1` from `1.0`, and `"1"` from `1`. `metadata` compares exactly as
  given, key for key.
- **Descriptive text.** Descriptions, statement descriptors
  (`statement_descriptor`, `calculated_statement_descriptor`), `footer`,
  `receipt_url`, `hosted_invoice_url`, `invoice_pdf`, receipt numbers, names and
  every other text field count like any other field: they must be
  byte-identical in every copy. Do not trim, translate, redact or truncate text
  in one copy only; redact the same way everywhere or not at all.
- **Partial expansions.** Send an object either in full (identical to its other
  copies) or as an ID only (a string ID, or `{"id": ...}` with nothing else).
  `{"id": ..., "object": ...}` is still an ID only. Never send a projection with
  a subset of fields, such as `{"id": ..., "amount": ...}`, beside the full
  object. A payout balance transaction whose `source` is an ID-only wrapper uses
  the full payout from `payouts`.
- **One fetch per object.** Take every copy of one object from the same API
  read (or expand it from the same response). Two reads taken at different times
  may differ (a status, an amount refunded, an updated `metadata` key) and then
  conflict.

The engine resolves first and decides after. For each movement it gathers one
closure of everything the evidence says about the charge: every copy of the
charge (captured, expanded on a balance transaction, a refund, a dispute, an
invoice payment or an invoice), every copy of its payment intent (including one
named only by a refund, or one whose `latest_charge` names the charge), Checkout
sessions and invoice payments of both types for the charge or intent, and every
invoice they name, including an invoice that only names the charge or intent
back (`invoice.charge`, `invoice.payment_intent`). It follows these references
until nothing new is found, in both directions, so the order of the arrays or the
side that states a relationship does not change the result. Only then does it
validate the closure.

The identity is uncertain, and the movement stays unresolved with a reason and
no entry, when references name different objects, a named invoice, charge,
refund or payment intent is not in the evidence, two Checkout sessions claim one
payment, an expanded object has no ID, copies disagree, or a document is
explicitly from the other live/test mode.

**Test and live mode never mix.** A movement whose balance transaction, or any
object in its closure (the payout, refund, charge, invoice, price, product,
transfer or any other linked document, at any depth, including a wrapper from
other books), states a `livemode` different from the policy's Stripe account
mode, or whose copies of one object disagree on `livemode`, is unresolved
(`livemode_mismatch`) with no entry. This is checked before the copy-equality
check. Its IDs still enter the owner map. A copy that leaves `livemode` out
states nothing. This covers payouts in both shapes (expanded, or an ID wrapper
with the document in `payouts`), zero payouts included. An expansion that carries only an ID
(`{"id": "pi_..."}`) is a reference, not the document: it never stands in for an
object the evidence does not hold, whichever copy (the charge, a refund, a
dispute) names it. Every relationship used must also be in the policy currency
when it states one: a Checkout session, an invoice payment, a price or a product
in another currency stays unresolved (`checkout_session_currency_conflict`,
`invoice_payment_currency_conflict`, `price_currency_conflict`,
`product_currency_conflict`). Every catalog copy used to choose an account (each
expanded or captured price and product) must be in the policy's live/test mode.
A Checkout session for the payment that is expired, open or unpaid never links
the payment to an invoice or lines (`checkout_session_not_paid`).

Invoice payments count in both shapes, `payment.type: "payment_intent"` and
`payment.type: "charge"`, and are checked together. An invoice belongs to one
charge: an invoice whose `charge` or `payment_intent` names another object, a
payment intent whose `latest_charge` is another charge, an invoice or payment
intent in another currency, and an invoice that another payment also claims (a
second charge that is not failed, a succeeded payment intent, a paid invoice
payment, a Checkout session with another payment intent, or another live charge
for the same payment intent) stay unresolved
(`invoice_reference_conflict`, `payment_intent_reference_conflict`,
`invoice_currency_conflict`, `invoice_claimed_by_another_payment`). A line's
price, its pricing details, `price_id`, `product_id`, the captured price and
every expanded or captured copy of the product must name the same objects and
agree on metadata. Copies of one price or product are merged by ID across every
line of the invoice before any rule is chosen, so two lines never see different
copies of one object; a contradiction stays unresolved with
`price_reference_conflict` or `product_reference_conflict`. Revenue rules read
the same combined payment intent that resolved the invoice. Descriptive text is
not exempt: the copy check above runs first, so two copies of one object that
word `description`, statement descriptors, receipt or hosted URLs or `footer`
differently already make the movement unresolved
(`conflicting_object_copies`) before any merge.

The allocator and the fingerprint read the same evidence. Before a sale or
refund posts, the engine records every document the allocator and resolver
actually read (the charge, payment intents, invoices, lines, prices and products
named by `price`, `price_id`, `product_id` or `product`, Checkout sessions and
invoice payments) and requires each to be inside the movement's hashed closure.
A document read but not hashed means the fingerprint cannot reproduce the
decision: the movement stays unresolved (`capture_fingerprint_incomplete`), and
cleanup of an already posted one needs review.

Claims are reservations. Every Stripe entry this engine writes records its kind
as entry metadata (`stripe-kind`: `sale`, `refund` or `other`), in the same
transaction as the entry. A sale entry also records what it claims:
`stripe-charge` (the charge), `stripe-payment-intent` and `stripe-invoice`
(every charge, payment intent and invoice reachable in its evidence,
space-separated). A refund entry records `stripe-refund`, and a payout movement
records `stripe-payout`. Once any ledger entry names an ID in that metadata (a
live, held, corrected or reversed entry, a reversal or a correction), it stays
reserved for good. A correction never frees a claim, and a correction that names
one adds its own. A lineage with a correction that has no source ID (the writer
now refuses one; an older writer could leave one) makes everything it names
unproven (`ownership_unproven`, unresolved).

**One owner map before anything posts.** Before any entry is proposed, the plan
builds one map from every movement ID to its owners: each balance transaction,
charge, payment intent, invoice, refund, payout and dispute balance transaction,
for every kind of movement in the supplied capture (posting, unresolved or with
no financial effect) and every durable reservation in the ledger (the claims
above, the IDs a verified no-effect source reserved, and the dispute IDs and
movements an earlier build recorded). Ownership is decided over the whole
capture before any paging: every validated row claims its IDs whichever page it
falls on, and only then is the result restricted to the page that `limit` and
`after_source_id` select. The same capture gives every row the same ownership
decision for every `limit` and cursor. A sale owns every charge, payment intent
and invoice reachable in its closure; a refund owns its refund ID; a payout its
payout ID; a dispute owns every balance transaction its document lists. An ID
with two owners makes every batch claimant unresolved with no entry, whichever
order the movements came in: `charge_claimed_by_another_movement`,
`payment_intent_claimed_by_another_payment`,
`invoice_claimed_by_another_payment`, `refund_already_recorded`,
`duplicate_refund_movement`, `payout_already_recorded`,
`duplicate_payout_movement` or `movement_claimed_by_another_owner` (for example
a fee movement that a dispute lists, or one balance transaction two disputes
list). A movement that is unresolved for any other reason still reserves its IDs
in this map for that capture. That reservation is not durable: a later capture
does not see it, because nothing was written.

**Every veto is decided over the whole capture.** Like ownership, every
cohort-level veto is evaluated over the whole supplied capture before paging
restricts which rows post. A known bank-income conflict for any bound payout
(for example `bank_source_already_posted_requires_review`, see bank matching
below) blocks every page: no page posts any action, for every `limit` and
cursor direction, and every page reports the conflict.

**A refund that arrived before its sale.** A refund may post only when its sale
is booked in the ledger or approved on the same page (the same write). A refund
left unresolved only because its sale was not booked yet (`sale_not_posted` or
`sale_movement_unverified` while the sale is on another page or in a later
capture) may post once on a later run, after the sale is booked, but only if
every check passes on that run: the same booked sale, its one stored charge and
an unchanged sale fingerprint, the cap counting every claim (below), and its
own refund ID owned by nobody else. A refund unresolved for any other reason
stays unresolved: booking the sale lifts nothing else. The engine keeps no
record of earlier unresolved reasons; every run checks everything again.

A sale needs a charge identity: a sale balance transaction whose `source` is
missing or null stays unresolved (`sale_identity_unverified`).

Nothing is held. Whenever the engine cannot prove a movement's identity,
uniqueness, booking or category, it records no entry at all (no holding entry
and no cash movement) and reports the row in `unresolved` with its reason. When
a row has several problems, the first reason found is reported, except that a
refund over its sale's cap always reports `refund_exceeds_booked_sale`.

No historical proof. Rule IDs, authority text, postings and saved digests are
never read to decide what an entry was. Only structured metadata written by
this engine version proves an entry is outside the block: the engine version
(`stripe-engine-version` of `1`), a known kind (`stripe-kind` of `sale`,
`refund`, `dispute`, `other` or `invoice_settlement`) and the Stripe account
(`stripe-account`) that the entry's own source ID names.
That exempts (a) another Stripe account's entries this engine wrote, which still
reserve every charge, payment intent and invoice they name, and (b) the invoice
engine's settlement of an open invoice by a Stripe movement, which also records
its invoice (`stripe-invoice`). When the ledger holds any other Stripe entry
(written by an earlier release, imported, with a `settlement-id` or Stripe source
ID but no such metadata, or any entry in the correction lineage of a Stripe
movement, original, reversal or replacement, that this engine did not write),
every new
Stripe movement for that ledger stays unresolved with no journal entry
(`legacy_stripe_entries_present`), and so does the bank arrival of a payout. That
includes fees and payouts. Movements already posted are still skipped and their
interrupted cleanup still runs. A later cutover tool will lift this; there is no
backfill. Ledgers written by earlier, unreleased builds of this change (before
entries recorded `stripe-engine-version`) count as legacy too, even when their
entries carry `stripe-kind` and `stripe-account`: for example a paid payout an
earlier build recorded as `other` without `stripe-payout`. Historical ownership
is not reconstructed. A zero payout an earlier build verified with no financial
effect, whose saved source records no reserved IDs, counts as legacy the same
way. They block new Stripe movements, so re-create any test books built with
them.

A refund posts only when it is keyed to a sale this engine booked. Its sale is
found through the charge's `balance_transaction` (or the one sale movement of
the charge in the cohort). The refund posts only when all of these hold:

- the sale movement is booked: either a live sale entry this engine wrote (never
  corrected, reversed or held), or an approved sale action in this same batch;
- the refund's charge ID equals that sale's recorded `stripe-charge` (and the
  refund names no other charge) — otherwise `refund_charge_conflict`;
- the sale's current fingerprint equals the one recorded on its entry
  (`stripe-closure`), so the sale's evidence is unchanged since it was booked —
  otherwise `sale_changed_since_booking`;
- it fits under the cap (below);
- its own refund ID is present and owned by nobody else (see the owner map).

That hash covers every object in the sale's closure, every field and every
copy: the sale movement itself (its status and extra fields included), the
charge, payment intents, invoices, lines, prices, products, Checkout sessions,
invoice payments and rival charges. Refunds and disputes of the charge, and
movements only they name, are left out. It is the same hash the sale's
finalization receipt binds as its capture digest. So a later refund cohort must
carry the original sale's balance transaction (top level or expanded on the
charge) and the rest of the sale's evidence unchanged. Anything else leaves the
refund unresolved with no entry: the sale movement changed, is refused or is
unavailable (`sale_not_posted`, `sale_movement_changed`), the sale was held by an
earlier build (`sale_in_holding`), corrected or reversed (`sale_corrected`), or
it is absent, a payout or not a sale this engine wrote
(`sale_movement_unverified`).

**The cap is never skipped.** The refundable amount is the booked sale's gross,
keyed by its sale movement (`stripe-sale`), minus every refund already posted
against that movement or its recorded charge (held by an earlier build,
corrected or reversed alike) and every refund of it in the supplied capture,
including refund rows that are unresolved for another reason (an invalid
status, a pending or failed refund document, a changed source, a closed period,
a legacy ledger). A refund's attribution (its charge and sale) is extracted
separately from its eligibility and before any posting check, so an early
refusal never hides a claim from the cap. Each identifiable claim counts once: a
refund ID the ledger already recorded counts there and not again from the
capture, two captured movements of one refund ID are one claim, and a refund
document that says another movement carries it is that movement's claim. A credit back
never raises it. Any refund over the cap stays unresolved with
`refund_exceeds_booked_sale`, and that reason replaces any other the row had, so
the cap shows even when the row has an earlier problem.

A refund posts once. A refund ID that any ledger entry already recorded never
posts again, whatever movement carries it (`refund_already_recorded`), and two
captured movements with the same refund ID both stay unposted
(`duplicate_refund_movement`). A refund without its own refund ID stays
unresolved (`refund_identity_unverified`). Only a refund entry written by an
earlier build that records neither its charge, its sale movement nor its own ID
makes later refunds unproven (`ownership_unproven`). A refund of a sale with
more than one invoice line stays unresolved with
`mixed_line_refund_requires_allocation`.

**Disputes never post.** Every dispute movement (funds withdrawn, reinstated,
or a dispute fee) stays unresolved with `dispute_needs_review`, with no entry
and no cash movement; no approved policy names a dispute account. A dispute
link always wins over the balance transaction's type: a movement of any type
(a `stripe_fee` included) whose source is a dispute, or that any dispute
document lists in `balance_transactions`, is a dispute movement. A source is a
dispute when it says so (`object` of `dispute`, or a `du_` or `dp_` ID, as a
bare ID, an ID wrapper or a full copy) or when any copy of that ID anywhere in
the capture is a dispute, including a typed reference that is not a full copy
(a `disputes` row or an expanded `{"id": ..., "object": "dispute"}` that carries
only its ID and type). Such a reference is type evidence only: it never joins
the copy-equality check or the fingerprint. Dispute documents are collected from
every place they appear: the top-level `disputes` array, a movement's expanded
source, or expanded inside any other object. Its dispute ID and the balance transactions
its document lists still enter the owner map, so another movement that claims
one of them stays unresolved too. A fee with no dispute link still posts to the
fee account.

**Connect platform dispute fees (launch scope).** A Connect platform can pay the
dispute fee for a connected account's dispute. Stripe attributes that fee to the
dispute in fee-attribution data (`incurred_by`, `incurred_by_type`) rather than
in the movement's own `source`, so the movement need not name the dispute. For
launch, a movement that shows any sign of being a dispute fee without itself
identifying the dispute is unresolved with `dispute_needs_review` and is never
booked as a standalone fee. Signs are a `reporting_category` of `dispute`,
`dispute_reversal` or `dispute_fee`; an `incurred_by_type` of `dispute` or an
`incurred_by` dispute reference, on the movement or on any `fee_details` row; a
`fee_details` type that names a dispute; and, for `stripe_fee` and
`stripe_fx_fee` movements, the word "dispute" in the movement's own description
or fee-detail descriptions. Text can only refuse a fee, never choose an account.
A connector that wants these fees booked must first capture itemized attribution
that names the connected-account dispute; such a fee is still a dispute movement
and stays unresolved until disputes are supported. Dispute
entries an earlier build wrote (with `stripe-dispute` and
`stripe-dispute-movements`) stay durable reservations of the movements they
recorded; the engine no longer writes that metadata.

**Payouts are reserved by payout ID.** Each paid payout movement records its
payout ID (`stripe-payout`). A payout ID already recorded never posts again
(`payout_already_recorded`), and two movements of one payout in a capture both
stay unposted (`duplicate_payout_movement`). A paid zero payout accepted with no
financial effect reserves its payout ID too: its saved source records the IDs
it reserves (`stripe_no_effect_claims`), and it takes part in the same owner
map. A later capture that books the same payout through another movement stays
unresolved (`payout_already_recorded`), and a zero payout whose payout another
movement in the same capture claims is not accepted. Payout entries written by
earlier builds of this change lack `stripe-engine-version`, so a ledger that
holds one is blocked as legacy (see "No historical proof"): no new Stripe
movement posts on it, so an unreserved payout ID can never post a second time.


Revenue authority comes from one checked path. A plain charge (no invoice or
Checkout session) is mapped only by metadata rules; a plain charge that carries a
price or product selector (`price`, `price_id`, `product_id` and the like) stays unresolved
with `plain_charge_price_unsupported`, because only invoice and session lines pass
the catalog's mode, currency and copy checks. A refund, dispute or Checkout
session in another mode or currency never adds evidence to a sale's closure. Copies
of one payment intent must agree on its status, and a payment intent that did not
succeed holds (`payment_intent_not_succeeded`).

A known sale whose allocation is not complete (a missing revenue rule, tax,
discount, proration, partial payment, truncated lines) or a movement that has
not settled stays unposted in `unresolved`, as before.

### Entries an earlier build held

Builds before round 9 posted some sales, refunds and disputes to
`Income:Uncategorized:Stripe` as held entries. The engine never moves such an
entry itself, and never posts a refund against a sale it held
(`sale_in_holding`). Check the evidence, then correct it through the ordinary
ledger correction path (`books ledger correct`), which reverses the held entry
and writes the reviewed replacement in one audited write. Do not edit, delete or
re-import it. The correction needs its own replacement source ID (the writer
refuses one without). The sale's claims stay reserved after the correction, and
the replacement adds any it names. A refund of a corrected sale never posts
automatically (`sale_corrected`).

Ledger corrections refuse a replacement ID that is another spelling of the entry
it corrects or of any source the ledger already holds (for example `abc` and
`banksync:abc`), because the original's reversal would hide it. A Stripe source
ID has one spelling: an alias such as `banksync:stripe:...` is refused, so the
guards and the reversal always name the real entry. Pre-release builds of this
holding account wrote `stripe-reclassify:` move entries; the engine no longer
writes them, but an original with a live move still cannot be reversed or
corrected on its own (reverse the move first), and a move can be reversed but not
replaced.

Bank matching requires the original bank source ID/digest, payout ID, exact
currency/amount and approved current bank mapping. A bank arrival can occur after
the payout in another period. An old net bank deposit already booked as income
blocks application until a normal audited correction resolves it; the engine
does not clone the source or reverse an already reversed entry. That block is
decided over the whole supplied capture: whichever page the bound payout is on,
no page of that capture posts anything, and every page reports the conflict. When managed
Stripe accounting exists, the hosted routine planner holds positive bank credits
with a Stripe label as suspected payouts. A label is suspicion, not proof of a
specific payout; unrelated approved routine items can continue.

## Explicit limits

This slice posts the company currency only (`currency` in `entity.json`, USD
when absent; see [company currency](company-currency.md)). The policy `currency`
must be the company currency. A Stripe account stays single-currency: balance
transactions in more than one currency, or in another currency than a company
with entries, stop the stage with `unsupported_currency`. A company with no
entries takes the Stripe balance currency. FX, unsupported balance types, unsettled
charges/refunds, incomplete lines, partial/multiple invoice payments and
unsupported tax, shipping, discounts, credits or prorations remain held unposted.
Disputes and sales or refunds of uncertain identity stay unresolved with no
entry. Refunds must also pass the original sale's evidence checks; mixed-line
refunds need explicit allocation and stay unresolved. Clearing roles are not available bank
cash. This is cash-basis processor bookkeeping, not deferred revenue, tax
compliance or subscription accrual recognition.

The local `books connector stripe download` command remains a raw balance/payout
collector. It does not collect the full revenue-mapping evidence or approve these
postings. Do not treat generic ingestion of net deposits as gross Stripe
accounting. Hosted connection, policy and captured-evidence routes supply the
trusted accounting workflow; the pure engine helpers are available to adapters
as `bookkeeping.stripe`.


## Connected cloud accounts

Use the company's normal cloud login. Never request a Stripe secret from the client.
After a human connects Stripe and approves revenue rules, agents can run:

```sh
books cloud stripe status --entity /path/to/company
books cloud stripe preview --entity /path/to/company --request-id preview-2026-01 --from 2026-01-01 --to 2026-01-31
books cloud stripe sync --entity /path/to/company --request-id import-2026-01 --from 2026-01-01 --to 2026-01-31
books cloud stripe receipt --entity /path/to/company --request-id import-2026-01
```

Preview never posts entries. Sync continues through the server's saved pages under
existing authorization. If interrupted, repeat the exact command with the same
request ID and dates. The server reuses saved evidence and completed receipts.
Use a new request ID only for a new intended import. Held items require specific
facts or authorized rule changes; do not change policy or silently guess.
Connection and policy approval stay in the human web workflow. This command does
not create a recurring schedule or continue after the command process exits.


### Propose company-specific rules

Read `books cloud stripe policy` for the current policy version, chart, products,
prices and metadata. Combine those facts with the saved business brief and the
owner's instructions. Do not infer accounting authority from provider text.

Save a proposed `context/stripe-revenue-draft.json` through the normal audited file command:

```json
{"policy_version":0,"explanation":"Explain the business reason and any assumptions.","revenue_rules":[{"id":"membership","match":{"product_id":"prod_example"},"category":"Income:Subscriptions"}]}
```

```sh
books cloud file put context/stripe-revenue-draft.json --file /path/to/draft.json --explanation "Propose Stripe revenue categories for accountant review"
```

The accountant can choose **Load agent draft** in Stripe categories, inspect and
edit the suggestions, and approve once. Drafts do not activate any rule. Use only
selectors present in the Stripe preview and current chart accounts. Read the prior
file before revising it, and preserve categories the accountant still wants.

An approved apply stores an immutable finalization receipt with the financial
entries. If a local failure interrupts marking sources seen or removing Review
rows, repeat the same captured evidence and policy with a fresh plan. The retry
verifies the receipt, posted entry, canonical source and unchanged staged row,
then completes cleanup without posting again. A newer capture may contain
additional movements: recovery compares the original movement's referenced
authority objects, not the whole capture. Changes to that source's charge,
invoice, refund, payout or other relevant authority evidence still block cleanup. The
fingerprint is the full hash of the movement's closure: every object reachable from
the movement through its references (and every object that names its charge,
payment intent, invoice or movement back), each in canonical typed JSON with every
field, the movement's own status and extra fields included, and an ID the capture
names but does not hold bound as absent. A sale's fingerprint is the same hash its
entry records as `stripe-closure` (it leaves out the charge's refunds and disputes).
Since round 6 it no longer equals the projection digest earlier releases recorded,
so cleanup of an apply that an earlier release interrupted needs review. A closure
with copies that disagree has no fingerprint. Evidence that cannot be fingerprinted
is never a match: a new movement stays unresolved, and cleanup of a posted one needs
review. Changed or
unproven evidence requires reconciliation before cleanup. This holds the one row, not the batch: the
row is reported as unresolved with the reason `cleanup_needs_review`, it stays in
Review, and the other rows in the batch still post. A source that an older engine
posted without a receipt is cleaned up only when its saved source digest, its policy
digest and its staged row all match the current plan; if they do not match, it gets
the same reason.

`stripe_apply` returns the held rows. `unresolved` lists every row held unposted as
`{source_id, reason}`. `uncategorized` is still returned for compatibility but is
always empty since round 9, because nothing posts to a holding account, and no
row carries a `review_reason`. `needs_review` lists only the cleanup rows as
`{source_id, reason: "cleanup_needs_review", detail}`; `held` is the length of
`unresolved`. Each cleanup row also gets `review_hold_reason: "cleanup_needs_review"`
in Review. If a person proposes a category for it, the confirm is refused with a
message that names this reason, because the source is already posted.

A person resolves the row in one of two ways:

- Restore the original evidence or policy and repeat the same `stripe_apply`. The
  engine verifies the row again, cleans it up and removes the hold.
- If the posted entry is correct and the evidence cannot be restored, check the entry,
  then call `stripe_dismiss_held(entity, source_id, note=...)`. It removes the row from
  Review, writes an audit event with the note, and does not change the ledger entry.
  It refuses a row that is not held with this reason or not posted. It also refuses in
  these three cases:
  - The posted entry was reversed, so it is not live.
  - The audit chain is not intact. Restore the ledger first.
  - A queue item for the source is still actionable (`open`, `reopened` or
    `requires-reproposal`). The error names the item. Withdraw the item first.

## Reserved balances

An optional `reserve_account` policy role must name a distinct, open
`Assets:Processors:` account in the company currency. Approve this role once
to record nonzero `payout_minimum_balance_hold`,
`payout_minimum_balance_release`, and `reserve_transaction` movements. Without
that role, these movements wait for account setup. This does not authorize
Connect transfers to recipients or treat those transfers as expenses.

These event meanings follow [Stripe's balance transaction reference](https://docs.stripe.com/reports/balance-transaction-types).

## Connected-account transfers

Captured `connectEvidence.transfers` and `transferReversals` support outgoing
transfers and their returns. The engine validates identity, mode, currency,
direction, amount and reversal parent before applying an explicit policy rule:

```json
{"transfer_rules":[{"id":"recipient-funds","match":{"destination":"acct_example"},"category":"Liabilities:Recipient-Funds","accounting_note":"Recipient funds are payable; related customer receipts are recorded separately under the confirmed treatment."}]}
```

Use exactly one `destination` or `transfer_id` selector. The category must be an
open expense, income or liability account, separate from the processor roles.
Overlapping rules hold the movement. This rule classifies the transfer only;
it does not split the customer charge, select gross versus net presentation,
or calculate commission. Confirm that treatment and check the related charge
and fees first. Provider metadata is evidence, never accounting authority.

Returns use the parent transfer's rule. A return cannot use a category that
conflicts with its already-recorded original transfer. Discovery exposes bounded
`transfer_options` without customer names, contact details or arbitrary metadata.

A paid zero-value payout with matching evidence and no fees is retained as a
source with status `no_financial_effect`. It creates no journal entry and returns
`no_financial_effect` separately from `posted`. Its saved source reserves its
payout ID (`stripe_no_effect_claims`) like a posted payout entry, so another
movement of that payout stays unresolved. Replays verify the source and
evidence before finishing queue cleanup; changed evidence stays unresolved.
