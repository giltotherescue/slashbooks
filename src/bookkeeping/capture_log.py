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

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

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
            self.upsert[str(key)] = row


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
    try:
        yield
    finally:
        after = signature(resolved)
        for session in sessions:
            session.record(resolved, before, after, delta)


