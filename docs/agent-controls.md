# Caller controls for connected agents

A host that runs the engine for a connected AI agent can set environment
variables to apply controls to one command. The variables are not authority by
themselves. The host sets them for the process that runs the command, and it
must not let the agent change them. They only add restrictions. The engine ignores these three variables in a `.env` file, so only the real process environment can set them.

| Variable | Effect |
|---|---|
| `SLASHBOOKS_ACTOR` | An opaque actor string. Each new suggestion records it as `created_by`. At most 200 characters, with no control characters and no leading or trailing whitespace. An invalid value stops the command before any write (`invalid_actor`). |
| `SLASHBOOKS_FORBID_SELF_APPROVAL` | When set to `1`, a command that approves a suggestion refuses if `SLASHBOOKS_ACTOR` equals the suggestion's `created_by`. It also refuses (`invalid_actor`) when `SLASHBOOKS_ACTOR` is not set. |
| `SLASHBOOKS_ENFORCE_ASK_FIRST` | When set to `1`, `queue propose*`, `confirm*` and `correct` refuse an item above the ask-first amount (`above_ask_first_amount`). |

## Settings a person applies

Some commands change a company setting, not a transaction. An agent can run the
preview and show it. A person runs the apply step. Standing posting authority
(see [standing policy](standing-policy.md)) never covers it, and the trusted
preparation runtime refuses it.

| Command | What it sets |
|---|---|
| `books entity set-currency PATH --currency CODE --apply` | The company currency, only while the company has no entries. See [company currency](company-currency.md). |
| `books entity set-view PATH --view cash\|accrual --apply` | The default report view. |
| `books accrual enable apply` | Accrual tracking from a tracking start date. `enable preview` is read-only. |
| `books entity set-fiscal-year PATH --start MM-01 --apply` | The fiscal-year start. |
| `books entity set-tax-method PATH --method cash\|accrual --apply` | The tax method. The tax outputs follow it. |
| `books entity set-tax-profile PATH ... --apply` | The prior return method and the entity type. |
| `books accrual schedule tax-treatment ... --apply` | How a cash-method filer deducts a prepaid schedule. |
| `books accrual schedule\|item deposit-kind ... --apply` | Whether a deposit is an advance payment or refundable. |

Without `--apply` these commands preview and write nothing. `books entity
infer-currency` only reads files, so an agent can run it. Closing and reopening a
period also need a person. See [the tax handoff](tax-handoff.md) for the tax
settings and [accrual books](accrual.md) for accrual tracking and the default view.

## Record that onboarding is complete

`books entity onboarding-complete PATH --via agent|meeting|staff [--on YYYY-MM-DD]`
records that the company finished onboarding. It writes `onboarding_completed`
(`on`, `via`) to `entity.json` only. It changes no ledger data, no other setting
and no queue item. A valid first record is kept: a second run prints
`already_complete` and writes nothing. `--on` defaults to the machine date
(`date.today()`), because the engine keeps no company time zone. A date after
today (the later of the machine date and the UTC date) is refused.

With local books, the books-onboard skill runs it with `--via agent` when
onboarding is done. A host can limit who records it. Inside Slashbooks Cloud,
only the setup meeting, staff and a person's own connected agent can record
completion. Slashbooks' own hosted automation cannot, so a hosted run cannot
mark its own company as set up. Outside Cloud, `--via agent` works as written.

## Who made a suggestion

`books queue propose`, `propose-group`, `propose-split`, `propose-transfer`,
`propose-intercompany` and `propose-related` read `SLASHBOOKS_ACTOR`. When it is
set, the queue item stores it in `created_by`. The field is absent when no actor
was set. A later proposal for the same item replaces the creator with the actor
of that proposal, because that actor wrote the content that is now waiting.

`books queue show` prints `created_by` in the item. `books queue list --json`
prints every item as `{"schema_version": 1, "kind": "queue_items", "count": N,
"items": [...]}`. The text output of `queue list` does not change.

## Refuse self-approval

With `SLASHBOOKS_FORBID_SELF_APPROVAL=1` and `SLASHBOOKS_ACTOR` set,
`queue confirm`, `confirm-group`, `confirm-split`, `confirm-transfer` and
`queue correct` refuse when the actor equals the item's `created_by`. The
refusal happens before any write. The command exits with code 1 and prints this
JSON on standard error:

```json
{"error": {"code": "self_approval", "message": "..."}}
```

An item with no `created_by` is not affected when an actor is set. With the control on and no actor set, the command refuses with `invalid_actor` (fail closed).
`confirm-group` checks every item first, so a refusal leaves no partial group.

## Ask first above an amount

The ask-first amount is a limit above which a person decides. It applies to the
absolute amount, so a payment of -900.00 and a receipt of 900.00 are the same.
An amount equal to the limit is not above it.

Set the limit in one of two places:

- `ask_first_above` in the company's `trust-policy.json`, for example
  `{"ask_first_above": "500.00"}`. A `null` value means no limit.
- `ask_first_above` in a version-1 standing policy
  (`StandingPolicy`, see [standing policy](standing-policy.md)). It applies to
  every command that runs inside the trusted preparation boundary.

When both are set, the lower amount applies. The setting only adds holds. A
setting that cannot be read (for example `"lots"` or a negative number) holds
every non-zero amount.

**Import.** `queue.eligible_for_autopost` returns false for a row above the
limit, so a learned category or a saved rule does not post it. The row goes to
Review, like a row that waits for accrual treatment. The staged row gets
`review_hold_reason: "above_ask_first_amount"`. A row without a readable amount
waits as well. A row above the limit with no matching rule is reported as `above_ask_first_amount`, not `needs_category`. A routine plan reports a rule match above the limit as
unresolved with the reason `above_ask_first_amount`.

**Stripe.** `stripe_plan` lists a row above the limit as unresolved with the reason `above_ask_first_amount`. `stripe_apply` does not record it.

**Propose and approve.** Inside the trusted preparation boundary the limit is
always enforced, and the lower of the company and standing-policy limits applies. Outside it, the limit is enforced when
`SLASHBOOKS_ENFORCE_ASK_FIRST=1`. Then `queue propose`, `propose-group`,
`propose-split`, `propose-transfer`, `confirm`, `confirm-group`,
`confirm-split`, `confirm-transfer` and `correct` refuse an item above the
limit before any write. The command exits with code 1 and prints
`{"error": {"code": "above_ask_first_amount", "message": "..."}}` on standard
error. A split compares the amount of its source transaction. A transfer pair
compares the absolute amount of the pair. `confirm-group` and `propose-group`
check every item first.

**Routes that do not check the limit.** Routine captured decisions (a person
decided), `backtest` category rules, corrections of posted entries
(`ledger correct`, `reclassify-bank`, `split-equity`) and the `accrual` commands
do not apply the limit. A payout bank deposit matched by `stripe_plan` is also not checked.

## Read the amount of an item

`bookkeeping.queue.amount_for_item(entity, item_id)` returns the absolute
`Decimal` amount that an item decides, or `None` when the amount cannot be
read. Treat `None` as above the limit. It raises `FileNotFoundError` when the
item does not exist. `queue.ask_first_above(entity)` returns the effective
limit or `None`. `queue.amount_is_above_ask_first(entity, amount)` and
`queue.txn_is_above_ask_first(entity, txn)` apply the rule. A host does not need
the private queue functions.

## Amounts in command results

A host reads the result of a command from the work events (`--work-out`, see
[work events](work-events.md)). Each `categorized`, `recorded`, `corrected` and
`reconciled` event has an optional `amounts` list with the amount and accounts
that the engine recorded, as Decimal strings. A row for an entry has an `amount` only when the entry records one bank or card row (two postings, or a reviewed split). An accrual journal or another multi-line entry keeps its `accounts` and has no `amount`. A host can write "matched a
transfer of 500.00 between A and B" without math. The text output of the
commands does not change.
