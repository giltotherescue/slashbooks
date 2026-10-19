# Caller controls for connected agents

A host that runs the engine for a connected AI agent can set environment
variables to apply controls to one command. The variables are not authority by
themselves. The host sets them for the process that runs the command, and it
must not let the agent change them. They only add restrictions.

| Variable | Effect |
|---|---|
| `SLASHBOOKS_ACTOR` | An opaque actor string. Each new suggestion records it as `created_by`. At most 200 characters, with no control characters. An invalid value stops the command before any write (`invalid_actor`). |
| `SLASHBOOKS_FORBID_SELF_APPROVAL` | When set to `1`, a command that approves a suggestion refuses if `SLASHBOOKS_ACTOR` equals the suggestion's `created_by`. |
| `SLASHBOOKS_ENFORCE_ASK_FIRST` | When set to `1`, `queue propose*`, `confirm*` and `correct` refuse an item above the ask-first amount (`above_ask_first_amount`). |

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

An item with no `created_by`, or a command with no actor, is not affected.
`confirm-group` checks every item first, so a refusal leaves no partial group.

## Ask first above an amount

The ask-first amount is a limit above which a person decides. It applies to the
absolute amount, so a payment of -900.00 and a receipt of 900.00 are the same.
An amount equal to the limit is not above it.

Set the limit in one of two places:

- `ask_first_above` in the company's `trust-policy.json`, for example
  `{"ask_first_above": "500.00"}`.
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
waits as well. A routine plan reports a rule match above the limit as
unresolved with the reason `above_ask_first_amount`.

**Propose and approve.** Inside the trusted preparation boundary the limit is
always enforced. Outside it, the limit is enforced when
`SLASHBOOKS_ENFORCE_ASK_FIRST=1`. Then `queue propose`, `propose-group`,
`propose-split`, `propose-transfer`, `confirm`, `confirm-group`,
`confirm-split`, `confirm-transfer` and `correct` refuse an item above the
limit before any write. The command exits with code 1 and prints
`{"error": {"code": "above_ask_first_amount", "message": "..."}}` on standard
error. A split compares the amount of its source transaction. A transfer pair
compares the absolute amount of the pair. `confirm-group` and `propose-group`
check every item first.

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
that the engine recorded, as Decimal strings. A host can write "matched a
transfer of 500.00 between A and B" without math. The text output of the
commands does not change.
