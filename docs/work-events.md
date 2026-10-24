# Work events

A work event is a typed record of books work, in counts. Each count refers to
the rows behind it. For example: "Imported 212 transactions", "Recorded 14
entries", "Reconciled one account through 2026-10-12".

The engine computes work events from the actual change to the company books.
It reads the books before a command and again after the command, and it
compares the two states. The request text, the reasoning text, and the agent
narrative never change a count. If a command changes nothing, it gives an
empty list.

The counts, the breakdown, the accounts and the subjects of a work event never
contain money. An optional `amounts` field lists the amounts and accounts that
the engine recorded (see "Amounts" below), so a host can write "matched a
transfer of 500.00 between A and B" without any math.

- Schema: [`docs/work-event.schema.json`](work-event.schema.json) (JSON Schema
  2020-12).
- Validator: `bookkeeping.work.validate_event(event)` (no dependencies).
- Examples: `tests/fixtures/work/valid-events.json`,
  `tests/fixtures/work/invalid-events.json`, and
  `tests/fixtures/work/golden-events.json` (the events that each command gives
  in a synthetic company).

## Get the events for a command

Add `--work-out PATH` to a command that changes local books:

```sh
books --work-out work.json queue confirm --entity . --item src-123
```

The command prints its usual output. The engine writes this document to
`PATH`:

```json
{"schema_version": 1, "work": [ { "kind": "recorded", ... } ]}
```

`--work-out` does not change the output or the behavior of the command. It
writes the document also when the command fails, because a failed command can
change state (for example, a partial import). A read-only command gives
`"work": []`. A command for a remote company binding, and the `cloud` and
`hosted` commands, reject `--work-out`.

A host that runs the engine as a command, for example in a sandbox, treats
`--work-out` as an output file. The host sends `--work-out @output/work.json`.
The command contract (`remote_contract.command_spec`) lists `work_out` in
`output_fields`, and `rewrite_paths` replaces the placeholder with a real path.
The host reads the file after the command and puts its `work` list in the
result of that command. The engine does not print the events in its normal
output, so the output of a command does not change when a host asks for them.

A host that embeds the engine in Python wraps one command:

```python
from bookkeeping import cli, work

with work.capture(company_dir) as receipt:
    code = cli.main(argv)
events = receipt.events
```

`work.capture` writes no file. It reads `ledger.sqlite` in read-only mode.

## Event shape

| Field | Set by | Meaning |
|---|---|---|
| `schema_version` | engine | Always `1`. |
| `kind` | engine | One of the kinds below. |
| `period` | engine | `{from, to}`: the first and the last books date that the event touches, or `null`. |
| `accounts` | engine | Engine account names, sorted, at most 20. For entries, the funding (first posting) account. |
| `counts` | engine | Counts for the kind. Each count is a non-negative integer. |
| `breakdown` | engine | At most 5 `{label, count}` rows, by count and then label. Labels are engine account names or treatment names such as `Split transaction`. |
| `subjects` | engine | Stable references to the rows behind the counts. The engine lists every subject. |
| `amounts` | engine | Optional. At most 200 rows with the amount and accounts that the engine recorded for each suggestion, entry or reconciliation behind the event. Not part of the fingerprint. |
| `amounts_truncated` | engine | `true` only when more than 200 rows exist. |
| `fingerprint` | engine | SHA-256 of the canonical JSON of the engine fields, without `amounts` and `amounts_truncated`. |
| `derived` | engine or host | `true` only for events rebuilt from the current state. |
| `actor`, `via`, `run`, `command_id`, `occurred_at`, `visibility`, `subjects_key` | host | Who did the work, through which surface, in which run, and when it was committed. The engine never sets these fields. |

Subject references:

| `type` | `id` |
|---|---|
| `source` | The source transaction ID. |
| `entry` | The ledger entry row ID in `ledger.sqlite`. The optional `source_id` gives the source ID of the entry. |
| `suggestion` | The review queue item ID. |
| `reconciliation` | The reconciliation record ID (`ACCOUNT@YYYY-MM-DD`). |
| `evidence`, `question`, `publication`, `email` | Reserved for hosts and later engine commands. |

## Amounts

`amounts` is an additive field. It is on the `categorized`, `recorded`,
`corrected` and `reconciled` events. A caller that does not read it sees no
change. Each row has the subject reference (`type`, `id`, optional `source_id`),
`accounts` in posting order, and Decimal strings (never numbers):

| Row for | `amount` | `accounts` | Other fields |
|---|---|---|---|
| A suggestion (`categorized`, withdrawn in `corrected`) | The absolute amount of the source transaction, or of the transfer pair. | The proposed category; the allocation accounts of a split; both accounts of a transfer pair. | |
| An entry (`recorded`, `corrected`) | The absolute amount of the funding (first) posting, only for an entry of one bank or card row (two postings, or a reviewed split). An accrual journal or another multi-line entry has no `amount`. | Every account the entry posts to. A same-day transfer pair is one entry between its two accounts. | |
| A reconciliation (`reconciled`) | | The reconciled account. | `ledger_balance`, `source_balance` and `discrepancy`, signed as recorded. |

Accrual adjustments, `ledger correct` and similar commands write ledger
entries, so their amounts appear on `recorded` or `corrected` rows. The text
output of every command does not change. Derived events (`books work`) carry no `amounts`, so its output does not change either. `summarize` ignores `amounts`, so a
summary line never contains money.

## Kinds

The engine produces these kinds:

| Kind | Counts | Produced when |
|---|---|---|
| `imported` | `transactions`, `duplicates_skipped` | A source transaction arrives in the books: it is staged for review or posted. `duplicates_skipped` counts rows that the same import skipped. |
| `categorized` | `suggestions` | A review suggestion is new or changed and waits for review (`queue propose`, `propose-group`, `propose-split`, `propose-transfer`, `propose-related`). |
| `recorded` | `entries`, `from_suggestions`, `direct` | New ledger entries that are not corrections. `from_suggestions` counts entries from approved suggestions. `direct` counts entries posted without a suggestion, for example by a learned rule during an import. |
| `corrected` | `entries`, `reversals`, `withdrawn` | Replacement entries (`queue correct`, `ledger correct`, `reclassify-bank`, `reclassify`, `split-equity`), reversal entries, and withdrawn suggestions. |
| `reconciled` | `accounts`, `clean`, `open` | A reconciliation record is new or changed in substance. A resolved record counts as `clean`. |
| `flagged` | `duplicates` | A possible duplicate waits for a decision. |

The schema also defines kinds that a host or a later engine version produces:
`matched_receipt` (`documents`), `asked_question` (`questions`),
`applied_answer` (`answers`, `entries`), `published` and `reopened`
(`months`). The `flagged` kind also accepts `transfer_exceptions` and
`held_emails`.

Each new ledger entry is counted one time:
`recorded.entries + corrected.entries + corrected.reversals` equals the number
of new rows in the ledger.

## Determinism and replay

- The same books before a command and the same command give the same events,
  in the same order, with the same fingerprints.
- A replayed command that changes nothing gives an empty list. For example, a
  second import of the same file, the same suggestion again, or the same
  reconciliation again.
- Timestamps that a command writes (such as `updated_at`) do not change the
  events.

## Summary: `books work`

```sh
books work --entity . --from 2026-10-01 --to 2026-10-31
books work --entity . --format json
```

The engine does not keep a history of work per command. `books work` rebuilds
the work from the current books, limited to rows dated inside the window. The
window is the books date, not the time of the work, and each event has
`derived: true`. The text output gives one plain line for each kind.

## Host integration

A host that shows "what was done this week" must store the events when it
commits each command, because the engine cannot rebuild the time of the work
later. For each committed command, the host must:

1. Take `work` from the command result (`work.capture`).
2. Validate each event with `validate_event(event, engine=True)`.
3. Add the host fields (`actor`, `via`, `run`, `command_id`, `occurred_at`,
   `visibility`). Set these from its own authentication, never from the
   request.
4. Store the events in the same transaction as the commit. Use the command ID
   and the kind as a unique key, so that a replayed command stores nothing new.
5. If an event has more than 200 subjects, store the full list elsewhere and
   set `subjects_key`.

To aggregate events, add counts and merge breakdowns and accounts. Never add
the amounts of different events: a suggestion and the entry that records it
show the same money. `bookkeeping.work.summarize(events)` is a reference implementation.

## Add a new state source

To make a new command produce work events, the command must change state that
the snapshot reads. The snapshot reads the ledger entries, the staged
transactions, the review queue, the reconciliation records, and the duplicate
candidates. A command that writes other state (for example statement coverage
records) needs one new snapshot field in `take_snapshot` and one builder in
`work_events`. A command that imports through `import_transactions` gives
`imported` events without a change.
