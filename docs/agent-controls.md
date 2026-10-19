# Caller controls for connected agents

A host that runs the engine for a connected AI agent can set environment
variables to apply controls to one command. The variables are not authority by
themselves. The host sets them for the process that runs the command, and it
must not let the agent change them. They only add restrictions.

| Variable | Effect |
|---|---|
| `SLASHBOOKS_ACTOR` | An opaque actor string. Each new suggestion records it as `created_by`. At most 200 characters, with no control characters. An invalid value stops the command before any write (`invalid_actor`). |
| `SLASHBOOKS_FORBID_SELF_APPROVAL` | When set to `1`, a command that approves a suggestion refuses if `SLASHBOOKS_ACTOR` equals the suggestion's `created_by`. |

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
