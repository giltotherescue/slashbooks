"""A journal of the covered files that a command writes through the engine's own helpers.

A change set (``bookkeeping.changeset``) lists the rows that a command changed. To find the changed JSON rows without
a read of every file, the engine's write helpers wrap each write of a covered file in ``writing(path)``. While a
capture is active, the journal keeps for each path the file signature (modification time, size, inode) before the first
write and after the last write, and, when the helper knows the rows that it changed, the row keys (the delta).

Outside a capture every function here returns at once and changes nothing.

The journal is a hint, never the proof. A write that does not use a helper is not in the journal. The change set code
checks the file signatures after the command (a cheap ``stat`` of each covered file).
A file that changed without a journal entry, or whose signature is not the signature that the last journaled write left,
makes the change set code read that section in full and compare it with the saved copy. Correctness does not depend on
every caller using a helper.
"""

from __future__ import annotations

import copy
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

_LOCK = threading.RLock()
_SESSIONS: list["Session"] = []


def signature(path: Path | str) -> list[int] | None:
    """Modification time (ns), size, inode and change time (ns) of a file, or ``None`` when it does not exist.

    The change time moves with every write and cannot be set back, so an edit of the same length with the old
    modification time restored (``os.utime``) still gives another signature.
    """
    try:
        status = os.stat(path)
    except OSError:
        return None
    return [status.st_mtime_ns, status.st_size, status.st_ino, status.st_ctime_ns]


class FileLog:
    """What the helpers did to one path during the command."""

    __slots__ = ("first_before", "last_after", "writes", "chain_ok", "exact", "upsert", "remove")

    def __init__(self, before: list[int] | None) -> None:
        self.first_before = before
        self.last_after: list[int] | None = before
        self.writes = 0
        self.chain_ok = True  # False when the file changed between two journaled writes.
        self.exact = True  # True while every write came with a row delta.
        self.upsert: dict[str, Any] = {}
        self.remove: set[str] = set()

    def merge(self, delta: Mapping[str, Any] | None) -> None:
        self.writes += 1
        if delta is None:
            self.exact = False
            return
        for key in delta.get("remove") or ():
            self.upsert.pop(str(key), None)
            self.remove.add(str(key))
        for key, row in (delta.get("upsert") or {}).items():
            self.remove.discard(str(key))
            # A copy: a caller that changes the object that it gave (or got back) must not change the receipt.
            self.upsert[str(key)] = copy.deepcopy(row)


class Session:
    """The journal of one capture. ``start`` holds the signatures that the capture saw before the command."""

    def __init__(self, root: Path, start: Mapping[str, list[int] | None]) -> None:
        self.root = os.path.realpath(root)
        self.start = dict(start)
        self.files: dict[str, FileLog] = {}
        self.closed = False

    def relative(self, resolved: str) -> str | None:
        prefix = self.root + os.sep
        return resolved[len(prefix):] if resolved.startswith(prefix) else None

    def record(self, resolved: str, before: list[int] | None, after: list[int] | None,
               delta: Mapping[str, Any] | None) -> None:
        if self.closed:
            return
        relative = self.relative(resolved)
        if relative is None:
            return
        with _LOCK:
            log = self.files.get(relative)
            if log is None:
                log = self.files[relative] = FileLog(self.start.get(relative, before))
                log.last_after = log.first_before
            if before != log.last_after:
                log.chain_ok = False  # Something wrote the file since the last journaled write.
            log.last_after = after
            log.merge(delta)


def begin(root: Path | str, start: Mapping[str, list[int] | None]) -> Session:
    session = Session(Path(root), start)
    with _LOCK:
        _SESSIONS.append(session)
    return session


def end(session: Session) -> None:
    session.closed = True
    with _LOCK:
        if session in _SESSIONS:
            _SESSIONS.remove(session)


def active() -> bool:
    return bool(_SESSIONS)


@contextmanager
def writing(path: Path | str, *, delta: Mapping[str, Any] | None = None) -> Iterator[None]:
    """Wrap the write of one covered file in a helper. ``delta`` names the rows that the write changed.

    ``delta`` is ``{"upsert": {key: row}, "remove": [key, ...]}`` with the row keys of the section that the file holds
    (for ``seen-ids.json``, the id as the key and ``None`` as the row). Without a delta the change set code reads the
    file. Call it around the whole write, including the temporary file, so that the signature before the write is the
    signature that the previous write left.    """
    if not _SESSIONS:
        yield
        return
    absolute = os.path.abspath(path)
    resolved = os.path.join(os.path.realpath(os.path.dirname(absolute)), os.path.basename(absolute))
    with _LOCK:
        sessions = [s for s in _SESSIONS if s.relative(resolved) is not None]
    if not sessions:
        yield
        return
    before = signature(resolved)
    completed = False
    try:
        yield
        completed = True
    finally:
        after = signature(resolved)
        for session in sessions:
            # A write that raised may have changed the file in a way that its delta does not describe: no delta.
            session.record(resolved, before, after, delta if completed else None)


# ---------------------------------------------------------------------------
# Ledger writes
# ---------------------------------------------------------------------------
#
# A change set capture logs the keys of the account rows and the bank rows that a command writes, with a log table
# and triggers in ``ledger.sqlite`` (``bookkeeping.change_capture``). It adds them only when the command changes a
# row, so a command that changes nothing leaves the ledger file byte for byte as it was. The engine opens its ledger
# connections with ``LedgerStore.connect``, which asks ``ledger_connection_factory`` for the connection class. While a
# capture watches the ledger, the class is ``WatchedConnection``. Before the first statement that changes a row, the
# connection takes the write lock, lets the capture check the file change counter, and runs the statement inside a
# savepoint. If the statement changed a row, it undoes the statement, lets the capture add the log in the same
# transaction, and runs the statement again, so that the log holds the row. A statement that changes no row
# (``INSERT OR IGNORE`` of a known id, a ``DELETE`` that matches nothing) adds nothing.
#
# The watch is a hint, never the proof. A write on another connection before the log exists moves the file change
# counter, and the capture then reads the tables in full. After the log exists its triggers fire for every
# connection.

_WATCHES: dict[str, Any] = {}
_PROBE = "books_capture_probe"
_WORD = re.compile(r"\s*([A-Za-z]+)")
_ROW_WORDS = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE"})
_SCHEMA_WORDS = frozenset({"CREATE", "DROP", "ALTER", "REINDEX", "VACUUM"})
_WRITE_IN_WITH = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)
_ROLLBACK_TO = re.compile(r"\s*ROLLBACK\s+(?:TRANSACTION\s+)?TO\b", re.IGNORECASE)
_TEMP = re.compile(r"\s*CREATE\s+TEMP(?:ORARY)?\b", re.IGNORECASE)
_NAME = r"(?:main\s*\.\s*)?[\"`\[]?([A-Za-z0-9_]+)"
_CREATE_IF = re.compile(r"\s*CREATE\s+(?:UNIQUE\s+)?(TABLE|INDEX|TRIGGER|VIEW)\s+IF\s+NOT\s+EXISTS\s+" + _NAME,
                        re.IGNORECASE)
_DROP_IF = re.compile(r"\s*DROP\s+(TABLE|INDEX|TRIGGER|VIEW)\s+IF\s+EXISTS\s+" + _NAME, re.IGNORECASE)


def watch_ledger(path: Path | str, watch: Any) -> str:
    """Watch the ledger at ``path`` for its first row change. Returns the key for ``unwatch_ledger``.

    ``watch`` has the methods ``locked(conn)``, ``install(conn)``, ``schema_write(text)``, ``unwatched(reason)`` and
    ``rolled_back()`` (see ``change_capture.Run``).
    """
    key = os.path.realpath(path)
    with _LOCK:
        _WATCHES[key] = watch
    return key


def unwatch_ledger(key: str | None, watch: Any) -> None:
    if key is None:
        return
    with _LOCK:
        if _WATCHES.get(key) is watch:
            del _WATCHES[key]


def ledger_connection_factory(path: Path | str) -> type[sqlite3.Connection]:
    """The connection class for a ledger file: ``WatchedConnection`` while a capture watches it, else the plain class."""
    if not _WATCHES:
        return sqlite3.Connection
    return WatchedConnection if os.path.realpath(path) in _WATCHES else sqlite3.Connection


def statement_kind(sql: str) -> str:
    """``row`` (a statement that can change rows), ``schema``, ``commit``, ``rollback`` or ``other``."""
    match = _WORD.match(sql)
    word = match.group(1).upper() if match else ""
    if word in _ROW_WORDS:
        return "row"
    if word == "WITH":
        return "row" if _WRITE_IN_WITH.search(sql) else "other"
    if word in _SCHEMA_WORDS:
        return "other" if _TEMP.match(sql) else "schema"
    if word in ("COMMIT", "END"):
        return "commit"
    if word == "ROLLBACK":
        return "other" if _ROLLBACK_TO.match(sql) else "rollback"
    return "other"  # SELECT, PRAGMA, BEGIN, SAVEPOINT, RELEASE, ATTACH, ...


class WatchedConnection(sqlite3.Connection):
    """A ledger connection that lets a capture add its log before the first row change (see above).

    Statements that cannot change a row run as on a plain connection. A statement through a cursor of
    ``Connection.cursor()`` is not watched; the capture sees its change with the row change counter of the connection
    or with the file change counter, and then reads the tables in full.
    """

    def __init__(self, database: Any, *args: Any, **kwargs: Any) -> None:
        super().__init__(database, *args, **kwargs)
        self._books_key = os.path.realpath(database)
        self._books_holds: Any = None  # The watch whose log the open transaction of this connection added.
        self._books_seen = self.total_changes

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:  # type: ignore[override]
        return self._books_run(sql, lambda: sqlite3.Connection.execute(self, sql, parameters))

    def executemany(self, sql: str, parameters: Any, /) -> sqlite3.Cursor:  # type: ignore[override]
        if self._books_watch() is not None:
            parameters = list(parameters)  # The watch can run the statement twice: an iterator must not run dry.
        return self._books_run(sql, lambda: sqlite3.Connection.executemany(self, sql, parameters))

    def executescript(self, script: str, /) -> sqlite3.Cursor:  # type: ignore[override]
        watch = self._books_watch()
        if watch is not None:
            watch.schema_write("a script")  # A script runs its statements outside the watch.
        try:
            return sqlite3.Connection.executescript(self, script)
        finally:
            self._books_holds = None  # A script commits the open transaction first.
            self._books_seen = self.total_changes

    def commit(self) -> None:
        sqlite3.Connection.commit(self)
        self._books_holds = None

    def rollback(self) -> None:
        was_open = self._books_open()
        try:
            sqlite3.Connection.rollback(self)
        finally:
            self._books_ended(was_open)

    def close(self) -> None:
        was_open = self._books_open()
        try:
            sqlite3.Connection.close(self)  # An open transaction is rolled back.
        finally:
            self._books_ended(was_open)

    def __exit__(self, kind: Any, value: Any, traceback: Any) -> Any:
        was_open = self._books_open()
        try:
            return sqlite3.Connection.__exit__(self, kind, value, traceback)
        finally:
            if not self._books_open():
                if kind is None:
                    self._books_holds = None  # Committed.
                else:
                    self._books_ended(was_open)

    def _books_open(self) -> bool:
        try:
            return self.in_transaction
        except sqlite3.ProgrammingError:  # A closed connection has no transaction.
            return False

    def _books_ended(self, was_open: bool = True) -> None:
        """The open transaction ended without a commit (when ``was_open``): the log of the capture went with it."""
        holds, self._books_holds = self._books_holds, None
        if holds is not None and was_open:
            holds.rolled_back()  # The capture watches again for the next row change.

    def _books_watch(self) -> Any:
        return _WATCHES.get(self._books_key) if _WATCHES else None

    def _books_run(self, sql: str, run: Callable[[], sqlite3.Cursor]) -> sqlite3.Cursor:
        watch = self._books_watch()
        try:
            if watch is None and self._books_holds is None:
                return run()
            kind = statement_kind(sql) if isinstance(sql, str) else "other"
            if kind == "commit":
                cursor = run()
                if not self.in_transaction:
                    self._books_holds = None
                return cursor
            if kind == "rollback":
                try:
                    return run()
                finally:
                    if not self.in_transaction:
                        self._books_ended()
            if watch is None:
                return run()
            if self.total_changes != self._books_seen:
                watch.unwatched("sql:a row change on the connection outside the watch")
                return run()
            if kind == "schema":
                if not self._books_no_op_schema(sql):
                    watch.schema_write(sql)
                return run()
            if kind != "row":
                return run()
            return self._books_probe(watch, run)
        finally:
            self._books_seen = self.total_changes

    def _books_no_op_schema(self, sql: str) -> bool:
        """True for ``CREATE ... IF NOT EXISTS`` of an object that exists and ``DROP ... IF EXISTS`` of one that does not."""
        for pattern, exists in ((_CREATE_IF, True), (_DROP_IF, False)):
            match = pattern.match(sql)
            if match:
                found = sqlite3.Connection.execute(
                    self, "SELECT 1 FROM main.sqlite_master WHERE type = ? AND lower(name) = lower(?)",
                    (match.group(1).lower(), match.group(2))).fetchone()
                return (found is not None) == exists
        return False

    def _books_probe(self, watch: Any, run: Callable[[], sqlite3.Cursor]) -> sqlite3.Cursor:
        if self.isolation_level is None and not self.in_transaction:
            watch.unwatched("sql:a row change in autocommit mode")
            return run()
        # Take the write lock first. No other connection can commit from here to the end of this transaction, so the
        # file change counter that the watch reads now is the counter that the log starts from. Python would open a
        # deferred transaction for this statement; an immediate one only takes the same lock a moment earlier.
        try:
            if self.in_transaction:
                sqlite3.Connection.execute(self, "DELETE FROM main.accounts WHERE 0")
            else:
                sqlite3.Connection.execute(self, "BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            watch.unwatched(f"sql:the write lock was not available to the watch ({exc})")
            return run()
        if not watch.locked(self):
            return run()
        before = self.total_changes
        sqlite3.Connection.execute(self, f"SAVEPOINT {_PROBE}")
        try:
            cursor = run()
        except BaseException:
            self._books_unwind()
            raise
        if self.total_changes == before:
            sqlite3.Connection.execute(self, f"RELEASE {_PROBE}")  # No row changed: nothing to log.
            return cursor
        self._books_unwind()
        if watch.install(self):
            self._books_holds = watch
        return run()

    def _books_unwind(self) -> None:
        for statement in (f"ROLLBACK TO {_PROBE}", f"RELEASE {_PROBE}"):
            try:
                sqlite3.Connection.execute(self, statement)
            except sqlite3.Error:
                pass  # The statement ended the transaction itself (for example ON CONFLICT ROLLBACK).
