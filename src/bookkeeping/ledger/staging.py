from __future__ import annotations

"""Staging layer for pending transactions.

Pending transactions live **only** here — never in the ledger.

Storage layout (inside entity.staging_dir):
    pending.json            Active pending transactions.
    seen-ids.json           Set of source IDs that have been imported into the
                            ledger (posted entries).  This is the authoritative
                            dedup store; source ID is the sole dedup key for
                            posted entries (KTD scoping rule).
    pending.json.tmp        Orphaned temp file from an interrupted write.
    seen-ids.json.tmp       Same.

All writes are atomic: write to <name>.tmp, then os.replace into place.
A startup check flags orphaned .tmp siblings (files present when the
staging layer is opened).

Correlation key (for pending→posted supersession only):
    (accountId, amount, date[:10], normalize_description(description))
This is NEVER used to dedup posted-vs-posted.

Public API
----------
StagingStore(staging_dir, audit_log=None)
    .check_orphaned_tmps() -> list[str]   paths of orphaned .tmp files
    .add_pending(txn) -> None
    .get_pending() -> list[dict]
    .supersede_pending(posted_txn) -> dict | None
        Match by pendingTransactionId first, else correlation key.
        Returns the superseded record (or None if no match).
    .drop_pending(source_id, reason, session_id, ts) -> None
        Remove a staged pending and audit-log it as pending-dropped.
    .purge_stale(days, session_id, ts) -> int
        Drop pendings older than *days*; returns count dropped.
    .mark_seen(source_id) -> None
        Record that *source_id* has been imported into the ledger.
    .is_seen(source_id) -> bool
    .bulk_mark_seen(source_ids) -> None
"""

import json
import os
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from .normalize import normalize_description

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write *data* as JSON to *path* atomically via a sibling .tmp file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _correlation_key(txn: dict) -> tuple:
    """Return the secondary correlation key for pending→posted supersession.

    Key components: (accountId, amount, date[:10], normalized description).
    """
    account_id = str(txn.get("accountId") or "")
    amount = str(txn.get("amount") or "")
    raw_date = str(txn.get("date") or "")
    txn_date = raw_date[:10]  # Take YYYY-MM-DD prefix.
    desc = normalize_description(str(txn.get("description") or ""))
    return (account_id, amount, txn_date, desc)


# ---------------------------------------------------------------------------
# StagingStore
# ---------------------------------------------------------------------------


class StagingStore:
    """Manages pending.json and seen-ids.json inside an entity's staging/ dir."""

    _PENDING_FILE = "pending.json"
    _SEEN_IDS_FILE = "seen-ids.json"

    def __init__(self, staging_dir: str | Path, audit_log: Any | None = None) -> None:
        """Initialise the store.

        Parameters
        ----------
        staging_dir
            Path to the entity's staging/ directory.
        audit_log
            Optional audit sink used by legacy/unit-test callers for
            pending-dropped and purge records. Canonical ledger audit events
            live in the ledger store.
        """
        self._dir = Path(staging_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._audit_log = audit_log
        self._pending_path = self._dir / self._PENDING_FILE
        self._seen_path = self._dir / self._SEEN_IDS_FILE

        # Load state into memory.
        self._pending: list[dict] = self._load_pending()
        self._seen: set[str] = self._load_seen()

    # ------------------------------------------------------------------
    # Orphaned .tmp detection
    # ------------------------------------------------------------------

    def check_orphaned_tmps(self) -> list[str]:
        """Return paths of any .tmp siblings present at startup.

        These indicate an interrupted atomic write from a previous session.
        The caller should warn the user; this method is purely diagnostic.
        """
        tmps: list[str] = []
        for fname in (self._PENDING_FILE, self._SEEN_IDS_FILE):
            tmp_path = self._dir / (fname + ".tmp")
            if tmp_path.exists():
                tmps.append(str(tmp_path))
        return tmps

    # ------------------------------------------------------------------
    # Internal load helpers
    # ------------------------------------------------------------------

    def _load_pending(self) -> list[dict]:
        if not self._pending_path.exists():
            return []
        try:
            data = json.loads(self._pending_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
            return []
        except (json.JSONDecodeError, OSError):
            return []

    def _load_seen(self) -> set[str]:
        if not self._seen_path.exists():
            return set()
        try:
            data = json.loads(self._seen_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return set(data)
            return set()
        except (json.JSONDecodeError, OSError):
            return set()

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _save_pending(self) -> None:
        _atomic_write_json(self._pending_path, self._pending)

    def _save_seen(self) -> None:
        _atomic_write_json(self._seen_path, sorted(self._seen))

    # ------------------------------------------------------------------
    # Pending operations
    # ------------------------------------------------------------------

    def add_pending(self, txn: dict) -> None:
        """Stage a pending transaction.

        Idempotent: if a record with the same ``id`` already exists,
        the existing record is left unchanged (no duplicate staging).
        """
        txn_id = txn.get("id")
        if txn_id and any(p.get("id") == txn_id for p in self._pending):
            return  # Already staged.
        self._pending.append(dict(txn))
        self._save_pending()

    def get_pending(self) -> list[dict]:
        """Return a copy of all currently staged pending transactions."""
        return list(self._pending)

    def supersede_pending(self, posted_txn: dict) -> dict | None:
        """Find and remove the staged record corresponding to *posted_txn*.

        Match strategy (KTD order):
        1. ``pendingTransactionId`` field of *posted_txn* matches ``id`` of staged.
        2. Correlation key match when no source-ID match exists.

        Returns the superseded record dict, or None if nothing matched.
        """
        pending_txn_id = posted_txn.get("pendingTransactionId")

        # Pass 1: match by pendingTransactionId.
        if pending_txn_id:
            for idx, staged in enumerate(self._pending):
                if staged.get("id") == pending_txn_id:
                    removed = self._pending.pop(idx)
                    self._save_pending()
                    return removed

        # Pass 2: match by correlation key (only when no source-ID match exists).
        posted_key = _correlation_key(posted_txn)
        for idx, staged in enumerate(self._pending):
            if _correlation_key(staged) == posted_key:
                removed = self._pending.pop(idx)
                self._save_pending()
                return removed

        return None

    def drop_pending(
        self,
        source_id: str,
        reason: str,
        session_id: str,
        ts: str | None = None,
    ) -> bool:
        """Remove a staged pending by its ``id`` and audit-log the drop.

        Returns True when a record was found and removed.
        """
        for idx, staged in enumerate(self._pending):
            if staged.get("id") == source_id:
                self._pending.pop(idx)
                self._save_pending()
                if self._audit_log is not None:
                    self._audit_log.append(
                        "pending-dropped",
                        ts=ts,
                        source_id=source_id,
                        reason=reason,
                        session_id=session_id,
                    )
                return True
        return False

    def purge_stale(
        self,
        days: int,
        session_id: str,
        ts: str | None = None,
        *,
        reference_date: date | None = None,
    ) -> int:
        """Drop pending transactions older than *days* from *reference_date*.

        Parameters
        ----------
        days
            Age threshold in days.
        session_id
            Import session identifier, included in the audit record.
        ts
            Timestamp for the audit record (defaults to now).
        reference_date
            The date to measure age from.  Defaults to today (UTC).

        Returns the number of records purged.
        """
        cutoff = (reference_date or datetime.now(tz=timezone.utc).date()) - timedelta(days=days)
        to_purge: list[dict] = []
        to_keep: list[dict] = []

        for staged in self._pending:
            raw_date = str(staged.get("date") or "")
            txn_date_str = raw_date[:10]
            try:
                txn_date = date.fromisoformat(txn_date_str)
            except (ValueError, TypeError):
                to_keep.append(staged)
                continue
            if txn_date < cutoff:
                to_purge.append(staged)
            else:
                to_keep.append(staged)

        if not to_purge:
            return 0

        self._pending = to_keep
        self._save_pending()

        for staged in to_purge:
            sid = staged.get("id", "unknown")
            if self._audit_log is not None:
                self._audit_log.append(
                    "pending-dropped",
                    ts=ts,
                    source_id=sid,
                    reason=f"purge_stale(days={days})",
                    session_id=session_id,
                )

        return len(to_purge)

    # ------------------------------------------------------------------
    # Seen-IDs (posted dedup)
    # ------------------------------------------------------------------

    def mark_seen(self, source_id: str) -> None:
        """Record that *source_id* has been imported into the ledger."""
        self._seen.add(source_id)
        self._save_seen()

    def bulk_mark_seen(self, source_ids: list[str]) -> None:
        """Mark multiple source IDs as seen in a single write."""
        self._seen.update(source_ids)
        self._save_seen()

    def is_seen(self, source_id: str) -> bool:
        """Return True when *source_id* has already been imported."""
        return source_id in self._seen
