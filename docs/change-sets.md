# Change sets

A change set is a typed record of what one ledger write changed. It lists the
rows that the write added, the rows that it changed, and the position of the
books before and after. A host that keeps its own view of the books (for
example a database of screen rows) applies the change set to that view. The host
does not read the whole books again after each write, so the cost of a write
depends on the size of the change.

The engine builds a change set from the actual state of the company before and
after the command. The request text and the agent text never change a change
set. The engine does no accounting in a change set: amounts are the strings that
the ledger holds, and no field is a total.

- Contract version: `schema_version` is `1`.
- Reference code: `bookkeeping.changeset` (`capture`, `empty_view`,
  `apply_change_set`, `read_view`). It has no dependencies.
- Related: work events (`docs/work-events.md`) count the work for a person to
  read. A change set carries the rows for a host to store. The two outputs are
  independent.

## Get the change set for a command

Add `--change-set-out PATH` to a command. The engine writes one JSON document to
`PATH` after the command ends:

```sh
books --change-set-out change.json queue confirm --entity . --item src-123
```

- The command prints its usual output. `--change-set-out` does not change the
  output or the behavior of the command.
- The engine writes the document also when the command fails or raises. A
  command is not one transaction, so a failed command can leave some of its
  writes. A change set of a failed command that left a change has
  `"complete": false`. A host never applies it and never publishes it. The host
  does a full read (`read_view`). A failed command that changed nothing gives a
  complete, empty change set.
- A read-only command gives a change set with `"changed": false` and empty
  lists. A command that has no company directory also gives that empty change
  set.
- A command for a remote company binding, and the `cloud` and `hosted`
  commands, reject `--change-set-out`.
- `--change-set-revision N` is optional. The engine copies the integer `N` into
  `books_revision`. The engine has no revision counter of its own. Its position
  is the audit chain head, and `ledger.before` and `ledger.after` give it.
- A host that runs the engine in a sandbox sends `--change-set-out
  @output/change.json`. The command contract (`remote_contract.command_spec`)
  lists `change_set_out` in `output_fields`.

A host that embeds the engine in Python wraps one command:

```python
from bookkeeping import changeset, cli

with changeset.capture(company_dir, books_revision=41) as receipt:
    code = cli.main(argv)
document = receipt.change_set
```

`capture` also wraps a Python call that is not a command, such as
`queue.set_taught`. It writes no file. It opens `ledger.sqlite` read-only.

## Document shape

```json
{
  "schema_version": 1,
  "kind": "books-change-set",
  "books_revision": 41,
  "complete": true,
  "reason": null,
  "state": {"before": "5e1a...", "after": "77c0..."},
  "changed": true,
  "reset": false,
  "ledger": {
    "before": {"audit_event_id": 17, "record_hash": "ab12...", "max_entry_id": 9, "max_posting_id": 21, "max_balance_assertion_id": 0},
    "after":  {"audit_event_id": 18, "record_hash": "cd34...", "max_entry_id": 10, "max_posting_id": 23, "max_balance_assertion_id": 0}
  },
  "entries_added": [
    {"id": 10, "date": "2026-10-01", "flag": "*", "payee": null, "narration": "Design tool",
     "tags": [], "links": [], "metadata": [["source-id", "syn-001"]],
     "source_id": "syn-001", "session": "s1", "late_arrival": false,
     "postings": [{"id": 22, "account": "Assets:Bank:Operating-Checking", "amount": "-54.99", "currency": "USD", "metadata": []}]}
  ],
  "entries_reversed": [
    {"reversal_entry_id": 11, "reverses_source_id": "syn-001", "reverses_entry_id": 10, "replacement_entry_ids": [12]}
  ],
  "balance_assertions_added": [
    {"id": 1, "date": "2026-10-31", "account": "Assets:Bank:Operating-Checking", "amount": "100.00", "currency": "USD"}
  ],
  "accounts": {
    "opened": [{"name": "Expenses:Software", "currency": "USD", "open_date": "2000-01-01", "financial_kind": null}],
    "renamed": [{"from": "Expenses:Office", "to": "Expenses:Workspace", "audit_event_id": 19}],
    "updated": [{"name": "Expenses:Fees", "currency": "USD", "open_date": "2000-01-01", "financial_kind": "expense"}],
    "removed": ["Expenses:Old"]
  },
  "bank_rows": [
    {"source_id": "syn-001", "change": "added", "status_before": null, "status_after": "posted", "imported_at": "2026-10-01T10:00:00Z", "payload_sha256": "3b4f...", "row": {"id": "syn-001", "...": "..."}}
  ],
  "staging": {
    "pending": {"added": [], "updated": [], "removed": ["syn-001"]},
    "duplicates": {"added": [], "updated": [], "removed": []},
    "store_pending": {"added": [], "updated": [], "removed": []},
    "seen_ids": {"added": [], "removed": []}
  },
  "review_queue": [
    {"id": "syn-001", "change": "updated", "status_before": "open", "status_after": "confirmed", "closed": true, "item": {"source_id": "syn-001", "status": "confirmed", "...": "..."}}
  ],
  "learned_context": [
    {"counterparty": "DESIGN TOOL", "change": "updated", "entry": {"canonical_category": "Expenses:Software", "confirmed_count": 1, "...": "..."}}
  ],
  "audit": {
    "first_event_id": 18, "last_event_id": 18, "count": 1,
    "events": [{"id": 18, "type": "ledger-store-sealed", "ts": "2026-10-01T10:00:00Z", "record_hash": "cd34..."}]
  },
  "fingerprint": "9f2c..."
}
```

### Fields

| Field | Meaning |
|---|---|
| `schema_version` | Always `1`. A new version changes the number. A reader that sees a number that it does not know must stop. |
| `kind` | Always `books-change-set`. |
| `books_revision` | The integer from `--change-set-revision`, or `null`. |
| `complete` | `false` when a host must not apply the change set: the command failed after it changed something, or the engine could not read the state in full. `reason` is then `command_failed` or `state_unreadable`. A host does a full read. |
| `state.before`, `state.after` | SHA-256 of the canonical JSON of the small tables (accounts, bank row index with payload hashes, review queue, staging, learned context) before and after the command. |
| `changed` | `false` when nothing in the document changed. All lists are then empty and `ledger.before` equals `ledger.after`. |
| `reset` | `true` when the ledger store is not an extension of the earlier store (for example a restore or a migration replaced it). The host clears its entries and balance assertions, then applies the lists. In that case `entries_added` and `balance_assertions_added` hold every row. |
| `ledger.before`, `ledger.after` | The position of the books. `audit_event_id` and `record_hash` are the newest audit event (`null` when none). The three `max_*_id` values are the newest row ids. |
| `entries_added` | Each new ledger entry with all its fields and its postings, in id order. Ledger rows are append-only, so a row never changes after it is added. A posting `account` changes only for a rename (see `accounts.renamed`). |
| `entries_reversed` | One row for each new reversing entry. `reverses_source_id` is the source id that the reversal names. `reverses_entry_id` is the id of the original entry (`null` if the engine cannot find it). `replacement_entry_ids` lists the new entries that name the same source id in `correction-of`. |
| `balance_assertions_added` | Each new balance assertion. |
| `accounts.opened` | New accounts in the catalog. |
| `accounts.renamed` | One row for each old and new account name, from the audit event `account-renamed`. The host changes the account name in its accounts, postings and balance assertions. |
| `accounts.updated` | An account whose currency, open date or financial kind changed and whose name did not. |
| `accounts.removed` | Account names that left the catalog. |
| `bank_rows` | A source transaction (a bank row) that was added (`added`), changed its status (`status-changed`), changed in another way, such as a new payload (`updated`), or left the table (`removed`). `payload_sha256` is the hash of the stored payload. `status_before` is `null` for an added row. `status_after`, `imported_at` and `row` (the stored row) are `null` for a removed row. |
| `fingerprint` | SHA-256 of the canonical JSON of the other fields. A host uses it to find a change set that it already applied. |
| `staging.pending` | Rows that wait for a category (`staging/pending-categorization.json`): new rows (`added`), changed rows (`updated`, whole row) and ids that left the list (`removed`). |
| `staging.duplicates` | The same three lists for duplicate candidates. The key is `source_id`. |
| `staging.store_pending` | The same three lists for the intake store rows in `staging/pending.json`. |
| `staging.seen_ids` | Source ids that the intake store added to or removed from `staging/seen-ids.json`. |
| `review_queue` | One row for each review item that was created, changed or removed. `id` is the item `source_id`. `change` is `created`, `updated` or `removed`. `status_before` is `null` for a created item. `status_after` is `null` for a removed item. `closed` is `true` when `status_after` is not an open status (`open`, `reopened`, `requires-reproposal`). `item` is the whole item (`null` for a removed item). |
| `learned_context` | One row for each payee key whose learned context changed, with the whole `entry`. A taught category is in `entry.taught` (`id`, `category`, `at`, `by`). `change` is `created`, `updated` or `removed`. |
| `audit` | The audit events that the command added: first id, last id, count, and for each event its id, type, time and record hash. All values are `null` or empty when none. |

Lists are sorted: entries by id, accounts by name, bank rows by source id,
queue items by id, learned context by key, audit events by id.

## Apply a change set

The host owns the revision number. Each change set carries the position of the
engine before and after it: `ledger.before` and `ledger.after` (the audit chain
head and the newest row ids) and `state.before` and `state.after` (the digest of
the small tables). A host that applies change sets in order always has a view
whose position and digest equal the `before` values of the next change set.

The reference reducer `changeset.apply_change_set(view, change_set)` turns a
host view into the next view. Start from `changeset.empty_view()`. It refuses
the change set with `ChangeSetRefused` in these cases:

- `complete` is `false`.
- `ledger.before` is not the position of the view, or `state.before` is not the
  digest of the view (`changeset.view_digest(view)`). This rejects a stale
  replay, a skipped change set and a change set that arrives out of order. A
  change set that only changes the review queue or learned context is also
  refused when it is applied twice, because the digest changes.
- After the apply, the position or the digest of the new view is not
  `ledger.after` or `state.after`.

A refused change set means that the host does a full read (`read_view`) and
starts again from that view. The reducer does not change the view that it
gets. A host that keeps `state.after` of the last change set can compare it with
the next `state.before` and does not have to compute the digest again.

The reducer works in this order:

1. If `reset` is `true`, clear `entries` and `balance_assertions`.
2. Apply `accounts.renamed` to the accounts, the postings and the balance
   assertions that the view holds.
3. Add `entries_added`, `balance_assertions_added` and `accounts.opened`. Replace
   `accounts.updated`. Delete `accounts.removed`.
4. Set or replace `bank_rows`, `staging` rows, `review_queue` items and
   `learned_context` entries. Delete the removed ones.
5. Set the audit position from `ledger.after` and add the audit events.

`changeset.read_view(company_dir)` reads the whole books into the same view
shape. The engine test applies the change sets of a full sequence of writes to
an empty view, and the result equals `read_view` after each write. The test also
compares the view with the books through other readers of the engine, and it
fails when a file appears in `staging/` or `learned-context/` that the change
set does not cover.

## Cost

- The ledger entries, postings, balance assertions and audit events come from
  row ids above the saved position. Their cost is the size of the change.
- The accounts table, the source transaction list (`id`, `status`, time), the
  review queue, the staging lists and the learned context are compared before
  and after. Their cost grows with the number of rows in the company directory
  that the command sees. A host that gives the engine a working set of rows
  pays for that working set only.
- The review queue is read with the queue module (`queue.load_all_items`): the
  item files and the month archives in `review-queue/archive/`. The change set
  code has no archive reader of its own. The reader refuses a corrupt archive or
  an archive version that it does not know, and it reads item files before
  archives, so a compaction that runs during the read does not hide an item.
  If the queue module has no such reader and an archive folder exists, or if an
  item file cannot be read, the change set is incomplete (`state_unreadable`).
  The engine never reads a bad archive as empty and never reads an unreadable
  item as a deletion. Moving a decided item into an archive is not a change.
  This part needs the queue archive of pull request #164.
- The source transaction index reads every payload to hash it. Its cost grows
  with the size of the table.

## Scope

A change set is authoritative only for these parts of the company: the ledger
(entries, postings, balance assertions, accounts, source transactions and the
audit chain), the review queue, the staging lists
(`pending-categorization.json`, `pending.json`, `seen-ids.json`,
`duplicate-candidates.json`) and the learned context. Anything else is out of
scope, and a host that needs it must keep reading it directly:

- The accrual and business tables (`accrual_*`, `business_*`). The ledger entries
  that they post are listed.
- Other files, such as reports, statements, `entity.json` and trust policy.
- A command that has no company directory gives an empty change set.

The host runs one writer for each company at a time. The engine does not
separate its own writes from a write by another process during the same
command.
