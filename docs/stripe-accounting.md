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

Use an explicit historical opening date to prepare separate USD processor,
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

The engine stages captured USD processor sources without learned auto-posting,
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
| Processor fee | Processor balance decreases; processing expense increases |
| Payout | Processor balance decreases; payout transit increases |
| Exact captured bank arrival | Bank balance increases; payout transit decreases; no income |

Bank matching requires the original bank source ID/digest, payout ID, exact
currency/amount and approved current bank mapping. A bank arrival can occur after
the payout in another period. An old net bank deposit already booked as income
blocks application until a normal audited correction resolves it; the engine
does not clone the source or reverse an already reversed entry. When managed
Stripe accounting exists, the hosted routine planner holds positive bank credits
with a Stripe label as suspected payouts. A label is suspicion, not proof of a
specific payout; unrelated approved routine items can continue.

## Explicit limits

This slice posts USD only. Disputes, FX, unsupported balance types, unsettled
charges/refunds, incomplete lines, partial/multiple invoice payments and
unsupported tax, shipping, discounts, credits or prorations remain held.
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
invoice, refund, payout or other relevant authority evidence still block cleanup. Changed or unproven evidence
requires reconciliation before cleanup.
