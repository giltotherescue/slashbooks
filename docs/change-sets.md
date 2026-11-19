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
- `--change-set-base-revision M` and `--change-set-revision N` are the revisions
  of the host. The host owns the numbers: `M` is the revision of the host view
  before the command, and `N` is the revision that the host gives to the result
  (`N` must be greater than `M`). The engine copies them into `base_revision` and
  `books_revision`. A change set without them cannot be applied by the reducer.
  The engine has no revision counter of its own. Its position is the audit chain
  head, and `ledger.before` and `ledger.after` give it.
- The engine removes an old file at `PATH` before it checks the command, so an
  invalid command never leaves the receipt of an earlier run. With `--work-out`
  also set, an error in the work events or in their file (after an ingest, for
  example) makes the change set incomplete.
- The engine removes an old file at `PATH` before the command, so a document of
  an earlier run is never read as the document of this run. If any step after
  the command body fails (the read of the state after the command, the digest),
  the change set has `"complete": false` (`finalize_failed`). If the write of
  the file fails, the engine writes an incomplete change set (`write_failed`),
  and if that also fails it removes the file. A host that finds no file does a
  full read.
- A host that runs the engine in a sandbox sends `--change-set-out
  @output/change.json`. The command contract (`remote_contract.command_spec`)
  lists `change_set_out` in `output_fields`.

A host that embeds the engine in Python wraps one command:

```python
from bookkeeping import changeset, cli

with changeset.capture(company_dir, base_revision=40, books_revision=41) as receipt:
    code = cli.main(argv)
document = receipt.change_set
```

`capture` also wraps a Python call that is not a command, such as
`queue.set_taught`. It writes no output file.

While the command runs, the capture keeps a log of the rows that it writes (see
Cost). It adds a log table and triggers to `ledger.sqlite` before the command and
removes them after. They write no ledger row. Pass `incremental=False` to compare
two full reads of the books instead. The two ways give the same document.

## Document shape

```json
{
  "schema_version": 1,
  "kind": "books-change-set",
  "base_revision": 40,
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
    "context_history": {"added": [], "updated": [], "removed": []},
    "seen_ids": {"added": [], "removed": []}
  },
  "documents": [
    {"path": "staging/split-templates.json", "change": "updated", "content": {"halves": []}}
  ],
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
| `base_revision`, `books_revision` | The integers from `--change-set-base-revision` and `--change-set-revision`, or `null`. |
| `complete` | `false` when a host must not apply the change set: the command failed after it changed something, or the engine could not read the state in full. `reason` is then `command_failed`, `state_unreadable` (a file or an archive that cannot be read, or a state that cannot be read), `finalize_failed` (a step after the command body failed) or `write_failed`. A host does a full read. |
| `state.before`, `state.after` | Digest of the whole authoritative content before and after the command. For each section (ledger entries with their postings, balance assertions, audit events, accounts, bank rows with the hash of the payload computed from the payload, review queue, the staging lists, context history, documents, seen ids, learned context) the digest takes the number of rows and the sum of the row hashes (SHA-512 of the canonical JSON of the section name, the row key and the row, modulo 2^512). It does not depend on the order of the rows, so the engine updates it from the changed rows. `changeset.view_digest(view)` computes the same digest from a host view, and the reducer checks it exactly. The digest holds a version number, so a digest of another version never equals this one. |
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
| `staging.context_history` | The same three lists for the rows of `staging/context-history.json`. The key is `source_id`. |
| `documents` | Small files that the change set holds as a whole: `staging/split-templates.json`, `staging/source-id-aliases.json` and `learned-context/accrual-rules.json`. `change` is `created`, `updated` or `removed`. `content` is the whole file (`null` for a removed file). |
| `staging.seen_ids` | Source ids that the intake store added to or removed from `staging/seen-ids.json`. |
| `review_queue` | One row for each review item that was created, changed or removed. `id` is the item `source_id`. `change` is `created`, `updated` or `removed`. `status_before` is `null` for a created item. `status_after` is `null` for a removed item. `closed` is `true` when `status_after` is not an open status (`open`, `reopened`, `requires-reproposal`). `item` is the whole item (`null` for a removed item). |
| `learned_context` | One row for each payee key whose learned context changed, with the whole `entry`. A taught category is in `entry.taught` (`id`, `category`, `at`, `by`). `change` is `created`, `updated` or `removed`. |
| `audit` | The audit events that the command added: first id, last id, count, and for each event the whole row: id, type, time, `prev_hash`, `record_hash` and `payload`. All values are `null` or empty when none. |

Lists are sorted: entries by id, accounts by name, bank rows by source id,
queue items by id, learned context by key, audit events by id.

## Apply a change set

The host owns the revision number. A change set applies only to the view that
has its base revision. Each change set also carries the position of the engine
before and after it: `ledger.before` and `ledger.after` (the audit chain head and
the newest row ids) and `state.before` and `state.after` (the digest of the whole
content). The revision is the first check, and the position and the digest are
the second check. A view can return to an earlier position and digest (for
example a taught category and its undo), so the position and the digest alone
cannot tell a stale change set from a new one.

The reference reducer `changeset.apply_change_set(view, change_set)` turns a
host view into the next view. Start from `changeset.empty_view()`. It refuses
the change set with `ChangeSetRefused` in these cases:

- `complete` is `false`.
- `base_revision` is not the revision of the view (`view["revision"]`), or
  `books_revision` is not greater than `base_revision`, or one of them is
  missing. This rejects a stale replay, a skipped change set and a change set
  that arrives out of order.
- `ledger.before` is not the position of the view.
- `state.before` is not the digest of the content of the view
  (`changeset.view_digest(view)`). The reducer computes the digest from the
  rows of the view, the ledger and audit rows included, and from the bank
  payloads. It does not trust a stored hash. Two replicas whose rows differ do
  not match.
- The `payload_sha256` of a bank row is not the hash of its `row`, or the `row`
  of a bank row that was not removed is null or not an object. A stored hash never
  stands in for a payload. The engine stores a payload that is not a JSON object
  as `{"unreadable_payload_text": TEXT}`, so every bank row has an object.
- After the apply, the position or the digest of the new view is not
  `ledger.after` or `state.after`.

A refused change set means that the host does a full read
(`changeset.read_view(company_dir, revision=N)`) and starts again from that view.
The reducer does not change the view that it gets. A host that keeps
`state.after` of the last change set can compare it with the next
`state.before`, and can keep a running hash over its rows, instead of computing
the digest again.

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

The cost of a capture grows with the rows that the command changed. It does not
grow with the number of transactions. The capture finds the changed rows with
logs and checks the logs with cheap tests. It falls back to a full read of the
part that it cannot prove.

| Part | How the capture finds the changed rows | Fallback |
|---|---|---|
| Ledger entries, postings, balance assertions, audit events | Row ids above the saved position. The digest adds the hash of each new row to the saved total. | A broken audit chain gives `reset`. A reset, an account rename (it relabels posted rows), an audit chain that moved back, and a store without the append-only triggers make the capture read the whole ledger for the digest. |
| Accounts and source transactions (`bank_rows`) | A log table and triggers in `ledger.sqlite`, added before the command and removed after. They fire for every connection and write only the key of each written row to the log. The capture reads only those rows. | The capture reads both tables in full when the log is not the log of this run (the store was replaced, the triggers were changed or dropped, the store could not take them). |
| Review queue items and month archives | One `stat` for each item file and each archive file. The capture reads the item files that differ from the saved copy. The helper that writes an archive journals the names that it changed, so the capture does not read the archive. A decided item that moves into an archive is not a change. | Two names with one source id, an unfinished group journal, or a file name that does not follow its source id: the capture reads the whole queue. |
| `seen-ids.json`, `pending-categorization.json`, learned context | The engine's write helpers journal the ids that they add or remove (`capture_log.writing`). The capture does not read the file. | A write that is not in the journal, or that left another file signature than the last journaled write: the capture reads the file. |
| `pending.json`, `duplicate-candidates.json`, `context-history.json`, the documents | The signature of the file. A changed file is read and compared with the saved copy. | None needed. |
| `state.before`, `state.after` | Totals that the saved copy keeps for each section (count and sum of row hashes). | A part whose signature is not the signature at the end of the last capture is read in full once. |

The saved copy is the file `.change-capture.sqlite` in the company folder. It
holds the rows of the small tables and the totals of every section as the last
capture left them. A host that runs each command in a new copy of the company
folder must keep this file with the folder. Without it, the first capture of each
command reads the books in full. A command that runs without `--change-set-out`
does not update the file. The next capture then notices the changed signatures
and reads the changed parts in full once. The file has no authority: a person can
delete it. It holds copies of company rows, so it is as private as the folder.

The log is append-only for the capture. Its rows have numbers (`AUTOINCREMENT`),
and triggers refuse an edit or a delete. At the end the capture checks that the
log table is the table that it made (token and root page), that every capture
trigger is present with its exact text, and that the numbers run from 1 to the
number in `sqlite_sequence` without a gap. Any fault gives a full read of the
accounts and the source transactions.

A file signature is the modification time, the size, the inode and the change
time. The change time cannot be set back, so an edit of the same length with the
old modification time restored still shows. The index holds a random generation
id that changes at each capture, and the capture checks the inode, the change
time and the generation of the index file at the start and the end. If the index
was deleted, restored or replaced during the command, the capture has no safe
copy of the state before, so the change set is incomplete (`state_unreadable`).

The log table and the triggers exist only while the command runs. They change no
ledger row and no ledger trigger, and `append_only_guard_problems` does not see
them. The next capture removes the log and the triggers of a command that was
killed. A store that holds them (for example a copy that was made during a
command) is refused by `hosted_migration`, which allows only the engine's own
triggers.

- The review queue is read with the queue module (`queue.load_all_items`) when
  the capture reads it in full: the item files and the month archives in
  `review-queue/archive/`. The change set code has no archive reader of its own.
  The reader refuses a corrupt archive or an archive version that it does not
  know, and it reads item files before archives, so a compaction that runs during
  the read does not hide an item. If the queue module has no such reader and an
  archive folder exists, or if an item file cannot be read, the change set is
  incomplete (`state_unreadable`). The engine never reads a bad archive as empty
  and never reads an unreadable item as a deletion.
- The check of the review queue costs one `stat` for each file in `review-queue/`
  (open items; a decided item is archived) and in its archive folder (one file
  for each month). The file signatures use the modification time of the file
  system. A write within the clock tick of an earlier write can get the same
  time, so the capture waits (at most 12 milliseconds) before it starts when a
  tracked file is that new.
- After the first capture of a company, the capture never reads the ledger rows
  or the source transaction payloads again, except for the rows that the command
  changed.

## Scope

A change set is authoritative only for these parts of the company:

- The ledger: entries, postings, balance assertions, accounts, source
  transactions (bank rows) and the audit chain.
- The review queue (item files and month archives).
- The staging lists: `staging/pending-categorization.json`,
  `staging/pending.json`, `staging/seen-ids.json`,
  `staging/duplicate-candidates.json` and `staging/context-history.json`.
- The learned context: `learned-context/counterparties.json`.
- The small documents: `staging/split-templates.json`,
  `staging/source-id-aliases.json` and `learned-context/accrual-rules.json`.

A file in `staging/` or `learned-context/` that is not in this list is not
covered, and a test fails when one appears. Anything else is out of scope, and a
host that needs it must keep reading it directly:

- The accrual and business tables (`accrual_*`, `business_*`). The ledger entries
  that they post are listed.
- Configuration files that a settings command writes (`entity.json`,
  `trust-policy.json`) and the business profile. A display row of the books does
  not come from them.
- Generated output: `reports/`, `ingestion/` and the lock file.
- A command that has no company directory gives an empty change set.

A file that is in scope and cannot be read (not valid JSON, a wrong shape, a
member that is null or of the wrong type or has no key, a corrupt archive) gives an incomplete change set (`state_unreadable`). The engine
never reads it as empty.

The host runs one writer for each company at a time. The engine does not
separate its own writes from a write by another process during the same
command.

## Benchmark

`python3 benchmarks/bench_change_capture.py --sizes 1000,10000,50000` builds
companies with decided items in many month archives, times the capture of one
approve and one categorize, prints the path of each capture (`incremental`, or
each part that it read in full and why), and exits with 1 when a capture fell
back or when the time grows with the size of the books.
