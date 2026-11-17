# Who is calling: caller kind

A host that lets an automated agent run `books` commands must say so. Otherwise
the engine records the agent's values as stated by a person.

## Kinds

| Kind | Meaning |
| --- | --- |
| `person` | A person decided or typed the value. This is the default. |
| `agent` | An automated agent supplied the value. |
| `system` | An automatic job supplied the value. |
| `unknown` | The host set a value that is not one of these kinds. |

## How to set it

- Set the environment variable `BOOKS_CALLER_KIND` for the commands the host starts.
- In Python, wrap the work in `bookkeeping.provenance.use_caller_kind(kind)`. It
  wins over the environment variable and resets when the block ends, also after an error.
- With nothing set, the kind is `person`, so existing scripts and stored records do
  not change. An invalid value is `unknown`.

The kind never comes from file content or command arguments. An imported file or an
agent's own arguments cannot choose it.

## What the engine records

- Statement import stores `caller_kind` on the statement record. A period or balance
  that the caller states (`--period-start`, `--period-end`, `--opening-balance`,
  `--closing-balance`) gets the kind as its source: `period_source` and
  `balance_source` are `agent`, or `agent_and_file` when the file gives the other
  balance. The result of the command has `caller_kind`.
- Only a person confirms a period. A period stated by any other kind is stored with
  `period_confirmed: false`, does not count as coverage, and waits for a person. A
  period that a person confirmed earlier is kept.
- An extracted statement: a person caller gets a confirmed period. Another kind gets a
  confirmed period only when the extraction writes `"period_source": "printed"` (the
  only value that counts as printed; `derived` and a missing value do not). Otherwise
  the record is stored unconfirmed, with a warning and `coverage: needs_period`. The
  `confirmation` field in the extraction JSON never confirms a period for another kind.
  Trust boundary: the extraction JSON comes from the host's extractor, and the engine
  trusts its `period_source` the same way it trusts the dates printed in an OFX or CSV
  file. A host that lets an agent write the extraction JSON itself must set the caller
  kind and must not let that agent claim a printed period.
- Saved statement profiles, statement review records and extracted statement records
  store `caller_kind`.
- A typed opening balance has `opening_source` set to the kind (`opening-source` in
  the entry metadata), and the result has `caller_kind`.
- An audit event written by a caller that is not a person includes `caller_kind`
  inside the hash chain. An event with no kind is a person event, so a default
  run writes the same audit events as before. Older events still verify.
- Reading a stored record returns the stored kind, not the kind of the reader.
