from __future__ import annotations

"""Idempotent transaction importer: store write with atomic-write discipline.

Atomic-write protocol:
  1. Open one SQLite transaction.
  2. Append store ``intent`` event.
  3. Insert entries/postings/source payloads.
  4. Validate the Beancount projection in memory.
  5. Append store ``entry-written`` and ``ledger-store-sealed`` events.
  6. Commit.

Close-start integrity check (``check_integrity``):
  - Verify the store audit-event chain.
  - Detect an impossible/incomplete trailing intent event if the store was
    manually edited outside the writer.

See module docstring of auditlog.py and staging.py for the storage layout.

Public API
----------
import_transactions(entity, normalized_txns, session_id, categorizer, ts)
    Main entry point.  Returns ImportResult.

check_integrity(entity) -> IntegrityResult
    Close-start check.

acknowledge_mismatch(entity, diff_text, session_id, ts)
    Record an owner acknowledgement in the store audit history.

ratify_git_commit(entity, commit_hash, author, diff_sha256, session_id, ts)
    Record a git ratification in the store audit history.

reverse_and_correct(entity, original_source_id, corrected_entry, session_id, ts)
    Write reversing + corrected entries in one atomic ledger write.

Categorizer callable contract:
    categorizer(txn: dict) -> (ledger_account: str, confidence: str)

    ``confidence`` is an opaque string; the importer treats anything that is
    not a falsy value as "categorized".  When the categorizer returns an empty
    account string or raises, the transaction is routed to the
    pending-categorization list (staging/pending-categorization.json).
"""

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from ..entity import Entity, load_entity
from .migrate import migrate_beancount_to_store
from .model import Entry, Open, Posting
from .projections import render_store_ledger
from .staging import StagingStore
from .store import LedgerStore, default_store_path
from .validator import validate
from .normalize import normalize_description

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LATE_ARRIVAL_DAYS = 30
_PENDING_CATEGORIZATION_FILE = "pending-categorization.json"
_LOCK_FILE = ".books.lock"

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ImportResult:
    """Summary of an import_transactions call."""

    session_id: str
    new_entries: int = 0
    new_pending: int = 0
    superseded: int = 0
    skipped_duplicate: int = 0
    pending_categorization: int = 0
    late_arrivals: int = 0
    orphaned_tmps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class IntegrityResult:
    """Result of a close-start integrity check."""

    status: str  # "ok" | "incomplete-write" | "ratifiable" | "halt" | "acknowledged"
    message: str = ""
    diff: str = ""
    commit_hash: str | None = None
    commit_author: str | None = None
    diff_sha256: str | None = None  # For acknowledged / ratifiable paths.


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@contextmanager
def _entity_write_lock(entity_path: Path):
    """Serialize ledger mutations for one entity directory."""
    lock_path = entity_path / _LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as fh:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_date(raw: str) -> date:
    """Parse YYYY-MM-DD prefix from a date string (handles timestamps)."""
    return date.fromisoformat(str(raw)[:10])


def _is_late_arrival(txn_date: date, session_date: date) -> bool:
    return (session_date - txn_date).days > _LATE_ARRIVAL_DAYS


# ---------------------------------------------------------------------------
# Atomic ledger write
# ---------------------------------------------------------------------------


def _atomic_ledger_write(
    entity: Entity,
    new_opens: list[Open],
    new_entries: list[Entry],
    session_id: str,
    ts: str | None,
    intent_description: str,
    source_transactions: list[dict[str, Any]] | None = None,
) -> str:
    with _entity_write_lock(entity.path):
        return _atomic_ledger_write_unlocked(
            entity=entity,
            new_opens=new_opens,
            new_entries=new_entries,
            session_id=session_id,
            ts=ts,
            intent_description=intent_description,
            source_transactions=source_transactions,
        )


def _atomic_ledger_write_unlocked(
    entity: Entity,
    new_opens: list[Open],
    new_entries: list[Entry],
    session_id: str,
    ts: str | None,
    intent_description: str,
    source_transactions: list[dict[str, Any]] | None = None,
) -> str:
    """Execute the store-backed atomic-write protocol and return seal hash.

    Protocol:
      1. Ensure the canonical SQLite store exists.
      2. Insert opens/entries/source payloads in one SQLite transaction.
      3. Append store audit events in the same transaction.
      4. Render and validate the Beancount projection in memory.

    Returns the store seal event hash.
    """
    store = _ensure_ledger_store(entity)
    source_transactions = source_transactions or []
    seal_hash = ""
    with store.transaction() as conn:
        store.append_audit_event(
            "intent",
            {
                "session_id": session_id,
                "description": intent_description,
                "entries": len(new_entries),
            },
            conn,
            ts=ts,
        )
        store.insert_opens(new_opens, conn)
        store.insert_entries(new_entries, conn)
        store.insert_source_transactions(source_transactions, conn, imported_at=ts)
        store.set_meta("canonical", "true", conn)
        store.set_meta("title", entity.entity_config.get("name", "Books"), conn)
        ledger_text = render_store_ledger(store.path, conn=conn)
        errors = validate(ledger_text)
        if errors:
            raise ValueError(
                f"Ledger validation failed after import: "
                + "; ".join(str(e) for e in errors[:5])
            )
        for entry in new_entries:
            store.append_audit_event(
                "entry-written",
                {
                    "session_id": session_id,
                    "source_id": entry.source_id or "",
                },
                conn,
                ts=ts,
            )
        seal_hash = store.append_audit_event(
            "ledger-store-sealed",
            {
                "session_id": session_id,
                "entries": len(new_entries),
                "source_ids": [entry.source_id or "" for entry in new_entries],
            },
            conn,
            ts=ts,
        )

    return seal_hash


def _ensure_ledger_store(entity: Entity) -> LedgerStore:
    """Return an initialized canonical store for *entity*.

    If only ``books.beancount`` exists, migrate it once. If neither surface
    exists yet, create an empty store.
    """
    store_path = default_store_path(entity.path)
    if store_path.exists():
        store = LedgerStore(store_path)
        store.initialize()
        return store

    if entity.books_path.exists():
        result = migrate_beancount_to_store(entity.path, force=True)
        if not result:
            raise RuntimeError(f"Could not initialize ledger store: {result.error_message}")
        return LedgerStore(result.store_path)

    store = LedgerStore(store_path)
    store.initialize()
    store.set_meta("canonical", "true")
    store.set_meta("title", entity.entity_config.get("name", "Books"))
    return store


# ---------------------------------------------------------------------------
# Pending-categorization list
# ---------------------------------------------------------------------------


def _load_pending_categorization(entity: Entity) -> list[dict]:
    path = entity.staging_dir / _PENDING_CATEGORIZATION_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_pending_categorization(entity: Entity, items: list[dict]) -> None:
    path = entity.staging_dir / _PENDING_CATEGORIZATION_FILE
    tmp = path.with_suffix(".json.tmp")
    text = json.dumps(items, indent=2, sort_keys=True) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _add_pending_categorization(entity: Entity, txn: dict) -> bool:
    """Add *txn* to the pending-categorization worklist.

    Returns True if newly added, or False if an item with the same id was
    already queued, so callers can treat a repeat ingest as a duplicate.
    """
    items = _load_pending_categorization(entity)
    txn_id = txn.get("id")
    if txn_id and any(i.get("id") == txn_id for i in items):
        return False  # Already queued.
    items.append(dict(txn))
    _save_pending_categorization(entity, items)
    return True


# ---------------------------------------------------------------------------
# Main import entry point
# ---------------------------------------------------------------------------


def import_transactions(
    entity: Entity,
    normalized_txns: list[dict],
    session_id: str,
    categorizer: Callable[[dict], tuple[str, str]] | None = None,
    ts: str | None = None,
    session_date: date | None = None,
) -> ImportResult:
    """Import a list of normalized transactions into the entity ledger.

    Parameters
    ----------
    entity
        Loaded Entity object (from entity.load_entity).
    normalized_txns
        List of normalized transaction dicts (banksync/csvsource shape).
    session_id
        Caller-supplied import session identifier (for pushtag and audit log).
    categorizer
        callable(txn) -> (account, confidence).  When None, all posted
        transactions are routed to pending-categorization.
    ts
        ISO-8601 timestamp string for audit log records.  Defaults to now.
        Supply a fixed value in tests for deterministic output.
    session_date
        Date to use as the "import date" for late-arrival detection.
        Defaults to today (UTC).

    Returns
    -------
    ImportResult
    """
    if session_date is None:
        session_date = datetime.now(tz=timezone.utc).date()

    staging = StagingStore(entity.staging_dir)
    store = _ensure_ledger_store(entity)

    result = ImportResult(session_id=session_id)

    # Startup check: orphaned .tmp files.
    result.orphaned_tmps = staging.check_orphaned_tmps()

    # Self-heal seen-ids from the ledger: a crash between a ledger write and
    # the seen-ids update would otherwise let the next run import the same
    # source id twice. The ledger's own source-id metadata is authoritative.
    if store.path.exists():
        ledger_sids: list[str] = []
        try:
            ledger_sids = [
                e.source_id
                for e in store.load_entries()
                if e.source_id and "reverses" not in {k for k, _ in e.meta}
            ]
        except Exception:
            ledger_sids = []
        missing = [s for s in ledger_sids if not staging.is_seen(s)]
        if missing:
            staging.bulk_mark_seen(missing)
    elif entity.books_path.exists():
        from .validator import parse_ledger as _parse_ledger
        ledger_text = entity.books_path.read_text(encoding="utf-8")
        if ledger_text.strip():
            ledger_sids = [
                e.source_id
                for e in _parse_ledger(ledger_text)["entries"]
                if e.source_id and "reverses" not in {k for k, _ in e.meta}
            ]
            missing = [s for s in ledger_sids if not staging.is_seen(s)]
            if missing:
                staging.bulk_mark_seen(missing)

    # Accumulate entries to write in this session.
    entries_to_write: list[Entry] = []
    # Track accounts that need open directives.
    needed_opens: set[str] = set()
    source_ids_to_mark: list[str] = []
    source_transactions_to_write: list[dict[str, Any]] = []

    for txn in normalized_txns:
        source_id = str(txn.get("id") or "")
        is_pending = bool(txn.get("pending", False))

        if is_pending:
            # Route to staging; never the ledger.
            staging.add_pending(txn)
            result.new_pending += 1
            continue

        # Posted transaction.

        # Dedup: source ID is the sole dedup key for posted entries.
        if staging.is_seen(source_id) or (source_id and store.source_exists(source_id)):
            result.skipped_duplicate += 1
            continue

        # Supersede any matching staged pending.
        superseded = staging.supersede_pending(txn)
        if superseded:
            result.superseded += 1

        # Categorize.
        account = ""
        if categorizer is not None:
            try:
                account, _confidence = categorizer(txn)
            except Exception:
                account = ""

        if not account:
            # Route to pending-categorization list. A repeat of an already
            # queued id is a duplicate, not a new pending item.
            if _add_pending_categorization(entity, txn):
                result.pending_categorization += 1
            else:
                result.skipped_duplicate += 1
            continue

        # Build entry metadata.
        raw_date = str(txn.get("date") or "")
        try:
            txn_date = _parse_date(raw_date)
        except (ValueError, TypeError):
            result.errors.append(f"Cannot parse date for source_id={source_id!r}: {raw_date!r}")
            continue

        meta: list[tuple[str, str]] = [
            ("source-id", source_id),
            ("import-session", session_id),
        ]

        # Late-arrival flag.
        if _is_late_arrival(txn_date, session_date):
            meta.append(("late-arrival", "true"))
            result.late_arrivals += 1

        narration = str(txn.get("description") or "")

        # Determine amount and posting direction.
        # BankSync: ``amount`` is signed (positive = credit to account, negative = debit).
        # We create a two-posting balanced entry:
        #   bank account posting (uses `amount` as the primary sign)
        #   category account posting (negated)
        raw_amount = txn.get("amount")
        if raw_amount is None:
            # Fall back to creditAmount - debitAmount.
            credit = Decimal(str(txn.get("creditAmount") or "0"))
            debit = Decimal(str(txn.get("debitAmount") or "0"))
            raw_amount_dec = (credit - debit).quantize(Decimal("0.01"))
        else:
            raw_amount_dec = Decimal(str(raw_amount)).quantize(Decimal("0.01"))

        # Bank account posting: entity bank_account_mappings take precedence.
        bank_account = _ledger_account_for_txn(
            txn, entity.entity_config.get("bank_account_mappings")
        )
        needed_opens.add(bank_account)
        needed_opens.add(account)

        bank_posting = Posting(
            account=bank_account,
            amount=raw_amount_dec,
            currency="USD",
        )
        category_posting = Posting(
            account=account,
            amount=-raw_amount_dec,
            currency="USD",
        )

        entry = Entry(
            date=txn_date,
            narration=narration,
            flag="*",
            meta=tuple(meta),
            tags=(f"import-{session_id}",),
            postings=(bank_posting, category_posting),
        )

        entries_to_write.append(entry)
        source_ids_to_mark.append(source_id)
        source_transactions_to_write.append(dict(txn))
        result.new_entries += 1

    # Write to ledger if we have new entries.
    if entries_to_write:
        # Build Open directives for any accounts not already in the ledger.
        existing_opens = _get_existing_opens(entity)
        new_opens = [
            Open(date=date(2000, 1, 1), account=acc)
            for acc in sorted(needed_opens)
            if acc not in existing_opens
        ]

        _atomic_ledger_write(
            entity=entity,
            new_opens=new_opens,
            new_entries=entries_to_write,
            session_id=session_id,
            ts=ts,
            intent_description=f"import {len(entries_to_write)} entries",
            source_transactions=source_transactions_to_write,
        )

        # Mark all source IDs as seen.
        staging.bulk_mark_seen(source_ids_to_mark)

    return result


# ---------------------------------------------------------------------------
# Helpers used by import_transactions
# ---------------------------------------------------------------------------


def _ledger_account_for_txn(txn: dict, mappings: dict[str, str] | None = None) -> str:
    """Derive a beancount-safe account name for the bank side of the posting.

    Entity-config ``bank_account_mappings`` (entity.json) take precedence,
    keyed by BankSync accountId first, then accountName — this is how feed
    accounts line up with the QuickBooks-derived opening-balance accounts.
    Falls back based on account type when available:
    ``Assets:Bank:<name>`` for asset-like accounts,
    ``Liabilities:CreditCard:<name>`` for credit cards, or
    ``Assets:Uncategorized`` when nothing usable is present.
    """
    if mappings:
        account_id = str(txn.get("accountId") or "")
        if account_id and account_id in mappings:
            return mappings[account_id]
        account_name_key = str(txn.get("accountName") or "")
        if account_name_key and account_name_key in mappings:
            return mappings[account_name_key]
    account_name = txn.get("accountName") or ""
    if account_name:
        # Sanitize: remove non-alphanumeric characters, capitalize segments.
        safe = "".join(c if c.isalnum() else "-" for c in account_name).strip("-")
        # Remove runs of hyphens.
        import re
        safe = re.sub(r"-+", "-", safe).strip("-")
        if safe:
            account_type = str(txn.get("accountType") or "").lower().replace("-", "_")
            if account_type in {"credit", "credit_card", "creditcard", "card"}:
                return f"Liabilities:CreditCard:{safe}"
            return f"Assets:Bank:{safe}"
    return "Assets:Uncategorized"


def _get_existing_opens(entity: Entity) -> set[str]:
    """Return the set of account names that already have open directives."""
    store_path = default_store_path(entity.path)
    if store_path.exists():
        try:
            return {item.account for item in LedgerStore(store_path).load_opens()}
        except Exception:
            pass
    books_path = entity.books_path
    if not books_path.exists():
        return set()
    text = books_path.read_text(encoding="utf-8")
    if not text.strip():
        return set()
    from .validator import parse_ledger
    parsed = parse_ledger(text)
    return {o.account for o in parsed["opens"]}


# ---------------------------------------------------------------------------
# Close-start integrity check
# ---------------------------------------------------------------------------


def check_integrity(entity: Entity) -> IntegrityResult:
    """Close-start integrity check for the canonical store.

    Store writes are committed in one SQLite transaction. A valid chain whose
    last event is not an ``intent`` is considered usable.

    Returns an IntegrityResult with one of these statuses:
      ``ok``               Store audit chain verifies.
      ``incomplete-write`` Last event is a stray intent.
      ``halt``             Store audit chain is broken or unreadable.
    """
    store_path = default_store_path(entity.path)
    if not store_path.exists():
        return IntegrityResult(status="ok", message="No ledger store yet.")
    store = LedgerStore(store_path)
    try:
        chain_errors = store.verify_audit_chain()
        if chain_errors:
            return IntegrityResult(
                status="halt",
                message=(
                    "Store audit chain is broken — the ledger store was edited "
                    f"or corrupted: {chain_errors[0]}"
                ),
            )
        events = store.load_audit_events()
    except Exception as exc:
        return IntegrityResult(status="halt", message=f"Could not verify ledger store: {exc}")

    if events and events[-1].get("type") == "intent":
        return IntegrityResult(
            status="incomplete-write",
            message="Last store audit event is an unsealed write intent.",
        )
    return IntegrityResult(status="ok", message="Ledger store integrity confirmed.")


# ---------------------------------------------------------------------------
# Acknowledgement and ratification
# ---------------------------------------------------------------------------


def acknowledge_mismatch(
    entity: Entity,
    diff_text: str,
    session_id: str,
    ts: str | None = None,
) -> None:
    """Record that the owner acknowledged a ledger-store issue."""
    diff_sha256 = _sha256_text(diff_text)
    store = _ensure_ledger_store(entity)
    with store.transaction() as conn:
        store.append_audit_event(
            "acknowledged",
            {"session_id": session_id, "diff_sha256": diff_sha256},
            conn,
            ts=ts,
        )


def ratify_git_commit(
    entity: Entity,
    commit_hash: str,
    author: str,
    diff_sha256: str,
    session_id: str,
    ts: str | None = None,
) -> None:
    """Record a ratification event in the store audit history."""
    store = _ensure_ledger_store(entity)
    with store.transaction() as conn:
        store.append_audit_event(
            "ratified",
            {
                "session_id": session_id,
                "commit_hash": commit_hash,
                "author": author,
                "diff_sha256": diff_sha256,
            },
            conn,
            ts=ts,
        )


# ---------------------------------------------------------------------------
# reverse_and_correct
# ---------------------------------------------------------------------------


def reverse_and_correct(
    entity: Entity,
    original_source_id: str,
    corrected_entry: Entry,
    session_id: str,
    ts: str | None = None,
) -> tuple[Entry, Entry]:
    """Write a reversing entry and a corrected entry in one atomic ledger write.

    The reversing entry has:
      - All posting amounts negated.
      - Metadata ``reverses: "<original_source_id>"``.
      - Flag ``*``.

    The corrected entry has:
      - Metadata ``correction-of: "<original_source_id>"``.

    Both entries are written in a single atomic ledger write.

    Returns (reversing_entry, corrected_entry_with_meta).
    """
    staging = StagingStore(entity.staging_dir)

    store = _ensure_ledger_store(entity)

    # Find the live entry for this source id: skip reversal entries (they
    # carry a `reverses` key) and prefer the most recent correction so a
    # second correction targets the corrected entry, never re-reverses a
    # reversal.
    original: Entry | None = None
    for e in store.load_entries():
        if e.source_id != original_source_id:
            continue
        meta_keys = {k for k, _ in e.meta}
        if "reverses" in meta_keys:
            continue
        original = e  # keep scanning — last non-reversal match wins

    if original is None:
        raise ValueError(f"No entry with source-id={original_source_id!r} found in ledger")

    # Build reversing entry (negate all posting amounts). The reversal's
    # metadata drops the original `source-id` so source-id lookups never
    # resolve to a reversal.
    rev_postings = tuple(
        Posting(
            account=p.account,
            amount=-p.amount,
            currency=p.currency,
        )
        for p in original.postings
    )
    rev_meta = tuple(
        (k, v) for k, v in original.meta if k not in ("source-id", "import-session")
    ) + (
        ("reverses", original_source_id),
        ("import-session", session_id),
    )
    reversing_entry = Entry(
        date=original.date,
        narration=f"Reversal of: {original.narration}",
        flag="*",
        meta=rev_meta,
        postings=rev_postings,
    )

    # Build corrected entry: add correction-of metadata.
    existing_meta = dict(corrected_entry.meta)
    existing_meta["correction-of"] = original_source_id
    existing_meta["import-session"] = session_id
    corrected_with_meta = Entry(
        date=corrected_entry.date,
        narration=corrected_entry.narration,
        payee=corrected_entry.payee,
        flag=corrected_entry.flag,
        meta=tuple(sorted(existing_meta.items())),
        postings=corrected_entry.postings,
    )

    # Find accounts needed.
    needed_accounts = set()
    for e in (reversing_entry, corrected_with_meta):
        for p in e.postings:
            needed_accounts.add(p.account)

    existing_opens = _get_existing_opens(entity)
    new_opens = [
        Open(date=date(2000, 1, 1), account=acc)
        for acc in sorted(needed_accounts)
        if acc not in existing_opens
    ]

    _atomic_ledger_write(
        entity=entity,
        new_opens=new_opens,
        new_entries=[reversing_entry, corrected_with_meta],
        session_id=session_id,
        ts=ts,
        intent_description=(
            f"reverse_and_correct original_source_id={original_source_id!r}"
        ),
    )

    return reversing_entry, corrected_with_meta
