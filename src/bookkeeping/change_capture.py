"""Incremental capture of a change set: the cost of a capture grows with the rows that a command changed.

``changeset.capture`` used to read the accounts table, every source transaction, the review queue, the staging files
and the learned context in full before and after each command. This module keeps a saved copy of those small tables
(the index, ``.change-capture.sqlite`` in the company folder) and finds the changed rows with logs.

How each part is found
    SQLite tables (``source_transactions``, ``accounts``)
        Before the command, the capture adds a log table and triggers to ``ledger.sqlite`` in one short transaction.
        The triggers only insert the key of each written row into the log. They fire for every connection, so a write
        that does not use ``LedgerStore`` is logged too. After the command, the capture reads the logged keys, reads
        only those rows, and removes the triggers and the log in one transaction. The triggers write no ledger row and
        change no ledger trigger, so they cannot open a way around the append-only triggers of the ledger
        (``append_only_guard_problems`` ignores them, and the check at the end of a capture refuses a store where
        the capture triggers or the log changed).
    Review queue item files
        The capture lists the folder with one ``stat`` for each item file (a name, a modification time, a size and an
        inode) and compares the list with the signatures in the index. It reads only the files that differ. A file
        that a helper wrote, a file that a command wrote in place, and a file that a person edited all show. The cost
        grows with the number of files in ``review-queue/`` (open items; a decided item is archived), not with the
        number of transactions. If a folder ``review-queue/archive`` exists, or two files could hold the same source
        id, the capture reads the whole queue.
    Staging files and learned context
        Five files. The capture compares the signature of each file (modification time, size, inode) with the index.
        A changed file is read in full and compared with the index, except ``seen-ids.json`` when its helper journaled
        the ids that it added or removed (``capture_log.writing``). The journal is a hint: a write that is not in the
        journal, or that left another signature than the last journaled write, makes the capture read the file.
    State digests
        ``state.before`` and ``state.after`` come from totals (the count and a sum of row hashes for each section) that
        the index keeps. A changed row changes the totals by its old and new hash. The index is valid only if the
        signature of each part is the signature that it saw at the end of the last capture. If a part changed in the
        meantime (a command without ``--change-set-out``, a restore), the capture reads that part in full once and
        builds the index again.

Every fallback reads more and never less: a bypassed write, a replaced ledger, a missing index and a store that
cannot take the triggers all give the document that a full comparison gives.
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import capture_log
from . import changeset
from .changeset import ChangeSetError
from .ledger.store import REWRITE_EVENT_TYPES

INDEX_NAME = ".change-capture.sqlite"
INDEX_VERSION = 3
LOG_TABLE = "_books_capture_log"
CTL_TABLE = "_books_capture_ctl"
TRIGGER_PREFIX = "_books_capture_"
_CHUNK = 400
_TICK_NS = 12_000_000  # A file system may give two writes in this span the same modification time.
_MISSING = object()

# The covered files, the section that each holds, and how to read its rows.
FILE_SECTIONS = {
    changeset._PENDING_FILE: "pending",
    changeset._DUPLICATES_FILE: "duplicates",
    changeset._STORE_PENDING_FILE: "store_pending",
    changeset._SEEN_FILE: "seen",
    changeset._LEARNED_FILE: "learned",
    changeset._CONTEXT_FILE: "context",
    **{name: "documents" for name in changeset.DOCUMENT_FILES},
}
LEDGER_SECTIONS = ("entries", "balances", "audit")
_QUEUE_FOLDER = changeset._QUEUE_DIR
#: The sections whose helpers journal the rows that they change, so the file is not read after the command.
DELTA_SECTIONS = frozenset({"pending", "seen"})


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Triggers and log in ledger.sqlite
# ---------------------------------------------------------------------------


def _trigger_sql() -> dict[str, str]:
    def trigger(name: str, event: str, table: str, body: str, timing: str = "AFTER") -> str:
        return f"CREATE TRIGGER {TRIGGER_PREFIX}{name} {timing} {event} ON main.{table} BEGIN {body} END"

    def note(tag: str, *keys: str) -> str:
        return f"INSERT INTO {LOG_TABLE} (tbl, key) VALUES " + ", ".join(f"('{tag}', {key})" for key in keys) + ";"

    conflicts = "INSERT INTO " + LOG_TABLE + " SELECT 'a', id FROM accounts WHERE {cond};"
    return {
        f"{TRIGGER_PREFIX}st_i": trigger("st_i", "INSERT", "source_transactions", note("s", "NEW.id")),
        f"{TRIGGER_PREFIX}st_u": trigger("st_u", "UPDATE", "source_transactions", note("s", "OLD.id", "NEW.id")),
        f"{TRIGGER_PREFIX}st_d": trigger("st_d", "DELETE", "source_transactions", note("s", "OLD.id")),
        f"{TRIGGER_PREFIX}ac_i": trigger("ac_i", "INSERT", "accounts", note("a", "NEW.id")),
        # A REPLACE on the unique name deletes another row without a DELETE trigger: log the rows that it can remove.
        f"{TRIGGER_PREFIX}ac_bi": trigger(
            "ac_bi", "INSERT", "accounts", conflicts.format(cond="id = NEW.id OR name = NEW.name"), "BEFORE"),
        f"{TRIGGER_PREFIX}ac_u": trigger("ac_u", "UPDATE", "accounts", note("a", "OLD.id", "NEW.id")),
        f"{TRIGGER_PREFIX}ac_bu": trigger(
            "ac_bu", "UPDATE", "accounts",
            conflicts.format(cond="(id = NEW.id OR name = NEW.name) AND id != OLD.id"), "BEFORE"),
        f"{TRIGGER_PREFIX}ac_d": trigger("ac_d", "DELETE", "accounts", note("a", "OLD.id")),
    }


def _norm(sql: str) -> str:
    return " ".join(str(sql).split())


def _capture_objects(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """The triggers and tables of the capture that the store holds, as (type, name)."""
    return [(str(kind), str(name)) for kind, name in conn.execute("SELECT type, name FROM sqlite_master")
            if str(name).startswith(TRIGGER_PREFIX)]


def _drop_capture_objects(conn: sqlite3.Connection) -> None:
    """Drop the capture log and triggers. It drops only objects with the capture prefix, never a ledger trigger."""
    for kind, name in _capture_objects(conn):
        if kind == "trigger":
            conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    for kind, name in _capture_objects(conn):
        if kind == "table":
            conn.execute(f'DROP TABLE IF EXISTS "{name}"')


def install_triggers(ledger: Path) -> str | None:
    """Add the log and the triggers in one transaction. Returns the run token, or ``None`` when that is not possible."""
    if not ledger.is_file():
        return None
    try:
        conn = sqlite3.connect(ledger, timeout=2.0, isolation_level=None)
    except sqlite3.Error:
        return None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _drop_capture_objects(conn)  # What a command that was killed left.
            present = {str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('source_transactions', 'accounts')")}
            if present != {"source_transactions", "accounts"}:
                conn.execute("ROLLBACK")
                return None
            token = secrets.token_hex(16)
            conn.execute(f"CREATE TABLE {CTL_TABLE} (token TEXT NOT NULL)")
            conn.execute(f"INSERT INTO {CTL_TABLE} (token) VALUES (?)", (token,))
            conn.execute(f"CREATE TABLE {LOG_TABLE} (tbl TEXT NOT NULL, key TEXT NOT NULL)")
            for sql in _trigger_sql().values():
                conn.execute(sql)
            conn.execute("COMMIT")
            return token
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def remove_triggers(ledger: Path) -> bool:
    """Remove the log and the triggers. Returns ``True`` when no capture object is left."""
    if not ledger.is_file():
        return True
    try:
        conn = sqlite3.connect(ledger, timeout=5.0, isolation_level=None)
    except sqlite3.Error:
        return False
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _drop_capture_objects(conn)
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return not _capture_objects(conn)
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _logged_keys(conn: sqlite3.Connection, token: str | None) -> tuple[list[int], list[str]] | None:
    """The keys that the triggers logged, or ``None`` when the log cannot be trusted (it is not the log of this run)."""
    if token is None:
        return None
    try:
        row = conn.execute(f"SELECT token FROM {CTL_TABLE}").fetchall()
        if [r[0] for r in row] != [token]:
            return None
        found = {str(name): _norm(sql) for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'") if str(name).startswith(TRIGGER_PREFIX)}
        if found != {name: _norm(sql) for name, sql in _trigger_sql().items()}:
            return None
        rows = conn.execute(f"SELECT DISTINCT tbl, key FROM {LOG_TABLE}").fetchall()
    except sqlite3.Error:
        return None
    return ([int(key) for tbl, key in rows if tbl == "a"], [str(key) for tbl, key in rows if tbl == "s"])


# ---------------------------------------------------------------------------
# The index (the saved copy of the small tables)
# ---------------------------------------------------------------------------


class Index:
    """``.change-capture.sqlite``: the rows of the small tables as the last capture saw them, and their totals."""

    def __init__(self, root: Path) -> None:
        self.path = root / INDEX_NAME
        self.conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.executescript(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;"
            "CREATE TABLE IF NOT EXISTS rows (section TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,"
            " PRIMARY KEY (section, key)) WITHOUT ROWID;"
            "CREATE TABLE IF NOT EXISTS item_files (name TEXT PRIMARY KEY, source_id TEXT NOT NULL, sig TEXT NOT NULL)"
            " WITHOUT ROWID;"
            "CREATE INDEX IF NOT EXISTS item_files_source ON item_files (source_id);"
            "CREATE TABLE IF NOT EXISTS archive_files (name TEXT PRIMARY KEY, sig TEXT NOT NULL) WITHOUT ROWID;"
            "CREATE TABLE IF NOT EXISTS archive_items (name TEXT PRIMARY KEY, archive TEXT NOT NULL,"
            " source_id TEXT NOT NULL, value TEXT NOT NULL) WITHOUT ROWID;"
            "CREATE INDEX IF NOT EXISTS archive_items_archive ON archive_items (archive);"
            "CREATE INDEX IF NOT EXISTS archive_items_source ON archive_items (source_id);")
        if self.meta("version") != str(INDEX_VERSION):
            self.wipe()
        self.totals: dict[str, list[int]] = {name: [0, 0] for name in changeset.DIGEST_SECTIONS}
        saved = self.meta("totals")
        if saved:
            try:
                loaded = json.loads(saved)
                self.totals = {name: [int(loaded[name][0]), int(loaded[name][1], 16)]
                               for name in changeset.DIGEST_SECTIONS}
            except (ValueError, KeyError, TypeError, IndexError):
                self.wipe()
        self.begin_write = False

    def close(self) -> None:
        try:
            if self.begin_write:
                self.conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        self.conn.close()

    # -- meta ---------------------------------------------------------------

    def meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    def token(self, name: str) -> Any:
        raw = self.meta("tok:" + name)
        return json.loads(raw) if raw is not None else _MISSING

    def set_token(self, name: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ("tok:" + name, _canon(value)))

    def counts(self) -> tuple[int, int, int] | None:
        raw = self.meta("counts")
        return tuple(json.loads(raw)) if raw else None  # type: ignore[return-value]

    def wipe(self) -> None:
        self.conn.executescript("DELETE FROM meta; DELETE FROM rows; DELETE FROM item_files; DELETE FROM archive_files; DELETE FROM archive_items;")
        self.conn.execute("INSERT INTO meta (key, value) VALUES ('version', ?)", (str(INDEX_VERSION),))
        self.totals = {name: [0, 0] for name in changeset.DIGEST_SECTIONS}

    def start_write(self) -> None:
        if not self.begin_write:
            self.conn.execute("BEGIN IMMEDIATE")
            self.begin_write = True

    def commit(self) -> None:
        self.set_meta_totals()
        if self.begin_write:
            self.conn.execute("COMMIT")
            self.begin_write = False

    def set_meta_totals(self) -> None:
        body = {name: [count, format(total % changeset._MODULUS, "x")] for name, (count, total) in self.totals.items()}
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('totals', ?)", (_canon(body),))

    def digest(self) -> str:
        return changeset.digest_from_totals(self.totals)

    # -- rows ---------------------------------------------------------------

    def section(self, name: str) -> dict[str, Any]:
        return {str(key): json.loads(value) for key, value in
                self.conn.execute("SELECT key, value FROM rows WHERE section = ?", (name,))}

    def lookup(self, name: str, keys: Sequence[str]) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for start in range(0, len(keys), _CHUNK):
            chunk = list(keys[start:start + _CHUNK])
            marks = ",".join("?" for _ in chunk)
            for key, value in self.conn.execute(
                    f"SELECT key, value FROM rows WHERE section = ? AND key IN ({marks})", [name, *chunk]):
                found[str(key)] = json.loads(value)
        return found

    def replace_section(self, name: str, rows: Mapping[str, Any]) -> None:
        """Make the index hold exactly ``rows`` for the section, and recompute its totals."""
        self.start_write()
        self.conn.execute("DELETE FROM rows WHERE section = ?", (name,))
        self.conn.executemany("INSERT INTO rows (section, key, value) VALUES (?, ?, ?)",
                              ((name, str(key), _canon(value)) for key, value in rows.items()))
        total = 0
        for key, value in rows.items():
            total += changeset.row_hash(name, _total_key(name, str(key), value), value)
        self.totals[name] = [len(rows), total % changeset._MODULUS]

    def set_total(self, name: str, total: Sequence[int]) -> None:
        """Set the total of a section whose rows the index does not keep (the ledger rows)."""
        self.start_write()
        self.totals[name] = [int(total[0]), int(total[1]) % changeset._MODULUS]

    def add_total(self, name: str, key: str, value: Any) -> None:
        count, total = self.totals[name]
        self.totals[name] = [count + 1, (total + changeset.row_hash(name, key, value)) % changeset._MODULUS]

    def apply(self, name: str, before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
        """Change the rows of the keys that ``before`` or ``after`` hold, and the totals, from their difference."""
        self.start_write()
        count, total = self.totals[name]
        for key in sorted(set(before) | set(after)):
            old, new = before.get(key, _MISSING), after.get(key, _MISSING)
            if old is not _MISSING and new is not _MISSING and old == new:
                continue
            if old is not _MISSING:
                total -= changeset.row_hash(name, _total_key(name, key, old), old)
                count -= 1
                if new is _MISSING:
                    self.conn.execute("DELETE FROM rows WHERE section = ? AND key = ?", (name, key))
            if new is not _MISSING:
                total += changeset.row_hash(name, _total_key(name, key, new), new)
                count += 1
                self.conn.execute("INSERT OR REPLACE INTO rows (section, key, value) VALUES (?, ?, ?)",
                                  (name, key, _canon(new)))
        self.totals[name] = [count, total % changeset._MODULUS]


def _total_key(section: str, key: str, value: Any) -> str:
    """The row key that the digest uses: an account by its name, every other row by its own key."""
    return str(value["name"]) if section == "accounts" else key


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------


def _db_token(ledger: Path) -> Any:
    """A token that changes with every write transaction of the ledger file (the change counter of its header)."""
    try:
        status = os.stat(ledger)
        with open(ledger, "rb") as handle:
            head = handle.read(100)
    except OSError:
        return None
    if len(head) < 100 or head[:16] != b"SQLite format 3\x00":
        return ["unreadable", status.st_size, status.st_mtime_ns]
    if head[18] == 2 or head[19] == 2:  # WAL: the header counter does not move, so the index never trusts it.
        return ["wal", time.time_ns()]
    return [int.from_bytes(head[24:28], "big"), status.st_size, status.st_mtime_ns]


def _has_archive(root: Path) -> bool:
    return (root / changeset._ARCHIVE_DIR).exists()


def _signatures(root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"sql": _db_token(root / "ledger.sqlite")}
    for relative in FILE_SECTIONS:
        result[relative] = capture_log.signature(root / relative)
    return result


def _wait_for_tick(signatures: Mapping[str, Any], newest: int = 0) -> None:
    """Make sure that no tracked file has a modification time inside the clock tick of now.

    A write after the capture start could otherwise get the same time as the write before it, and the signature would
    not show the change.
    """
    for name, value in signatures.items():
        if name != "sql" and isinstance(value, list) and value:
            newest = max(newest, int(value[0]))
    wait = newest + _TICK_NS - time.time_ns()
    if wait > 0:
        time.sleep(min(wait, _TICK_NS) / 1e9)


# ---------------------------------------------------------------------------
# Full reads (used to build or repair the index)
# ---------------------------------------------------------------------------


def _read_sql(ledger: Path) -> tuple[dict[int, dict[str, Any]], dict[str, tuple[str, str, str]], tuple[int, int, int]]:
    conn = changeset._connect_read_only(ledger)
    if conn is None:
        return {}, {}, (0, 0, 0)
    try:
        try:
            return changeset._load_accounts(conn), changeset._load_bank_index(conn), changeset._counts(conn)
        except sqlite3.DatabaseError as exc:
            raise ChangeSetError(f"The ledger store cannot be read: {exc}") from exc
    finally:
        conn.close()


def ledger_totals(ledger: Path) -> dict[str, list[int]]:
    """The totals of the ledger entries (with postings), balance assertions and audit events: a read in full."""
    conn = changeset._connect_read_only(ledger)
    if conn is None:
        return {name: [0, 0] for name in LEDGER_SECTIONS}
    try:
        try:
            return {
                "entries": changeset.section_total("entries", ((str(r["id"]), r) for r in changeset._load_entries(conn, 0))),
                "balances": changeset.section_total("balances", ((str(r["id"]), r) for r in changeset._load_balances(conn, 0))),
                "audit": changeset.section_total("audit", ((str(r["id"]), r) for r in changeset._load_audit(conn, 0))),
            }
        except sqlite3.DatabaseError as exc:
            raise ChangeSetError(f"The ledger store cannot be read: {exc}") from exc
    finally:
        conn.close()


def _read_file_section(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    if relative == changeset._PENDING_FILE or relative == changeset._STORE_PENDING_FILE:
        return changeset._load_keyed(path, "id")
    if relative == changeset._DUPLICATES_FILE:
        return changeset._load_keyed(path, "source_id")
    if relative == changeset._SEEN_FILE:
        return {key: None for key in changeset._load_ids(path)}
    if relative == changeset._CONTEXT_FILE:
        return changeset._load_context(root)
    if relative in changeset.DOCUMENT_FILES:
        value = changeset._read_strict(path)
        if value is None:
            return {}
        if not isinstance(value, changeset._DOCUMENT_TYPES[relative]):
            raise ChangeSetError(f"{relative} has an unexpected shape.")
        return {relative: value}
    return changeset._load_learned(root)


def _account_rows(accounts: Mapping[int, Mapping[str, Any]]) -> dict[str, Any]:
    return {str(key): dict(row) for key, row in accounts.items()}


def _bank_values(bank: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    return {key: list(value) for key, value in bank.items()}


def _queue_by_source(files: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for _, item in sorted(files.items()):
        items[str(item["source_id"])] = dict(item)
    return items


def _sweep(root: Path, folder: str = _QUEUE_FOLDER) -> dict[str, str]:
    """The signature of each ``*.json`` file in a folder, by file name: one ``stat`` for each file."""
    found: dict[str, str] = {}
    try:
        entries = os.scandir(root / folder)
    except OSError:
        return found
    with entries:
        for entry in entries:
            if entry.name.endswith(".json"):
                try:
                    status = entry.stat()
                except OSError:
                    continue
                found[entry.name] = f"{status.st_mtime_ns}:{status.st_size}:{status.st_ino}"
    return found


def _newest(sweep: Mapping[str, str]) -> int:
    return max((int(sig.split(":", 1)[0]) for sig in sweep.values()), default=0)


def _archive_reader() -> Any:
    from . import queue as queue_module
    return getattr(queue_module, "_read_archive_file", None)


def _store_queue(index: Index, files: Mapping[str, Mapping[str, Any]], sweep: Mapping[str, str],
                 archive_sweep: Mapping[str, str], root: Path) -> None:
    """Save the whole queue (``files`` by file name) and the signatures of its item files and archive files."""
    index.replace_section("queue", _queue_by_source(files))
    index.conn.execute("DELETE FROM item_files")
    index.conn.execute("DELETE FROM archive_files")
    index.conn.execute("DELETE FROM archive_items")
    top = {name for name in sweep}
    index.conn.executemany("INSERT OR REPLACE INTO item_files (name, source_id, sig) VALUES (?, ?, ?)",
                           ((name, str(item["source_id"]), sweep[name]) for name, item in files.items()
                            if name in top))
    index.conn.executemany("INSERT OR REPLACE INTO archive_files (name, sig) VALUES (?, ?)", archive_sweep.items())
    reader = _archive_reader()
    if reader is not None:
        for archive in sorted(archive_sweep):
            items = reader(root / changeset._ARCHIVE_DIR / archive)
            index.conn.executemany(
                "INSERT OR REPLACE INTO archive_items (name, archive, source_id, value) VALUES (?, ?, ?, ?)",
                ((name, archive, str(item["source_id"]), _canon(item)) for name, item in items.items()
                 if isinstance(item, dict) and "source_id" in item))


class _Slow(Exception):
    """The queue cannot be updated from the changed files alone: read the whole queue."""


def _sync_queue(index: Index, root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Make the index hold the review queue as it is on disk. Returns the old and the new rows of the changed ids.

    After a full read the rows are every row.
    """
    sweep = _sweep(root)
    archive_sweep = _sweep(root, changeset._ARCHIVE_DIR)
    saved = {str(name): str(sig) for name, sig in index.conn.execute("SELECT name, sig FROM item_files")}
    saved_archive = {str(name): str(sig) for name, sig in index.conn.execute("SELECT name, sig FROM archive_files")}
    names = sorted(name for name in set(sweep) | set(saved) if sweep.get(name) != saved.get(name))
    archives = sorted(name for name in set(archive_sweep) | set(saved_archive)
                      if archive_sweep.get(name) != saved_archive.get(name))
    if not names and not archives:
        return {}, {}
    if _has_group_journal(root):
        return _queue_in_full(index, root, sweep, archive_sweep)
    try:
        return _sync_queue_names(index, root, names, archives, sweep, archive_sweep)
    except _Slow:
        return _queue_in_full(index, root, sweep, archive_sweep)


def _has_group_journal(root: Path) -> bool:
    try:
        return any(os.scandir(root / queue_transaction_dir()))
    except OSError:
        return False


def queue_transaction_dir() -> str:
    from . import queue as queue_module
    return getattr(queue_module, "_QUEUE_TRANSACTION_DIR", "review-queue/.transactions")


def _queue_in_full(index: Index, root: Path, sweep: Mapping[str, str],
                   archive_sweep: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    old_rows = index.section("queue")
    files = changeset.load_queue_files(root) if root.is_dir() else {}
    _store_queue(index, files, sweep, archive_sweep, root)
    return old_rows, _queue_by_source(files)


def _sync_queue_names(index: Index, root: Path, names: Sequence[str], archives: Sequence[str],
                      sweep: Mapping[str, str], archive_sweep: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    from .queue import _sanitize_filename
    reader = _archive_reader()
    if archives and reader is None:
        raise _Slow()
    conn = index.conn

    def rows(sql: str, values: Sequence[Any]) -> list[tuple[Any, ...]]:
        found: list[tuple[Any, ...]] = []
        for start in range(0, max(len(values), 1), _CHUNK):
            chunk = list(values[start:start + _CHUNK])
            found.extend(conn.execute(sql.format(",".join("?" for _ in chunk)), chunk).fetchall())
        return found

    # The archives that changed: read them (a bad archive raises), and find every name that they touch.
    new_archive_rows: list[tuple[str, str, str, str]] = []
    for archive in archives:
        if archive not in archive_sweep:
            continue
        try:
            items = reader(root / changeset._ARCHIVE_DIR / archive)
        except (ValueError, OSError) as exc:
            raise ChangeSetError(f"The review queue cannot be read: {exc}") from exc
        for name, item in items.items():
            if isinstance(item, dict) and "source_id" in item:
                new_archive_rows.append((str(name), archive, str(item["source_id"]), _canon(item)))
    old_archive_names = [str(r[0]) for r in rows("SELECT name FROM archive_items WHERE archive IN ({})", archives)] \
        if archives else []
    affected = sorted(set(names) | set(old_archive_names) | {row[0] for row in new_archive_rows})
    # The effective source id of each affected name before: its item file, else its archive entry.
    old_top = {str(n): str(sid) for n, sid in rows("SELECT name, source_id FROM item_files WHERE name IN ({})", affected)}
    old_arch = {str(n): str(sid) for n, sid in rows("SELECT name, source_id FROM archive_items WHERE name IN ({})", affected)}
    old_ids = {name: old_top.get(name, old_arch.get(name)) for name in affected}
    # Apply the archive changes to the saved archive rows.
    for archive in archives:
        conn.execute("DELETE FROM archive_items WHERE archive = ?", (archive,))
        conn.execute("DELETE FROM archive_files WHERE name = ?", (archive,))
    for name, archive, sid, value in new_archive_rows:
        other = conn.execute("SELECT archive FROM archive_items WHERE name = ?", (name,)).fetchone()
        if other is not None:
            raise _Slow()  # Two archives hold one file name: the later one wins in a full read.
        conn.execute("INSERT INTO archive_items (name, archive, source_id, value) VALUES (?, ?, ?, ?)",
                     (name, archive, sid, value))
    conn.executemany("INSERT OR REPLACE INTO archive_files (name, sig) VALUES (?, ?)",
                     [(a, archive_sweep[a]) for a in archives if a in archive_sweep])
    # The item files that changed.
    new_top: dict[str, tuple[str, dict[str, Any]]] = {}
    for name in names:
        if name not in sweep:
            continue
        item = changeset._queue_item(changeset._read_json(root / _QUEUE_FOLDER / name))
        if item is None:
            raise ChangeSetError("A review item file cannot be read: " + name)
        source_id = str(item["source_id"])
        if name != _sanitize_filename(source_id) + ".json":
            raise _Slow()  # The name does not follow the source id: another file could hold the same id.
        new_top[name] = (source_id, dict(item))
    conn.executemany("DELETE FROM item_files WHERE name = ?", [(name,) for name in names])
    conn.executemany("INSERT OR REPLACE INTO item_files (name, source_id, sig) VALUES (?, ?, ?)",
                     [(name, sid, sweep[name]) for name, (sid, _) in new_top.items()])
    # The effective item of each affected name after: its item file (changed or not), else its archive entry.
    new_rows: dict[str, Any] = {}
    new_ids: dict[str, str | None] = {}
    for name in affected:
        if name in new_top:
            new_ids[name], new_rows[new_top[name][0]] = new_top[name][0], new_top[name][1]
            continue
        top = conn.execute("SELECT source_id FROM item_files WHERE name = ?", (name,)).fetchone()
        if top is not None:
            new_ids[name] = str(top[0])
            new_rows[str(top[0])] = index.lookup("queue", [str(top[0])]).get(str(top[0]), _MISSING)
            if new_rows[str(top[0])] is _MISSING:
                raise _Slow()
            continue
        arch = conn.execute("SELECT source_id, value FROM archive_items WHERE name = ?", (name,)).fetchone()
        new_ids[name] = str(arch[0]) if arch is not None else None
        if arch is not None:
            new_rows[str(arch[0])] = json.loads(arch[1])
    touched = {sid for sid in (*old_ids.values(), *new_ids.values()) if sid is not None}
    for sid in touched:  # Another unchanged name with the same source id makes the result depend on the file order.
        holders = {str(r[0]) for r in conn.execute("SELECT name FROM item_files WHERE source_id = ?", (sid,))}
        holders |= {str(r[0]) for r in conn.execute("SELECT name FROM archive_items WHERE source_id = ?", (sid,))}
        if holders - set(affected):
            raise _Slow()
    old_rows = index.lookup("queue", sorted(touched))
    index.apply("queue", old_rows, new_rows)
    return old_rows, new_rows


def _rebuild(index: Index, root: Path, group: str) -> None:
    """Read one part of the books in full and make the index hold it."""
    index.start_write()
    if group == "sql":
        accounts, bank, counts = _read_sql(root / "ledger.sqlite")
        index.replace_section("accounts", _account_rows(accounts))
        index.replace_section("bank", _bank_values(bank))
        for name, total in ledger_totals(root / "ledger.sqlite").items():
            index.set_total(name, total)
        index.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('counts', ?)", (_canon(list(counts)),))
    elif FILE_SECTIONS[group] == "documents":
        index.apply("documents", index.lookup("documents", [group]), _read_file_section(root, group))
    else:
        index.replace_section(FILE_SECTIONS[group], _read_file_section(root, group))


# ---------------------------------------------------------------------------
# One capture
# ---------------------------------------------------------------------------


def start(root: Path) -> "Run | None":
    """Prepare the capture before a command. ``None`` means that the incremental capture is not possible here."""
    if not root.is_dir():
        return None
    try:
        index = Index(root)
    except (sqlite3.Error, OSError):
        return None  # The folder cannot hold the index: the caller compares two full reads.
    try:
        signatures = _signatures(root)
        _wait_for_tick(signatures, _newest(_sweep(root)))
        signatures = _signatures(root)
        for group, current in signatures.items():
            if index.token(group) != current:
                _rebuild(index, root, group)
                index.set_token(group, current)
        index.start_write()
        _sync_queue(index, root)
        index.commit()
    except ChangeSetError:
        index.close()
        raise  # An unreadable part is the same failure as in a full read: the caller reports ``state_unreadable``.
    except (sqlite3.Error, OSError):
        index.close()
        return None
    conn = changeset._connect_read_only(root / "ledger.sqlite")
    try:
        position = changeset._position(conn)
        problems = _guard_problems(conn)
    except sqlite3.Error as exc:
        index.close()
        raise ChangeSetError(f"The ledger store cannot be read: {exc}") from exc
    finally:
        if conn is not None:
            conn.close()
    run = Run(root, index, signatures, position, index.digest(), index.counts() or (0, 0, 0), bool(problems))
    run.begin()
    return run


def _real_counts(root: Path) -> tuple[int, int, int]:
    conn = changeset._connect_read_only(root / "ledger.sqlite")
    try:
        return changeset._counts(conn)
    finally:
        if conn is not None:
            conn.close()


def _guard_problems(conn: sqlite3.Connection | None) -> list[str]:
    if conn is None:
        return []
    try:
        from .ledger.store import append_only_guard_problems
        return list(append_only_guard_problems(conn))
    except Exception as exc:  # noqa: BLE001 - an unknown store layout is a store without the guard
        return [str(exc)]


class Run:
    """The state of one capture between ``start`` and ``finish``."""

    def __init__(self, root: Path, index: Index, signatures: dict[str, Any], position: dict[str, Any],
                 digest: str, counts: tuple[int, int, int], guard_problems: bool) -> None:
        self.root, self.index, self.start_signatures = root, index, signatures
        self.position, self.digest, self.counts = position, digest, counts
        self.guard_problems_before = guard_problems
        self.token: str | None = None
        self.journal: capture_log.Session | None = None
        self.cleaned = False
        self.ledger_full_read = False
        self.closed = False

    def begin(self) -> None:
        self.index.close()  # No file stays open during the command.
        self.token = install_triggers(self.root / "ledger.sqlite")
        self.journal = capture_log.begin(self.root, self.start_signatures)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.journal is not None:
            capture_log.end(self.journal)
        if not self.cleaned:
            remove_triggers(self.root / "ledger.sqlite")
        try:
            self.index.close()
        except sqlite3.Error:
            pass

    # -- finish ---------------------------------------------------------------

    def finish(self, failed: bool, books_revision: int | None, base_revision: int | None = None) -> dict[str, Any]:
        root, journal = self.root, self.journal
        assert journal is not None
        capture_log.end(journal)  # Later writes (the cleanup below) are not part of the command.
        self.index = Index(root)
        self.index.start_write()
        index = self.index
        before = changeset._Side(self.position, self.counts, {}, {}, {}, {}, {}, {}, {}, {}, [], {}, self.digest)
        after = changeset._Side({}, None, {}, {}, {}, {}, {}, {}, {}, {}, [], {}, "")
        slow_sql = self._sql(before, after)
        self._queue(before, after)
        self._files(before, after)
        # Position and counts of the ledger after the command.
        conn = changeset._connect_read_only(root / "ledger.sqlite")
        try:
            after.position = changeset._position(conn)
            problems = _guard_problems(conn)
            real_counts = None
            if conn is not None and (slow_sql or problems or self.guard_problems_before):
                real_counts = changeset._counts(conn)
        finally:
            if conn is not None:
                conn.close()
        after.counts = real_counts
        before.counts = self.counts if real_counts is not None else None
        ledger_full = bool(slow_sql or problems or self.guard_problems_before)
        if conn is None:
            for name in LEDGER_SECTIONS:
                index.set_total(name, [0, 0])
        after.digest_value = index.digest()

        def after_digest(document: dict[str, Any]) -> str:
            self._ledger_totals(document, before, after, ledger_full)
            return index.digest()

        document = changeset._build(before, after, root, books_revision, failed, base_revision, after_digest)
        # Save the counts for the next capture.
        if real_counts is not None:
            counts = real_counts
        else:
            counts = (self.counts[0] + len(document["entries_added"]),
                      self.counts[1] + sum(len(entry["postings"]) for entry in document["entries_added"]),
                      self.counts[2] + len(document["balance_assertions_added"]))
        if document["reset"] and real_counts is None:
            counts = _real_counts(root)
        index.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('counts', ?)", (_canon(list(counts)),))
        # Remove the log, then record the signatures that the next capture compares with.
        capture_log.end(journal)
        self.journal = None
        self.cleaned = remove_triggers(root / "ledger.sqlite")
        for group, current in _signatures(root).items():
            index.set_token(group, current)
        index.commit()
        index.close()
        return document

    # -- ledger totals --------------------------------------------------------------

    def _ledger_totals(self, document: Mapping[str, Any], before: "changeset._Side", after: "changeset._Side",
                       full: bool) -> None:
        """Update the totals of the ledger sections from the rows that the command added.

        Posted rows are append-only, so the new rows are the whole change, with three exceptions that make the capture
        read the ledger in full: a reset, an account rename (it relabels posted rows), and an audit chain that moved
        back (the newest ``feed-intake`` event can be discarded). A store without the append-only triggers, and a log
        that cannot be trusted, also read in full.
        """
        index = self.index
        renamed = bool(document["accounts"]["renamed"]) or any(
            event["type"] in REWRITE_EVENT_TYPES for event in document["audit"]["events"])
        rewound = (after.position["audit_event_id"] or 0) < (before.position["audit_event_id"] or 0)
        if full or document["reset"] or renamed or rewound:
            self.ledger_full_read = True
            for name, total in ledger_totals(self.root / "ledger.sqlite").items():
                index.set_total(name, total)
            return
        for entry in document["entries_added"]:
            index.add_total("entries", str(entry["id"]), entry)
        for row in document["balance_assertions_added"]:
            index.add_total("balances", str(row["id"]), row)
        for event in document["audit"]["events"]:
            index.add_total("audit", str(event["id"]), event)

    # -- SQLite tables ----------------------------------------------------------

    def _sql(self, before: "changeset._Side", after: "changeset._Side") -> bool:
        """Fill the account and bank rows of both sides. Returns ``True`` when the tables were read in full."""
        root, index = self.root, self.index
        ledger = root / "ledger.sqlite"
        conn = changeset._connect_read_only(ledger) if ledger.is_file() else None
        keys = _logged_keys(conn, self.token) if conn is not None else None
        try:
            if conn is None:
                if self.start_signatures["sql"] is None:
                    return False  # No store before, none after.
                return self._sql_full(before, after, conn)
            if keys is None:
                return self._sql_full(before, after, conn)
            account_ids, bank_ids = keys
            old_accounts = index.lookup("accounts", [str(key) for key in account_ids])
            old_bank = index.lookup("bank", bank_ids)
            new_accounts: dict[str, Any] = {}
            if account_ids:
                for start in range(0, len(account_ids), _CHUNK):
                    chunk = account_ids[start:start + _CHUNK]
                    marks = ",".join("?" for _ in chunk)
                    for row in conn.execute(f"SELECT * FROM accounts WHERE id IN ({marks})", chunk):  # type: ignore[union-attr]
                        fields = row.keys()
                        new_accounts[str(int(row["id"]))] = {
                            "name": str(row["name"]), "currency": str(row["currency"]),
                            "open_date": str(row["open_date"]),
                            "financial_kind": row["financial_kind"] if "financial_kind" in fields else None}
            new_bank: dict[str, Any] = {}
            for start in range(0, len(bank_ids), _CHUNK):
                chunk = bank_ids[start:start + _CHUNK]
                marks = ",".join("?" for _ in chunk)
                for row in conn.execute(  # type: ignore[union-attr]
                        f"SELECT id, status, imported_at, payload_json FROM source_transactions WHERE id IN ({marks})",
                        chunk):
                    new_bank[str(row[0])] = [str(row[1]), str(row[2]), changeset._payload_hash(row[3])]
            self._sides_sql(before, after, old_accounts, new_accounts, old_bank, new_bank)
            index.apply("accounts", old_accounts, new_accounts)
            index.apply("bank", old_bank, new_bank)
            return False
        finally:
            if conn is not None:
                conn.close()

    def _sql_full(self, before: "changeset._Side", after: "changeset._Side", conn: Any) -> bool:
        index = self.index
        accounts, bank, _ = _read_sql(self.root / "ledger.sqlite")
        old_accounts, old_bank = index.section("accounts"), index.section("bank")
        new_accounts, new_bank = _account_rows(accounts), _bank_values(bank)
        self._sides_sql(before, after, old_accounts, new_accounts, old_bank, new_bank)
        index.replace_section("accounts", new_accounts)
        index.replace_section("bank", new_bank)
        return True

    @staticmethod
    def _sides_sql(before: "changeset._Side", after: "changeset._Side", old_accounts: Mapping[str, Any],
                   new_accounts: Mapping[str, Any], old_bank: Mapping[str, Any], new_bank: Mapping[str, Any]) -> None:
        before.accounts = {int(key): dict(row) for key, row in old_accounts.items()}
        after.accounts = {int(key): dict(row) for key, row in new_accounts.items()}
        before.bank = {key: tuple(value) for key, value in old_bank.items()}  # type: ignore[misc]
        after.bank = {key: tuple(value) for key, value in new_bank.items()}  # type: ignore[misc]

    # -- review queue -----------------------------------------------------------

    def _queue(self, before: "changeset._Side", after: "changeset._Side") -> None:
        old_rows, new_rows = _sync_queue(self.index, self.root)
        before.queue, after.queue = old_rows, new_rows

    # -- staging files and learned context ----------------------------------------

    def _files(self, before: "changeset._Side", after: "changeset._Side") -> None:
        root, index, journal = self.root, self.index, self.journal
        assert journal is not None
        for relative, section in FILE_SECTIONS.items():
            current = capture_log.signature(root / relative)
            if current == self.start_signatures[relative]:
                continue
            log = journal.files.get(relative)
            old_rows: dict[str, Any]
            new_rows: dict[str, Any]
            if (section in DELTA_SECTIONS and log is not None and log.chain_ok and log.exact
                    and log.last_after == current):
                # Every write came from a helper that named its rows, and nothing else wrote the file.
                touched = sorted(set(log.upsert) | log.remove)
                old_rows = index.lookup(section, touched)
                new_rows = {key: row for key, row in log.upsert.items() if key not in log.remove}
                new_rows = {key: copy.deepcopy(row) for key, row in new_rows.items()}
            elif section == "documents":
                old_rows = index.lookup("documents", [relative])
                new_rows = _read_file_section(root, relative)
            else:
                old_rows = index.section(section)
                new_rows = _read_file_section(root, relative)
            if section == "seen":
                before.seen, after.seen = sorted(old_rows), sorted(new_rows)
            elif section == "documents":
                before.documents.update(old_rows)
                after.documents.update(new_rows)
            else:
                setattr(before, section, old_rows)
                setattr(after, section, new_rows)
            index.apply(section, old_rows, new_rows)
