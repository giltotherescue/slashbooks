"""Change sets: what one ledger write changed, as rows that a host can store.

The contract is in ``docs/change-sets.md``. The engine reads the company before a command and reads it again after
the command. It lists the new and changed rows. It never computes an accounting result, and the request text never
changes a change set.

Ledger rows are append-only, so the new entries, postings, balance assertions and audit events come from row ids
above the saved position. The smaller tables (accounts, bank rows, review queue, staging, learned context) are
compared by key.

A host wraps one command::

    with changeset.capture(entity_dir, books_revision=41) as receipt:
        code = cli.main(argv)
    document = receipt.change_set

``apply_change_set`` and ``read_view`` are the reference reducer and the full reader. They define what "equal to a
full read of the books" means, and the engine tests check it after every write.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

SCHEMA_VERSION = 1
KIND = "books-change-set"
OPEN_QUEUE_STATUSES = frozenset({"open", "reopened", "requires-reproposal"})
_QUEUE_DIR = "review-queue"
_ARCHIVE_DIR = "review-queue/archive"
_PENDING_FILE = "staging/pending-categorization.json"
_STORE_PENDING_FILE = "staging/pending.json"
_SEEN_FILE = "staging/seen-ids.json"
_DUPLICATES_FILE = "staging/duplicate-candidates.json"
_LEARNED_FILE = "learned-context/counterparties.json"
_CHUNK = 500

# Every file that the change set covers, as a path under the company directory (a glob for a folder of items).
# A file under these folders that is not in this list is not covered: ``test_changeset`` fails when one appears.
COVERED_FILES = frozenset({
    "staging/pending-categorization.json", "staging/pending.json", "staging/seen-ids.json",
    "staging/duplicate-candidates.json", "learned-context/counterparties.json",
})
COVERED_FOLDERS = ("staging", "learned-context")
QUEUE_ITEM_FOLDERS = ("review-queue", "review-queue/archive")

BANK_ROW_CHANGES = ("added", "status-changed", "updated", "removed")
LIST_CHANGES = ("created", "updated", "removed")
ZERO_POSITION = {
    "audit_event_id": None, "record_hash": None,
    "max_entry_id": 0, "max_posting_id": 0, "max_balance_assertion_id": 0,
}


class ChangeSetError(ValueError):
    """The books state cannot be read in full (for example a corrupt review queue archive)."""


class ChangeSetRefused(ValueError):
    """A change set does not follow the host view. The host must do a full read (``read_view``)."""


# ---------------------------------------------------------------------------
# Small readers
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None


def _connect_read_only(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _pairs(text: Any) -> list[list[str]]:
    try:
        raw = json.loads(text or "[]")
    except (ValueError, TypeError):
        return []
    return [[str(pair[0]), str(pair[1])] for pair in raw if isinstance(pair, (list, tuple)) and len(pair) == 2]


def _strings(text: Any) -> list[str]:
    try:
        raw = json.loads(text or "[]")
    except (ValueError, TypeError):
        return []
    return [str(value) for value in raw] if isinstance(raw, list) else []


def _entry_doc(row: sqlite3.Row, postings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    keys = row.keys()
    return {
        "id": int(row["id"]),
        "date": str(row["date"]),
        "flag": str(row["flag"]),
        "payee": row["payee"],
        "narration": str(row["narration"]),
        "tags": _strings(row["tags_json"]),
        "links": _strings(row["links_json"]),
        "metadata": _pairs(row["metadata_json"]),
        "source_id": row["source_id"],
        "session": row["session"] if "session" in keys else None,
        "late_arrival": bool(row["late_arrival"]) if "late_arrival" in keys else False,
        "postings": [dict(posting) for posting in postings],
    }


def _posting_doc(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]), "account": str(row["account"]), "amount": str(row["amount"]),
        "currency": str(row["currency"]), "metadata": _pairs(row["metadata_json"]),
    }


def _load_entries(conn: sqlite3.Connection, after_id: int) -> list[dict[str, Any]]:
    """Entries with an id above ``after_id``, with their postings, in id order."""
    postings: dict[int, list[dict[str, Any]]] = {}
    for row in conn.execute("SELECT * FROM postings WHERE entry_id > ? ORDER BY entry_id, id", (after_id,)):
        postings.setdefault(int(row["entry_id"]), []).append(_posting_doc(row))
    return [
        _entry_doc(row, postings.get(int(row["id"]), []))
        for row in conn.execute("SELECT * FROM entries WHERE id > ? ORDER BY id", (after_id,))
    ]


def _load_balances(conn: sqlite3.Connection, after_id: int) -> list[dict[str, Any]]:
    return [
        {"id": int(row["id"]), "date": str(row["date"]), "account": str(row["account"]),
         "amount": str(row["amount"]), "currency": str(row["currency"])}
        for row in conn.execute("SELECT * FROM balance_assertions WHERE id > ? ORDER BY id", (after_id,))
    ]


def _load_audit(conn: sqlite3.Connection, after_id: int) -> list[dict[str, Any]]:
    return [
        {"id": int(row["id"]), "type": str(row["type"]), "ts": str(row["ts"]), "record_hash": str(row["record_hash"])}
        for row in conn.execute(
            "SELECT id, type, ts, record_hash FROM audit_events WHERE id > ? ORDER BY id", (after_id,))
    ]


def _load_accounts(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for row in conn.execute("SELECT * FROM accounts"):
        keys = row.keys()
        result[int(row["id"])] = {
            "name": str(row["name"]), "currency": str(row["currency"]), "open_date": str(row["open_date"]),
            "financial_kind": row["financial_kind"] if "financial_kind" in keys else None,
        }
    return result


def _load_bank_index(conn: sqlite3.Connection) -> dict[str, tuple[str, str, str]]:
    """Each source transaction as (status, imported_at, SHA-256 of the stored payload). A payload change shows."""
    return {str(row[0]): (str(row[1]), str(row[2]), hashlib.sha256(str(row[3]).encode("utf-8")).hexdigest())
            for row in conn.execute("SELECT id, status, imported_at, payload_json FROM source_transactions")}


def _bank_rows(conn: sqlite3.Connection, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for start in range(0, len(ids), _CHUNK):
        chunk = list(ids[start:start + _CHUNK])
        marks = ",".join("?" for _ in chunk)
        for row in conn.execute(
                f"SELECT id, payload_json, status, imported_at FROM source_transactions WHERE id IN ({marks})", chunk):
            try:
                payload = json.loads(row["payload_json"])
            except (ValueError, TypeError):
                payload = None
            found[str(row["id"])] = {
                "status": str(row["status"]), "imported_at": str(row["imported_at"]),
                "payload_sha256": hashlib.sha256(str(row["payload_json"]).encode("utf-8")).hexdigest(), "row": payload}
    return found


def _position(conn: sqlite3.Connection | None) -> dict[str, Any]:
    if conn is None:
        return dict(ZERO_POSITION)
    head = conn.execute("SELECT id, record_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "audit_event_id": int(head[0]) if head else None,
        "record_hash": str(head[1]) if head else None,
        "max_entry_id": int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM entries").fetchone()[0]),
        "max_posting_id": int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM postings").fetchone()[0]),
        "max_balance_assertion_id": int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM balance_assertions").fetchone()[0]),
    }


def _counts(conn: sqlite3.Connection | None) -> tuple[int, int, int]:
    if conn is None:
        return 0, 0, 0
    return tuple(int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # type: ignore[return-value]
                 for table in ("entries", "postings", "balance_assertions"))


# Review queue, staging and learned context ----------------------------------


def _queue_item(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict) and "source_id" in raw and "status" in raw:
        return raw
    return None


def load_queue(root: Path) -> dict[str, dict[str, Any]]:
    """Every review item by ``source_id``: item files and month archives, read with the queue module.

    The archive reader is the one of the queue module (``queue.load_all_items``): it refuses a corrupt archive or a
    version it does not know, and it reads item files before archives, so an item that a compaction moves during the
    read is not missed. This module has no archive reader of its own. If the queue module has no such reader and an
    archive folder exists, or if an item file cannot be read, the read raises ``ChangeSetError``. It never reads an
    archive as empty and never reads an unreadable item as a deletion.
    """
    from . import queue as queue_module
    directory = root / _QUEUE_DIR
    if not directory.is_dir():
        return {}
    reader = getattr(queue_module, "load_all_items", None)
    named: dict[str, Any] = {}
    if reader is not None:
        try:
            from .entity import load_entity
            named = reader(load_entity(root))
        except ValueError as exc:
            raise ChangeSetError(f"The review queue cannot be read: {exc}") from exc
        unread = {path.name for path in directory.glob("*.json")} - set(named)
    else:
        if (root / _ARCHIVE_DIR).is_dir():
            raise ChangeSetError("The review queue has archives and this engine has no strict archive reader.")
        for path in sorted(directory.glob("*.json")):
            raw = _read_json(path)
            if _queue_item(raw) is not None:
                named[path.name] = raw
        unread = {path.name for path in directory.glob("*.json")} - set(named)
    if unread:
        raise ChangeSetError("A review item file cannot be read: " + ", ".join(sorted(unread)[:3]))
    items: dict[str, dict[str, Any]] = {}
    for _, raw in sorted(named.items()):
        item = _queue_item(raw)
        if item is not None:
            items[str(item["source_id"])] = item
    return items


def _load_keyed(path: Path, key: str) -> dict[str, dict[str, Any]]:
    data = _read_json(path)
    rows = [row for row in data if isinstance(row, dict) and row.get(key)] if isinstance(data, list) else []
    return {str(row[key]): row for row in rows}


def _load_ids(path: Path) -> list[str]:
    data = _read_json(path)
    return sorted(str(value) for value in data) if isinstance(data, list) else []


def _load_learned(root: Path) -> dict[str, Any]:
    data = _read_json(root / _LEARNED_FILE)
    return {str(key): value for key, value in data.items()} if isinstance(data, dict) else {}


@dataclass
class _Side:
    """The small tables of one company state, and the position of its ledger."""

    position: dict[str, Any]
    counts: tuple[int, int, int]
    accounts: dict[int, dict[str, Any]]
    bank: dict[str, tuple[str, str, str]]
    queue: dict[str, dict[str, Any]]
    pending: dict[str, dict[str, Any]]
    duplicates: dict[str, dict[str, Any]]
    store_pending: dict[str, dict[str, Any]]
    seen: list[str]
    learned: dict[str, Any]

    def digest(self) -> str:
        return _state_digest(
            {row["name"]: row for row in self.accounts.values()},
            {key: list(value) for key, value in self.bank.items()},
            self.queue, self.pending, self.duplicates, self.store_pending, self.seen, self.learned)


def _state_digest(accounts: Mapping[str, Any], bank: Mapping[str, Any], queue: Mapping[str, Any],
                  pending: Mapping[str, Any], duplicates: Mapping[str, Any], store_pending: Mapping[str, Any],
                  seen: Sequence[str], learned: Mapping[str, Any]) -> str:
    """SHA-256 of the small tables of a state. The engine and a host view compute it in the same way."""
    body = {"accounts": accounts, "bank": bank, "queue": queue, "pending": pending, "duplicates": duplicates,
            "store_pending": store_pending, "seen": sorted(seen), "learned": learned}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _take(root: Path) -> _Side:
    conn = _connect_read_only(root / "ledger.sqlite") if root.is_dir() else None
    try:
        try:
            position, counts = _position(conn), _counts(conn)
            accounts = _load_accounts(conn) if conn is not None else {}
            bank = _load_bank_index(conn) if conn is not None else {}
        except sqlite3.DatabaseError as exc:
            raise ChangeSetError(f"The ledger store cannot be read: {exc}") from exc
    finally:
        if conn is not None:
            conn.close()
    return _Side(
        position=position, counts=counts, accounts=accounts, bank=bank,
        queue=load_queue(root) if root.is_dir() else {},
        pending=_load_keyed(root / _PENDING_FILE, "id"),
        duplicates=_load_keyed(root / _DUPLICATES_FILE, "source_id"),
        store_pending=_load_keyed(root / _STORE_PENDING_FILE, "id"),
        seen=_load_ids(root / _SEEN_FILE),
        learned=_load_learned(root),
    )


# ---------------------------------------------------------------------------
# Building a change set
# ---------------------------------------------------------------------------


def _diff_keyed(before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[list[Any], list[Any], list[str]]:
    added = [after[key] for key in sorted(after) if key not in before]
    updated = [after[key] for key in sorted(after) if key in before and before[key] != after[key]]
    removed = sorted(key for key in before if key not in after)
    return added, updated, removed


def _variants(source_id: str) -> set[str]:
    from .source_ids import source_id_variants
    return set(source_id_variants(source_id))


def _reversals(conn: sqlite3.Connection, added: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in added:
        meta = dict((key, value) for key, value in entry["metadata"])
        target = meta.get("reverses")
        if not target:
            continue
        spellings = _variants(target)
        marks = ",".join("?" for _ in spellings)
        found = conn.execute(
            f"SELECT id FROM entries WHERE source_id IN ({marks}) AND id != ? ORDER BY id", [*sorted(spellings), entry["id"]]
        ).fetchone()
        replacements = sorted(
            int(other["id"]) for other in added
            if dict((key, value) for key, value in other["metadata"]).get("correction-of") in spellings)
        rows.append({
            "reversal_entry_id": entry["id"], "reverses_source_id": target,
            "reverses_entry_id": int(found[0]) if found else None, "replacement_entry_ids": replacements,
        })
    return rows


def _list_changes(before: Mapping[str, Any], after: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) == after.get(key) and (key in before) == (key in after):
            continue
        if key not in before:
            change = "created"
        elif key not in after:
            change = "removed"
        else:
            change = "updated"
        rows.append({"key": key, "change": change, "before": before.get(key), "after": after.get(key)})
    return rows


def _queue_rows(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for row in _list_changes(before, after, "queue"):
        status_after = str(row["after"].get("status")) if row["after"] is not None else None
        rows.append({
            "id": row["key"], "change": row["change"],
            "status_before": str(row["before"].get("status")) if row["before"] is not None else None,
            "status_after": status_after,
            "closed": status_after is not None and status_after not in OPEN_QUEUE_STATUSES,
            "item": copy.deepcopy(row["after"]),
        })
    return rows


_EMPTY_DIGEST = _state_digest({}, {}, {}, {}, {}, {}, [], {})


def _empty(books_revision: int | None, position: Mapping[str, Any] | None = None, digest: str | None = None,
           *, complete: bool = True, reason: str | None = None) -> dict[str, Any]:
    position = dict(position or ZERO_POSITION)
    digest = digest or _EMPTY_DIGEST
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "kind": KIND, "books_revision": books_revision,
        "complete": complete, "reason": reason,
        "changed": False, "reset": False,
        "ledger": {"before": dict(position), "after": dict(position)},
        "state": {"before": digest, "after": digest},
        "entries_added": [], "entries_reversed": [], "balance_assertions_added": [],
        "accounts": {"opened": [], "renamed": [], "updated": [], "removed": []},
        "bank_rows": [],
        "staging": {"pending": {"added": [], "updated": [], "removed": []},
                    "duplicates": {"added": [], "updated": [], "removed": []},
                    "store_pending": {"added": [], "updated": [], "removed": []},
                    "seen_ids": {"added": [], "removed": []}},
        "review_queue": [], "learned_context": [],
        "audit": {"first_event_id": None, "last_event_id": None, "count": 0, "events": []},
    }
    document["fingerprint"] = _fingerprint(document)
    return document


def _fingerprint(document: Mapping[str, Any]) -> str:
    body = {key: value for key, value in document.items() if key != "fingerprint"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _account_changes(before: Mapping[int, Mapping[str, Any]], after: Mapping[int, Mapping[str, Any]],
                     events: Sequence[Mapping[str, Any]], conn: sqlite3.Connection | None) -> dict[str, list[Any]]:
    opened = [dict(after[ident]) for ident in sorted(after, key=lambda i: after[i]["name"]) if ident not in before]
    removed = sorted(before[ident]["name"] for ident in before if ident not in after)
    renamed: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    event_ids: dict[tuple[str, str], int] = {}
    if conn is not None and any(before[i]["name"] != after[i]["name"] for i in before if i in after):
        for event in events:
            if event["type"] != "account-renamed":
                continue
            row = conn.execute("SELECT payload_json FROM audit_events WHERE id = ?", (event["id"],)).fetchone()
            try:
                payload = json.loads(row[0]) if row else {}
            except (ValueError, TypeError):
                payload = {}
            for pair in payload.get("accounts") or []:
                if isinstance(pair, list) and len(pair) == 2:
                    event_ids[(str(pair[0]), str(pair[1]))] = int(event["id"])
    for ident in sorted(before, key=lambda i: before[i]["name"]):
        if ident not in after:
            continue
        old, new = before[ident], after[ident]
        if old["name"] != new["name"]:
            renamed.append({"from": old["name"], "to": new["name"],
                            "audit_event_id": event_ids.get((old["name"], new["name"]))})
        if {k: v for k, v in old.items() if k != "name"} != {k: v for k, v in new.items() if k != "name"}:
            updated.append(dict(new))
    return {"opened": opened, "renamed": renamed, "updated": updated, "removed": removed}


def _build(before: _Side, root: Path, books_revision: int | None, failed: bool = False) -> dict[str, Any]:
    after = _take(root)
    conn = _connect_read_only(root / "ledger.sqlite") if root.is_dir() else None
    try:
        document = _empty(books_revision, before.position, before.digest(), complete=not failed,
                          reason="command_failed" if failed else None)
        document["ledger"]["after"] = dict(after.position)
        document["state"]["after"] = after.digest()
        entries_after, postings_after, balances_after = after.counts
        entries_before, postings_before, balances_before = before.counts
        entry_floor = before.position["max_entry_id"]
        balance_floor = before.position["max_balance_assertion_id"]
        audit_floor = before.position["audit_event_id"] or 0
        reset = False
        if conn is not None:
            entries = _load_entries(conn, entry_floor)
            balances = _load_balances(conn, balance_floor)
            row = (conn.execute("SELECT record_hash FROM audit_events WHERE id = ?", (audit_floor,)).fetchone()
                   if audit_floor else None)
            chain_broken = bool(audit_floor) and row is not None and str(row[0]) != before.position["record_hash"]
            posting_count = sum(len(entry["postings"]) for entry in entries)
            reset = (chain_broken
                     or entries_after != entries_before + len(entries)
                     or postings_after != postings_before + posting_count
                     or balances_after != balances_before + len(balances))
            if reset:
                entries, balances, audit_floor = _load_entries(conn, 0), _load_balances(conn, 0), 0
            events = _load_audit(conn, audit_floor)
            document["entries_added"] = entries
            document["balance_assertions_added"] = balances
            document["entries_reversed"] = _reversals(conn, entries)
            document["audit"] = {
                "first_event_id": events[0]["id"] if events else None,
                "last_event_id": events[-1]["id"] if events else None,
                "count": len(events), "events": events,
            }
            document["accounts"] = _account_changes(before.accounts, after.accounts, events, conn)
            changed_ids = sorted(
                key for key in set(before.bank) | set(after.bank) if before.bank.get(key) != after.bank.get(key))
            rows = _bank_rows(conn, [key for key in changed_ids if key in after.bank])
            for key in changed_ids:
                old, new = before.bank.get(key), rows.get(key)
                if new is None:
                    change, status_after, imported_at, row_doc = "removed", None, None, None
                else:
                    status_after, imported_at, row_doc = new["status"], new["imported_at"], new["row"]
                    change = ("added" if old is None else
                              "status-changed" if old[0] != new["status"] else "updated")
                document["bank_rows"].append({
                    "source_id": key, "change": change, "status_before": old[0] if old else None,
                    "status_after": status_after, "imported_at": imported_at,
                    "payload_sha256": new["payload_sha256"] if new else None, "row": row_doc})
        document["reset"] = reset
        added, updated, removed = _diff_keyed(before.pending, after.pending)
        document["staging"]["pending"] = {"added": added, "updated": updated, "removed": removed}
        added, updated, removed = _diff_keyed(before.duplicates, after.duplicates)
        document["staging"]["duplicates"] = {"added": added, "updated": updated, "removed": removed}
        added, updated, removed = _diff_keyed(before.store_pending, after.store_pending)
        document["staging"]["store_pending"] = {"added": added, "updated": updated, "removed": removed}
        document["staging"]["seen_ids"] = {
            "added": sorted(set(after.seen) - set(before.seen)), "removed": sorted(set(before.seen) - set(after.seen))}
        document["review_queue"] = _queue_rows(before.queue, after.queue)
        document["learned_context"] = [
            {"counterparty": row["key"], "change": row["change"], "entry": row["after"]}
            for row in _list_changes(before.learned, after.learned, "learned")
        ]
    finally:
        if conn is not None:
            conn.close()
    document["changed"] = bool(
        document["reset"] or document["entries_added"] or document["balance_assertions_added"]
        or any(document["accounts"].values()) or document["bank_rows"] or document["review_queue"]
        or document["learned_context"] or document["audit"]["count"]
        or document["ledger"]["before"] != document["ledger"]["after"]
        or before.digest() != after.digest()
        or any(document["staging"][name][part] for name in document["staging"] for part in document["staging"][name]))
    if failed and not document["changed"]:
        # The command failed and left nothing: the state is the state before, so the change set is complete.
        document["complete"], document["reason"] = True, None
    document["fingerprint"] = _fingerprint(document)
    return document


# ---------------------------------------------------------------------------
# Capture around one command
# ---------------------------------------------------------------------------


@dataclass
class ChangeSetCapture:
    """Result holder for :func:`capture`; ``change_set`` is set when the block exits.

    A caller that sees a command fail without an exception sets ``failed = True`` inside the block.
    """

    entity_path: Path | None
    books_revision: int | None = None
    change_set: dict[str, Any] = field(default_factory=dict)
    failed: bool = False


@contextmanager
def capture(entity_path: Path | str | None, *, books_revision: int | None = None) -> Iterator[ChangeSetCapture]:
    """Compute the change set for the command that runs inside the block.

    Commands are not one transaction: a command that fails can leave some of its writes. When the block raises, or
    the caller sets ``failed``, the change set has ``complete: false`` and a host must do a full read instead of
    applying it. If the state cannot be read in full before or after the command, the change set is also incomplete
    (``reason``). The capture never stops the command itself. With ``entity_path=None`` the result is an empty change set.
    """
    holder = ChangeSetCapture(Path(entity_path) if entity_path is not None else None, books_revision)
    holder.change_set = _empty(books_revision)
    if holder.entity_path is None:
        yield holder
        return
    try:
        before: _Side | None = _take(holder.entity_path)
    except ChangeSetError:
        before = None
    if before is not None:
        holder.change_set = _empty(books_revision, before.position, before.digest())
    try:
        yield holder
    except BaseException:
        holder.failed = True
        raise
    finally:
        holder.change_set = _finish(before, holder, books_revision)


def _finish(before: "_Side | None", holder: ChangeSetCapture, books_revision: int | None) -> dict[str, Any]:
    if before is None:
        return _incomplete(books_revision, "state_unreadable")
    try:
        return _build(before, holder.entity_path, books_revision, holder.failed)  # type: ignore[arg-type]
    except ChangeSetError:
        return _incomplete(books_revision, "state_unreadable")


def _incomplete(books_revision: int | None, reason: str) -> dict[str, Any]:
    document = _empty(books_revision, complete=False, reason=reason)
    document["changed"] = True  # The host cannot tell: it must do a full read.
    document["fingerprint"] = _fingerprint(document)
    return document


def write_document(path: Path, document: Mapping[str, Any]) -> None:
    """Write the machine-readable change set for ``--change-set-out``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def entity_path_from_args(args: argparse.Namespace) -> Path | None:
    from . import work
    return work.entity_path_from_args(args)


# ---------------------------------------------------------------------------
# Reference reducer and full reader
# ---------------------------------------------------------------------------


def empty_view() -> dict[str, Any]:
    """The host view of a company with no books."""
    return {
        "position": dict(ZERO_POSITION),
        "entries": {}, "balance_assertions": {}, "accounts": {}, "bank_rows": {},
        "staging": {"pending": {}, "duplicates": {}, "store_pending": {}, "seen_ids": []},
        "review_queue": {}, "learned_context": {}, "audit_events": {},
    }


def apply_change_set(view: dict[str, Any], change_set: Mapping[str, Any]) -> dict[str, Any]:
    """Return the next host view. The input view does not change."""
    errors = validate(change_set)
    if errors:
        raise ValueError("Not a valid change set: " + "; ".join(errors[:3]))
    if not change_set["complete"]:
        raise ChangeSetRefused(f"The change set is not complete ({change_set['reason']}): do a full read.")
    if change_set["ledger"]["before"] != view["position"]:
        raise ChangeSetRefused(
            "The change set does not start at the position of the view (a stale, skipped or out-of-order change set): "
            "do a full read.")
    if change_set["state"]["before"] != view_digest(view):
        raise ChangeSetRefused("The change set does not start at the state of the view: do a full read.")
    new = _apply(view, change_set)
    if new["position"] != change_set["ledger"]["after"] or view_digest(new) != change_set["state"]["after"]:
        raise ChangeSetRefused("The view after the change set does not match the engine state: do a full read.")
    return new


def view_digest(view: Mapping[str, Any]) -> str:
    """The digest of the small tables of a host view. It equals ``state.after`` of the last change set that was applied."""
    staging = view["staging"]
    return _state_digest(
        view["accounts"],
        {key: [row["status"], row["imported_at"], row["payload_sha256"]] for key, row in view["bank_rows"].items()},
        view["review_queue"], staging["pending"], staging["duplicates"], staging["store_pending"],
        list(staging["seen_ids"]), view["learned_context"])


def _apply(view: Mapping[str, Any], change_set: Mapping[str, Any]) -> dict[str, Any]:
    new = copy.deepcopy(view)
    if change_set["reset"]:
        new["entries"], new["balance_assertions"], new["audit_events"] = {}, {}, {}
    accounts = change_set["accounts"]
    mapping = {row["from"]: row["to"] for row in accounts["renamed"]}
    if mapping:
        for entry in new["entries"].values():
            for posting in entry["postings"]:
                posting["account"] = mapping.get(posting["account"], posting["account"])
        for row in new["balance_assertions"].values():
            row["account"] = mapping.get(row["account"], row["account"])
        renamed = {}
        for name, row in new["accounts"].items():
            row = dict(row)
            row["name"] = mapping.get(name, name)
            renamed[row["name"]] = row
        new["accounts"] = renamed
    for name in accounts["removed"]:
        new["accounts"].pop(name, None)
    for row in (*accounts["opened"], *accounts["updated"]):
        new["accounts"][row["name"]] = copy.deepcopy(row)
    for entry in change_set["entries_added"]:
        new["entries"][entry["id"]] = copy.deepcopy(entry)
    for row in change_set["balance_assertions_added"]:
        new["balance_assertions"][row["id"]] = copy.deepcopy(row)
    for row in change_set["bank_rows"]:
        if row["change"] == "removed":
            new["bank_rows"].pop(row["source_id"], None)
        else:
            new["bank_rows"][row["source_id"]] = {
                "status": row["status_after"], "imported_at": row["imported_at"],
                "payload_sha256": row["payload_sha256"], "row": copy.deepcopy(row["row"])}
    seen = change_set["staging"]["seen_ids"]
    new["staging"]["seen_ids"] = sorted((set(new["staging"]["seen_ids"]) | set(seen["added"])) - set(seen["removed"]))
    for name, key in (("pending", "id"), ("duplicates", "source_id"), ("store_pending", "id")):
        part = change_set["staging"][name]
        for row in (*part["added"], *part["updated"]):
            new["staging"][name][str(row[key])] = copy.deepcopy(row)
        for ident in part["removed"]:
            new["staging"][name].pop(ident, None)
    for row in change_set["review_queue"]:
        if row["change"] == "removed":
            new["review_queue"].pop(row["id"], None)
        else:
            new["review_queue"][row["id"]] = copy.deepcopy(row["item"])
    for row in change_set["learned_context"]:
        if row["change"] == "removed":
            new["learned_context"].pop(row["counterparty"], None)
        else:
            new["learned_context"][row["counterparty"]] = copy.deepcopy(row["entry"])
    after = change_set["ledger"]["after"]
    new["position"] = dict(after)
    for event in change_set["audit"]["events"]:
        new["audit_events"][event["id"]] = dict(event)
    if after["audit_event_id"] is None:
        new["audit_events"] = {}
    else:
        new["audit_events"] = {key: value for key, value in new["audit_events"].items()
                               if key <= after["audit_event_id"]}
    return new


def read_view(entity_path: Path | str) -> dict[str, Any]:
    """Read the whole books of a company into the host view shape."""
    root = Path(entity_path)
    view = empty_view()
    conn = _connect_read_only(root / "ledger.sqlite")
    if conn is not None:
        try:
            view["position"] = _position(conn)
            view["entries"] = {entry["id"]: entry for entry in _load_entries(conn, 0)}
            view["balance_assertions"] = {row["id"]: row for row in _load_balances(conn, 0)}
            view["audit_events"] = {event["id"]: event for event in _load_audit(conn, 0)}
            view["accounts"] = {row["name"]: row for row in _load_accounts(conn).values()}
            index = _load_bank_index(conn)
            view["bank_rows"] = _bank_rows(conn, sorted(index))
        finally:
            conn.close()
    view["staging"]["pending"] = _load_keyed(root / _PENDING_FILE, "id")
    view["staging"]["duplicates"] = _load_keyed(root / _DUPLICATES_FILE, "source_id")
    view["staging"]["store_pending"] = _load_keyed(root / _STORE_PENDING_FILE, "id")
    view["staging"]["seen_ids"] = _load_ids(root / _SEEN_FILE)
    view["review_queue"] = load_queue(root)
    view["learned_context"] = _load_learned(root)
    return view


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


_TOP_KEYS = {
    "schema_version", "kind", "books_revision", "complete", "reason", "state", "changed", "reset", "ledger", "entries_added", "entries_reversed",
    "balance_assertions_added", "accounts", "bank_rows", "staging", "review_queue", "learned_context", "audit",
    "fingerprint",
}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_position(value: Any, label: str) -> list[str]:
    if not isinstance(value, dict) or set(value) != set(ZERO_POSITION):
        return [f"{label} must have the keys {sorted(ZERO_POSITION)}"]
    errors = []
    for key in ("max_entry_id", "max_posting_id", "max_balance_assertion_id"):
        if not _is_int(value[key]) or value[key] < 0:
            errors.append(f"{label}.{key} must be a non-negative integer")
    if not ((value["audit_event_id"] is None and value["record_hash"] is None)
            or (_is_int(value["audit_event_id"]) and isinstance(value["record_hash"], str))):
        errors.append(f"{label}.audit_event_id and record_hash must both be set or both be null")
    return errors


def validate(document: Any) -> list[str]:
    """Return the problems of a change set document. An empty list means that it is valid. No dependencies."""
    if not isinstance(document, dict):
        return ["a change set must be an object"]
    if document.get("schema_version") != SCHEMA_VERSION:
        return [f"schema_version must be {SCHEMA_VERSION}"]
    errors: list[str] = []
    if set(document) != _TOP_KEYS:
        errors.append(f"keys must be {sorted(_TOP_KEYS)}")
        return errors
    if document["kind"] != KIND:
        errors.append(f"kind must be {KIND!r}")
    if document["books_revision"] is not None and not _is_int(document["books_revision"]):
        errors.append("books_revision must be an integer or null")
    for name in ("changed", "reset", "complete"):
        if not isinstance(document[name], bool):
            errors.append(f"{name} must be a boolean")
    if document["reason"] is not None and not isinstance(document["reason"], str):
        errors.append("reason must be a string or null")
    state = document["state"]
    if not isinstance(state, dict) or set(state) != {"before", "after"} or not all(
            isinstance(value, str) and len(value) == 64 for value in state.values()):
        errors.append("state must have the SHA-256 digests before and after")
    ledger = document["ledger"]
    if not isinstance(ledger, dict) or set(ledger) != {"before", "after"}:
        errors.append("ledger must have before and after")
    else:
        errors += _check_position(ledger["before"], "ledger.before") + _check_position(ledger["after"], "ledger.after")
    for name in ("entries_added", "entries_reversed", "balance_assertions_added", "bank_rows", "review_queue",
                 "learned_context"):
        if not isinstance(document[name], list):
            errors.append(f"{name} must be a list")
    if errors:
        return errors
    for entry in document["entries_added"]:
        if (not isinstance(entry, dict) or not _is_int(entry.get("id")) or not isinstance(entry.get("postings"), list)
                or not all(isinstance(p, dict) and _is_int(p.get("id")) and isinstance(p.get("account"), str)
                           and isinstance(p.get("amount"), str) for p in entry["postings"])):
            errors.append("entries_added rows need an integer id and postings with id, account and amount")
            break
    accounts = document["accounts"]
    if not isinstance(accounts, dict) or set(accounts) != {"opened", "renamed", "updated", "removed"} or not all(
            isinstance(accounts[key], list) for key in accounts):
        errors.append("accounts must have the lists opened, renamed, updated and removed")
    elif not all(isinstance(r, dict) and isinstance(r.get("from"), str) and isinstance(r.get("to"), str)
                 for r in accounts["renamed"]):
        errors.append("accounts.renamed rows need from and to")
    for row in document["bank_rows"]:
        if not isinstance(row, dict) or row.get("change") not in BANK_ROW_CHANGES or not isinstance(row.get("source_id"), str):
            errors.append("bank_rows rows need a source_id and a change from " + ", ".join(BANK_ROW_CHANGES))
            break
    staging = document["staging"]
    lists = ("pending", "duplicates", "store_pending")
    if (not isinstance(staging, dict) or set(staging) != {*lists, "seen_ids"}
            or not all(isinstance(staging[name], dict) and set(staging[name]) == {"added", "updated", "removed"}
                       and all(isinstance(staging[name][key], list) for key in staging[name]) for name in lists)
            or not isinstance(staging["seen_ids"], dict) or set(staging["seen_ids"]) != {"added", "removed"}
            or not all(isinstance(value, list) for value in staging["seen_ids"].values())):
        errors.append("staging must have pending, duplicates and store_pending (added, updated, removed) and seen_ids (added, removed)")
    for row in document["review_queue"]:
        if (not isinstance(row, dict) or row.get("change") not in LIST_CHANGES or not isinstance(row.get("id"), str)
                or not isinstance(row.get("closed"), bool)):
            errors.append("review_queue rows need an id, a change from " + ", ".join(LIST_CHANGES) + " and closed")
            break
    for row in document["learned_context"]:
        if not isinstance(row, dict) or row.get("change") not in LIST_CHANGES or not isinstance(row.get("counterparty"), str):
            errors.append("learned_context rows need a counterparty and a change")
            break
    audit = document["audit"]
    if (not isinstance(audit, dict) or set(audit) != {"first_event_id", "last_event_id", "count", "events"}
            or not isinstance(audit["events"], list) or audit["count"] != len(audit["events"])):
        errors.append("audit must have first_event_id, last_event_id, count and events")
    if document["fingerprint"] != _fingerprint(document):
        errors.append("fingerprint does not match the content")
    return errors
