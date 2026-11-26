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
| Sale, refund or dispute whose identity is uncertain | Processor balance moves by net and fees are expense, as usual; the gross goes to `Income:Uncategorized:Stripe` ("Uncategorized Stripe activity") for review |
| Held sale or refund | Stays in `Income:Uncategorized:Stripe` until a person corrects it with `books ledger correct`; the engine never moves it |

### Which invoice a charge belongs to

The engine reads every place Stripe names a charge's invoice: `charge.invoice`,
`payment_intent.invoice` (captured, or expanded on the charge, a Checkout session
or an invoice payment), a modern invoice payment, a Checkout session's `invoice`,
and inline or expanded invoice objects. Copies of one object (an expanded balance
transaction source and a captured charge, an inline and a captured invoice) are
combined: a copy may add fields or invoice lines the other leaves out, but the
copies must agree on every field the engine reads, including nested lines,
taxes, discounts and metadata. Accounting checks run on the combined result.

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

The identity is uncertain, and the movement posts to the holding account with a
`review_reason`, when references name different objects, a named invoice, charge,
refund or payment intent is not in the evidence, two Checkout sessions claim one
payment, an expanded object has no ID, copies disagree, or a document is
explicitly from the other live/test mode. An expansion that carries only an ID
(`{"id": "pi_..."}`) is a reference, not the document: it never stands in for an
object the evidence does not hold, whichever copy (the charge, a refund, a
dispute) names it. Every relationship used must also be in the policy currency
when it states one: a Checkout session, an invoice payment, a price or a product
in another currency is held (`checkout_session_currency_conflict`,
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
for the same payment intent) are held
(`invoice_reference_conflict`, `payment_intent_reference_conflict`,
`invoice_currency_conflict`, `invoice_claimed_by_another_payment`). A line's
price, its pricing details, `price_id`, `product_id`, the captured price and
every expanded or captured copy of the product must name the same objects and
agree on metadata. Copies of one price or product are merged by ID across every
line of the invoice before any rule is chosen, so two lines never see different
copies of one object; a contradiction is held with
`price_reference_conflict` or `product_reference_conflict`. Revenue rules read
the same combined payment intent that resolved the invoice. Copies may word
descriptive text differently (`description`, statement descriptors, receipt and
hosted URLs, `footer`); amounts, taxes, discounts, selectors, identity and line
completeness must still agree.

Claims are reservations. Every Stripe entry this engine writes records its kind
as entry metadata (`stripe-kind`: `sale`, `refund`, `dispute` or `other`), in the
same transaction as the entry. A sale entry, held or not, also records what it
claims: `stripe-charge` (the charge), `stripe-payment-intent` and
`stripe-invoice` (every payment intent and invoice its evidence names,
space-separated). Once any ledger entry names a charge, payment intent or invoice
in that metadata (a live, held, corrected or reversed entry, a reversal or a
correction), it stays reserved for good. Before a sale posts as revenue, the plan
checks every such claim across every cohort and the whole correction lineage: a
sale that names a reserved charge, payment intent or invoice is held
(`charge_claimed_by_another_movement`,
`payment_intent_claimed_by_another_payment`,
`invoice_claimed_by_another_payment`), and so are sales in one batch that share
one. A correction never frees a claim, and a correction that names one adds its
own. A lineage with a correction that has no source ID (the writer now refuses
one; an older writer could leave one) makes everything it names unproven
(`ownership_unproven`).

Kinds are proved, never inferred. Rule IDs, authority text and postings are never
read to decide what an entry was. Entries written by earlier releases record no
kind: the engine knows what one was only when the cohort contains its balance
transaction unchanged (it matches the saved financial digest). A proven older
fee, payout, reserve or transfer movement owns nothing; a proven older sale
reserves its charge, but the payment intents and invoices it owned are never
known. Any other older entry makes ownership unproven. So while the ledger holds
an older Stripe entry whose kind cannot be proven, every new sale and every
refund is held with `ownership_unproven`; with only proven older sales, a new
sale is held when it names a payment intent or an invoice, and a refund of an
older sale is held. An exact replay of an older entry still posts nothing. There
is no backfill.

A refund posts to the refund account only behind narrow proof of its sale, found
through the charge's `balance_transaction` (or the one sale movement of the
charge in the cohort). Either (a) this batch has an approved (not held) sale
action with the same charge, payment intents and invoice, or (b) the ledger has
one live sale entry this engine wrote, never corrected or reversed, whose
recorded charge, payment intents and invoices equal the refund's current
evidence exactly, and whose recorded evidence digest (`stripe-closure`: the
charge, payment intent, invoice or Checkout session and allocation that decided
the sale) the refund's capture reproduces. A capture that contradicts the sale is
checked first and rejects either proof: the sale movement changed, is refused or
is no longer available (`sale_not_posted`, `sale_movement_changed`). Anything
else posts the refund to the holding account with a reason: the sale is held
(`sale_in_holding`), corrected or reversed in any way (`sale_corrected`), its
links or evidence changed (`sale_links_changed`, `sale_evidence_changed`), it is
absent, a payout or not a sale this engine wrote (`sale_movement_unverified`), or
it was written by an earlier release (`ownership_unproven`). A refund that names
another payment intent than its charge is held with `refund_reference_conflict`.

A refund posts once. Each refund entry records its own provider ID
(`stripe-refund`); a refund ID that any ledger entry already recorded never posts
again, whatever movement carries it (`refund_already_recorded`), and two
movements in one batch with the same refund ID both stay unposted
(`duplicate_refund_movement`). The refundable amount is the sale's booked gross
minus every refund and dispute already posted (held, corrected or reversed alike)
or proposed in this batch for the charge; a credit back, such as a won dispute,
never raises it. A refund over that stays unresolved with
`refund_exceeds_booked_sale`. A refund or dispute entry that does not record its
charge makes every later refund unproven (`ownership_unproven`), because what it
consumed cannot be attributed. A refund of a sale with more than one invoice line
stays unresolved with `mixed_line_refund_requires_allocation`. Disputes always
post to the holding account, because no approved policy names a dispute account.

Revenue authority comes from one checked path. A plain charge (no invoice or
Checkout session) is mapped only by metadata rules; a plain charge that carries a
price or product selector (`price`, `price_id`, `product_id` and the like) is held
with `plain_charge_price_unsupported`, because only invoice and session lines pass
the catalog's mode, currency and copy checks. A refund, dispute or Checkout
session in another mode or currency never adds evidence to a sale's closure. Copies
of one payment intent must agree on its status, and a payment intent that did not
succeed holds (`payment_intent_not_succeeded`).

A known sale whose allocation is not complete (a missing revenue rule, tax,
discount, proration, partial payment, truncated lines) or a movement that has
not settled stays unposted in `unresolved`, as before.

### Correcting a held posting

The engine never moves a held entry out of the holding account by itself, and a
held refund is never released later: a held sale or refund stays held until a
person corrects it. Do not edit, delete or re-import the held entry. Check the
evidence, then correct it through the ordinary ledger correction path
(`books ledger correct`), which reverses the held entry and writes the reviewed
replacement in one audited write. Capturing the missing evidence later changes
nothing: the held movement is already posted and is skipped as `already_posted`.
The correction needs its own replacement source ID (the writer refuses one
without). The sale's claims stay reserved after the correction, and the
replacement adds any it names. A refund of a corrected sale is never posted
automatically: it is held with `sale_corrected` for the same person.

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
does not clone the source or reverse an already reversed entry. When managed
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
Disputes and sales of uncertain identity post to the holding account instead.
Refunds must also pass the original sale's evidence checks; mixed-line refunds
need explicit allocation and are held. Clearing roles are not available bank
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
fingerprint reads the same resolved objects as allocation (an expanded price inside
pricing details included) and binds every document in the closure by its whole
content: a document with a field outside the fixed projection (a charge's price
selector, a payment intent's status) is bound in full. A refund's fingerprint also
binds its sale movement: its financial identity whenever that differs from what the
books recorded, its status when it is not `available`, any other field it carries,
and its absence from the capture. A resolution that stops early (an unsettled or
unallocated sale) has an incomplete fingerprint, which counts as unavailable. Evidence
that cannot be fingerprinted is never a match: a new movement stays unresolved with
`capture_fingerprint_unavailable`, and cleanup of a posted one needs review. Changed or
unproven evidence requires reconciliation before cleanup. This holds the one row, not the batch: the
row is reported as unresolved with the reason `cleanup_needs_review`, it stays in
Review, and the other rows in the batch still post. A source that an older engine
posted without a receipt is cleaned up only when its saved source digest, its policy
digest and its staged row all match the current plan; if they do not match, it gets
the same reason.

`stripe_apply` returns the held rows. `unresolved` lists every row held unposted as
`{source_id, reason}`. Rows posted to the holding account are not in `unresolved`:
they are posted (counted in `posted`) and listed in `uncategorized` as
`{source_id, reason}`. Review both lists. `needs_review` lists only the cleanup rows as
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
`no_financial_effect` separately from `posted`. Replays verify the source and
evidence before finishing queue cleanup; changed evidence remains held.
