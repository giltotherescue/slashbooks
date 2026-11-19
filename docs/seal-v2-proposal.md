# Proposal: seal v2, a ledger seal whose cost does not grow with the books

Status: proposal only. No code. The owner decides before anyone builds it.

## Problem

Each ledger write appends a `ledger-store-sealed` audit event. Its payload has `store_sha256`. This is the SHA-256 of
one JSON document that holds every row of `meta`, `accounts`, `entries`, `postings`, `balance_assertions` and
`source_transactions` (plus the accrual and business tables), in sorted order (`LedgerStore.content_digest`).
A writer must read and serialize every row to make it. At 50,000 transactions this is about 180 ms of a 226 ms
approve command. It is the only step left that grows with the books. SHA-256 cannot be updated from an old state
without the old text, so the format must change to remove the cost.

The seal has two jobs today. Keep both:
1. Tamper evidence: a later check finds a change to any row that the seal covers.
2. Staleness check: a preview or plan (`expected_digest`, `ledger_digest` in plans) is refused when the ledger changed.

## Tables and what protects them today

| Table | Write pattern | Protection other than the seal |
|---|---|---|
| `entries`, `postings`, `balance_assertions`, `audit_events` | Append-only (triggers from #165; one narrow path lets `account-rename` change `postings.account`) | Triggers, which an attacker with file access can drop |
| `source_transactions` | Insert, and a status change | None |
| `accounts`, `meta` | Insert and update | None |
| `accrual_*`, `business_*` | Insert and update by revision | Revision column |

## Design

### (a) Append-only tables: a hash chain with a running head

For each append-only table T, keep a head `H_T` and a row count `N_T`. When a write adds rows r1..rk (in id order):

    H_T' = SHA256(H_T || rowhash(r1)) ... then for r2..rk in order

`rowhash(r)` is SHA-256 of the canonical JSON of the row, as `content_digest` writes it today, with the table name
and the row id inside the hashed bytes. A write reads only its own new rows. The cost is O(rows written).

The seal event payload carries `{seal_version: 2, heads: {entries: [N, H], postings: [N, H], ...}, mutable: <root>}`.
The seal event is itself in the `audit_events` hash chain, so the heads are chained to every earlier event.

Tamper evidence is stronger than v1 in one way: v1 proves the state at one moment only. A v2 chain also proves the
order of the rows. A full check (scheduled, or `books check`) recomputes each chain from row 1 and compares it with the
head in the newest seal, and it also compares every earlier seal with the prefix of the chain at its row count.
This finds an edit in the middle of a table. It costs O(n), and it is an explicit check, as today.

A rename of `postings.account` (the one open path) must not break the chain. Rule: the rename event records the
old and new name, and `rowhash` for a posting uses the account name that the rename log gives at the time of
the row's creation. A simpler option: the rename writes one new seal that re-bases the postings chain (one O(n) write,
the same as the rename's own cost, which already rewrites the rows). Choose the second; it is easier to verify.

### (b) Mutable tables: a Merkle tree, not a plain sum

`accounts`, `meta`, `source_transactions` and the accrual and business tables change in place. Store a per-row hash
in a side table `seal_leaf(table, key, leaf_hash)` and combine the leaves in a binary Merkle tree keyed by
`SHA256(table || key)`. Use a sparse Merkle tree with a fixed depth of 32 over the first 4 bytes of the key hash (or a
standard Merkle B-tree with the same properties). A changed row changes one leaf and the nodes on its path to
the root: O(log n) reads and writes (about 20 for 1,000,000 rows). Nodes are kept in a side table
`seal_node(level, index, hash)`. The root goes in the seal payload as `mutable`.

Why not a plain additive sum (as `change_capture.py` uses for divergence detection)?

- A sum or XOR of row hashes is order-free and O(1) to update. That suits the change-capture index, which only needs to
  see that two states differ and where an honest mistake happened.
- It does not suit a tamper seal. An additive sum modulo 2^k of 256-bit hashes is open to the generalized birthday
  attack (Wagner): an adversary who can choose or add rows can find a set of forged rows with the same sum in about
  2^(k/(1+log2 k)) work, which is practical for k = 256 with enough chosen rows. XOR is linear and worse (Gaussian
  elimination). A Merkle tree has no such attack: a collision needs a SHA-256 collision or preimage.
- A sum also gives no proof of where a difference is. A tree finds the changed leaf by walking down.

So: sums stay in the capture index (divergence detection), and the seal uses a tree.

Trade-off: the side tables are derived data. A writer must keep them in step in the same transaction. A full check
rebuilds the tree from the rows and compares the root with the newest seal. If the side tables are damaged, the
check reports it and rebuilds them; the seal in the audit chain is the authority, not the side table.

### (c) Old v1 seals

- A v1 seal stays valid forever as history. The verifier reads `seal_version` (absent means 1).
- Checking a v1 seal is the existing code: recompute `content_digest` at that point. This works only for the newest seal
  (earlier states are gone), as today. Older v1 seals are checked as chain links only, as today.
- Upgrade, one time, per company: a command (or the first write after the new engine) does these steps in one
  transaction:
  1. Verify the current state with the v1 full check (audit chain, integrity, digest equals the newest v1 seal).
     If it fails, stop; do not upgrade a ledger that does not verify.
  2. Build the chains, the leaf table and the tree from the rows (one O(n) pass).
  3. Append a `seal-upgraded` event that records the v1 digest, the heads and the root, then a v2 seal.
  The v1 digest in the `seal-upgraded` event links the v2 history to the last v1 proof, so tamper evidence is not
  reset. An older engine reads `seal_version: 2` and refuses to write (it cannot extend the seal), which is the same
  guard as a newer schema version today.
- Hosted migration bundles and the `ledger_digest` of plans: keep `content_digest()` as an explicit O(n) function for
  plans and exports. Plans can switch later to the v2 root, which is O(1) to read.

### (d) Expected cost per approve

Estimates from the measured v1 costs and the structure above; not measured.

| Transactions | v1 seal now | v2 seal | Whole approve now | Whole approve with v2 |
|---|---|---|---|---|
| 1,000 | 6 ms | 1 to 2 ms | 33 ms | about 28 ms |
| 10,000 | 35 ms | 1 to 2 ms | 67 ms | about 32 ms |
| 50,000 | 180 ms | 2 to 3 ms | 226 ms | about 45 ms (parser 30 to 55 ms is the floor) |

Upgrade: one O(n) pass, about 0.5 s at 50,000, once. Full check: O(n), about the cost of v1's digest plus the chain.
Disk: the leaf table and tree add about 100 bytes per mutable row (about 5 MB at 50,000 source transactions).

## Risks and open questions

- Format break: seal payloads change, so goldens that pin `store_sha256` change. The v2 rollout needs a written reason.
- The side tables are new schema: `schema_fingerprint` in change capture, the migration bundle and the hosted
  migration check must allow them (they are derived, like `entries_reverses_idx`).
- Triggers are the only guard on append-only rows between full checks. The chain does not change that (a full check
  catches it, as with v1). Option: add a cheap per-write check of the last row of each chain against its head.
- Concurrent writers must extend the heads under the existing write lock (they already hold it).
- Rename re-base is an O(n) write; it is rare and already O(n).

## Recommendation

Build it, in this order: (1) chains for the four append-only tables, which remove the largest part and are simple;
(2) the Merkle tree for the mutable tables; (3) the upgrade and the `seal-upgraded` event. Do not use an additive sum
for the seal. Expect the approve command at 50,000 transactions to fall from 226 ms to about 45 ms.
