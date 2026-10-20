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
import re
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from ..accrual.store import SupportingMutation

from ..entity import Entity, bank_mapping_account, load_entity
from .migrate import migrate_beancount_to_store
from .model import Entry, Open, Posting
from .staging import StagingStore
from .store import LedgerStore, default_store_path
from .normalize import normalize_description
from ..source_ids import source_id_variants

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LATE_ARRIVAL_DAYS = 30
_PENDING_CATEGORIZATION_FILE = "pending-categorization.json"
_DUPLICATE_CANDIDATES_FILE = "duplicate-candidates.json"
_SOURCE_ID_ALIASES_FILE = "source-id-aliases.json"
_LOCK_FILE = ".books.lock"
_held_entity_locks: ContextVar[tuple[Path, ...]] = ContextVar(
    "books_held_entity_locks", default=()
)

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
    duplicate_candidates: int = 0
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
    """Serialize all entity mutations, allowing nested writers in one context."""
    resolved = entity_path.resolve()
    held = _held_entity_locks.get()
    if resolved in held:
        yield
        return
    lock_path = resolved / _LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as fh:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        token = _held_entity_locks.set((*held, resolved))
        try:
            yield
        finally:
            _held_entity_locks.reset(token)
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
    finalization_receipt: dict[str, Any] | None = None,
    supporting_mutations: Sequence[SupportingMutation] = (),
    expected_digest: str | None = None,
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
            finalization_receipt=finalization_receipt,
            supporting_mutations=supporting_mutations,
            expected_digest=expected_digest,
        )


def _atomic_ledger_write_unlocked(
    entity: Entity,
    new_opens: list[Open],
    new_entries: list[Entry],
    session_id: str,
    ts: str | None,
    intent_description: str,
    source_transactions: list[dict[str, Any]] | None = None,
    finalization_receipt: dict[str, Any] | None = None,
    supporting_mutations: Sequence[SupportingMutation] = (),
    expected_digest: str | None = None,
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
    from ..accrual.scope import DISCIPLINE_ALL, discipline, require_write_scope
    from ..entity import resolve_basis
    resolve_basis(entity.entity_config, authoritative_basis=store.get_meta("basis"))
    current_entity = load_entity(entity.path) if (entity.path / "entity.json").exists() else entity
    resolve_basis(entity.entity_config, authoritative_basis=resolve_basis(current_entity.entity_config))
    source_transactions = source_transactions or []
    seal_hash = ""
    if supporting_mutations:
        require_write_scope(entity)
        require_write_scope(current_entity)
    if discipline(entity.entity_config) == DISCIPLINE_ALL and new_entries:
        from ..accrual.journals import _check_dates
        require_write_scope(current_entity)
        _check_dates(current_entity, store, [entry.date.isoformat() for entry in new_entries], "ordinary_write")
    with store.transaction(immediate=bool(supporting_mutations) or expected_digest is not None) as conn:
        if supporting_mutations or expected_digest is not None:
            integrity = check_store_integrity(store)
            if integrity.status != "ok":
                raise ValueError(f"Cannot publish accrual state: {integrity.message}")
        if expected_digest is not None and store.content_digest(conn) != expected_digest:
            raise ValueError("Stale accounting preview: canonical ledger state changed.")
        from ..preparation_runtime import record_preparation
        record_preparation(entity, store, conn, new_entries, source_transactions, ts)
        if finalization_receipt is not None:
            store.append_audit_event(
                "queue-finalization-intent", finalization_receipt, conn, ts=ts
            )
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
        if supporting_mutations:
            from ..accrual.store import apply_mutations
            apply_mutations(store, supporting_mutations, conn)
            store.append_audit_event("supporting-records-written", {
                "session_id": session_id,
                "records": [{"table": m.table, "id": m.record_id,
                             "revision": m.expected_revision + 1} for m in supporting_mutations],
            }, conn, ts=ts)
        store.set_meta("canonical", "true", conn)
        store.set_meta("title", entity.entity_config.get("name", "Books"), conn)
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
                "store_sha256": store.content_digest(conn),
            },
            conn,
            ts=ts,
        )

    return seal_hash


def _ensure_ledger_store(entity: Entity) -> LedgerStore:
    """Return an initialized canonical store for *entity*.

    If a Beancount source file exists before the store has been initialized,
    import it once. If neither surface exists yet, create an empty store.
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
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"Cannot read pending review state at {path}; repair it before ingesting.") from exc
    if not isinstance(data, list) or any(not isinstance(item, dict) or not item.get("id")
                                         for item in data):
        raise ValueError(f"Invalid pending review state at {path}; expected transactions with source IDs.")
    return data


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
    with _entity_write_lock(entity.path):
        items = _load_pending_categorization(entity)
        txn_id = txn.get("id")
        if txn_id and any(i.get("id") in source_id_variants(txn_id) for i in items):
            return False  # Already queued, under either spelling of a BankSync ID.
        items.append(dict(txn))
        _save_pending_categorization(entity, items)
        return True


# ---------------------------------------------------------------------------
# Main import entry point
# ---------------------------------------------------------------------------


def _validate_normalized_transactions(txns: list[dict]) -> None:
    """Reject unsupported ledger authority before any part of a batch changes state."""
    reserved = {"correction-of", "reverses"}
    for index, txn in enumerate(txns, start=1):
        fields = reserved.intersection(txn)
        metadata = txn.get("metadata")
        if isinstance(metadata, Mapping):
            fields.update(reserved.intersection(metadata))
        if fields:
            raise ValueError(
                f"Transaction {index} contains reserved ledger directives: {', '.join(sorted(fields))}. "
                "Use 'books ledger correct' for an atomic correction; if the original is already "
                "reversed, stop and request accountant review."
            )
        if "account_id" in txn and "accountId" not in txn:
            raise ValueError(
                f"Transaction {index} uses unsupported 'account_id'; use the documented 'accountId' field."
            )


def import_transactions(
    entity: Entity,
    normalized_txns: list[dict],
    session_id: str,
    categorizer: Callable[[dict], tuple[str, str]] | None = None,
    ts: str | None = None,
    session_date: date | None = None,
    legacy_duplicate_guard: bool = False,
    intake_only: bool = False,
    cutover_hold_ids: frozenset[str] = frozenset(),
) -> ImportResult:
    """Import transactions while serializing staging, queue, and ledger writes.

    ``cutover_hold_ids`` names rows dated on or before their account's cutover
    date. They are never posted or queued, with or without the guard: each
    goes to the duplicate-review hold, so a late row the old source never sent is
    seen by a person instead of being dropped.
    """
    _validate_normalized_transactions(normalized_txns)
    with _entity_write_lock(entity.path):
        result = _import_transactions_unlocked(
            entity, normalized_txns, session_id, categorizer, ts, session_date,
            legacy_duplicate_guard, intake_only, cutover_hold_ids,
        )
    from ..work import observe_import
    observe_import(result.skipped_duplicate)
    return result


def _import_transactions_unlocked(
    entity: Entity,
    normalized_txns: list[dict],
    session_id: str,
    categorizer: Callable[[dict], tuple[str, str]] | None = None,
    ts: str | None = None,
    session_date: date | None = None,
    legacy_duplicate_guard: bool = False,
    intake_only: bool = False,
    cutover_hold_ids: frozenset[str] = frozenset(),
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
    _validate_normalized_transactions(normalized_txns)
    from ..accrual.treatment import treatment_checker
    from ..queue import txn_is_above_ask_first
    needs_treatment = treatment_checker(entity)

    def holds_for_amount(row: dict[str, Any]) -> bool:
        return txn_is_above_ask_first(entity, row)

    # Trusted hosted runners force this at the shared boundary so every
    # connector, including legacy paths, keeps approval separate from intake.
    if intake_only or os.environ.get("SLASHBOOKS_INTAKE_ONLY") == "1":
        categorizer = None
    # Once queued, a source belongs to the review workflow. A newly learned
    # category must not approve it implicitly on reingest or bypass its proposal.
    queued_source_ids = {
        str(txn["id"]) for txn in _load_pending_categorization(entity)
    }
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
                ref.source_id
                for ref in store.load_source_refs()
                if "reverses" not in {k for k, _ in ref.metadata}
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

    # Connected-feed rows are checked against posted entries and review items
    # with the same plan a statement import uses (see ``_plan_feed_holds``).
    # A row dated on or before a cutover date is held even when the guard is off (a
    # generic ingest), so no learned rule can post it. Without the guard only those rows
    # are planned; every other row follows the unguarded path.
    guarded = bool(legacy_duplicate_guard or cutover_hold_ids)
    feed_holds = (_plan_feed_holds(
        entity, normalized_txns if legacy_duplicate_guard else
        [t for t in normalized_txns if str(t.get("id") or "") in cutover_hold_ids], cutover_hold_ids)
        if guarded else {})
    # A row that is already held for review stays held on every later sync, under
    # either spelling of its ID, until a person resolves it.
    open_hold_ids = ({str(c.get("source_id") or "") for c in _load_json_list(
        entity.staging_dir / _DUPLICATE_CANDIDATES_FILE) if c.get("status") == "duplicate-candidate"}
        if guarded else set())

    # Accumulate entries to write in this session.
    entries_to_write: list[Entry] = []
    # Track accounts that need open directives.
    needed_opens: set[str] = set()
    source_ids_to_mark: list[str] = []
    source_ids_in_batch: set[str] = set()
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

        # Source IDs deduplicate both posted entries and existing review work.
        # A BankSync row has two spellings (``abc`` and ``banksync:abc``); every
        # lookup tries both.
        spellings = source_id_variants(source_id)
        if (
            any(staging.is_seen(s) for s in spellings)
            or (source_id and spellings & source_ids_in_batch)
            or (source_id and any(store.source_exists(s) for s in spellings))
            or (source_id and spellings & queued_source_ids)
        ):
            result.skipped_duplicate += 1
            continue
        if source_id:
            source_ids_in_batch.add(source_id)

        # A provider can replace historical IDs after a recovery.  This guard
        # deliberately does not auto-skip a plausible match: two legitimate
        # same-day purchases can look identical.  Hold it for review instead.
        bank_account = _ledger_account_for_txn(
            txn, entity.entity_config.get("bank_account_mappings"),
            entity.entity_config.get("csv_account_mappings"),
        )
        if legacy_duplicate_guard or source_id in cutover_hold_ids:
            if spellings & open_hold_ids:
                result.skipped_duplicate += 1
                continue
            alias_status = _legacy_alias_status(entity, source_id)
            if alias_status == "confirmed-duplicate":
                result.skipped_duplicate += 1
                continue
            if alias_status != "confirmed-distinct":
                candidate_state = _record_feed_hold(entity, feed_holds.get(source_id))
                if candidate_state:
                    if candidate_state == "new":
                        result.duplicate_candidates += 1
                    else:
                        result.skipped_duplicate += 1
                    continue

        # Supersede any matching staged pending.
        superseded = staging.supersede_pending(txn)
        if superseded:
            result.superseded += 1

        # Categorize.
        if needs_treatment(txn):
            # A learned category describes a counterparty, not whether this
            # payment settles an existing obligation or funds future service.
            held = dict(txn)
            held["accrual_review_required"] = True
            held["accrual_review_action"] = "Link a settlement/schedule, or explicitly confirm new_cash_event."
            if _add_pending_categorization(entity, held):
                result.pending_categorization += 1
            else:
                result.skipped_duplicate += 1
            continue
        account = ""
        if categorizer is not None:
            try:
                account, _confidence = categorizer(txn)
            except Exception:
                account = ""

        if not account:
            # Route to pending-categorization list. A repeat of an already
            # queued id is a duplicate, not a new pending item.
            held = txn
            if holds_for_amount(txn):
                # Tell Review why this row waits: its amount is above the ask-first limit.
                held = dict(txn)
                held["review_hold_reason"] = "above_ask_first_amount"
            if _add_pending_categorization(entity, held):
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


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_json_list(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(items, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# The match rule name that earlier releases wrote for a same-date, same-amount,
# same-description hold. It stays so existing review items read the same.
_SAME_DAY_RULE = "legacy_id_same_account_date_amount_description"


def _plan_feed_holds(entity: Entity, txns: list[dict[str, Any]],
                     cutover_hold_ids: frozenset[str] = frozenset()) -> dict[str, dict[str, Any]]:
    """Rows of a connected-feed batch that may repeat activity already recorded.

    This is the statement-import duplicate plan (``plan_duplicates``): a row is
    held when a posted entry or a queued review item on the same ledger account
    has the same date, amount and description, or the same amount within three
    days from a different source. A held row is never posted; it goes to the
    "To check" list for a person to decide. Rows on the same feed are not held
    against each other by the near-date rule, so two real purchases of one
    amount are not mistaken for one.
    """
    from ..source_ids import source_family
    from ..statement_import import _txn_amount, plan_duplicates

    mappings = entity.entity_config.get("bank_account_mappings")
    csv_mappings = entity.entity_config.get("csv_account_mappings")
    originals: dict[str, dict[str, Any]] = {}
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for txn in txns:
        source_id = str(txn.get("id") or "")
        if txn.get("pending") or not source_id:
            continue
        try:
            account = _ledger_account_for_txn(txn, mappings, csv_mappings)
            day = _parse_date(str(txn.get("date") or "")).isoformat()
            if txn.get("amount") is None:
                _ = Decimal(str(txn.get("creditAmount") or "0")) - Decimal(str(txn.get("debitAmount") or "0"))
            else:
                _ = Decimal(str(txn["amount"]))
        except (ValueError, TypeError, ArithmeticError):
            continue  # The main loop reports a row it cannot read.
        text = str(txn.get("reference") or txn.get("description") or "")
        originals[source_id] = txn
        groups.setdefault((account, source_family(source_id)), []).append(
            {**txn, "id": source_id, "date": day, "description": text})
    holds: dict[str, dict[str, Any]] = {}
    for (account, family), rows in groups.items():
        plan = plan_duplicates(entity, rows, account, family)
        for row in plan["import"]:
            # Dated on or before the cutover and not matched to anything: a late row the old
            # source may never have sent. A person decides; it is never dropped or posted.
            if row["id"] in cutover_hold_ids and _legacy_alias_status(entity, row["id"]) != "confirmed-distinct":
                holds[row["id"]] = {
                    "source_id": row["id"], "existing_source_id": "", "account": account,
                    "date": row["date"], "amount": format(_txn_amount(row), "f"),
                    "description": str(originals[row["id"]].get("description") or ""),
                    "transaction": dict(originals[row["id"]]), "match_rule": "before_cutover_not_in_books",
                    "status": "duplicate-candidate"}
        for held in plan["held"]:
            source_id = held["source_id"]
            record = {**held, "transaction": dict(originals[source_id]),
                      "description": str(originals[source_id].get("description") or "")}
            if record["match_rule"] == "same_date_amount_description":
                record["match_rule"] = _SAME_DAY_RULE
            holds[source_id] = record
    return holds


def _record_feed_hold(entity: Entity, hold: dict[str, Any] | None) -> str | None:
    """Persist one planned hold. Returns ``new``, ``existing`` or ``None`` (no hold)."""
    if not hold:
        return None
    candidate_path = entity.staging_dir / _DUPLICATE_CANDIDATES_FILE
    existing_candidates = _load_json_list(candidate_path)
    if any(str(item.get("source_id") or "") == hold["source_id"] for item in existing_candidates):
        return "existing"
    existing_candidates.append({**hold, "status": "duplicate-candidate", "created_at": _now_iso()})
    _save_json_list(candidate_path, existing_candidates)
    aliases_path = entity.staging_dir / _SOURCE_ID_ALIASES_FILE
    aliases = _load_json_list(aliases_path)
    aliases.append({
        "source_id": hold["source_id"],
        "canonical_source_id": hold["existing_source_id"],
        "status": "candidate",
    })
    _save_json_list(aliases_path, aliases)
    return "new"


def _legacy_alias_status(entity: Entity, source_id: str) -> str:
    """Return the latest reviewed decision for *source_id*, under either spelling."""
    spellings = source_id_variants(source_id)
    aliases = _load_json_list(entity.staging_dir / _SOURCE_ID_ALIASES_FILE)
    for alias in reversed(aliases):
        if str(alias.get("source_id") or "") in spellings:
            return str(alias.get("status") or "")
    return ""


def _ledger_account_for_txn(
    txn: dict,
    mappings: dict[str, Any] | None = None,
    csv_mappings: dict[str, dict] | None = None,
) -> str:
    """Derive a beancount-safe account name for the bank side of the posting.

    Entity-config ``bank_account_mappings`` (entity.json) take precedence,
    keyed by BankSync accountId first, then accountName — this is how feed
    accounts line up with the QuickBooks-derived opening-balance accounts.
    Falls back based on account type when available:
    ``Assets:Bank:<name>`` for asset-like accounts,
    ``Liabilities:CreditCard:<name>`` for credit cards, or
    ``Assets:Uncategorized`` when nothing usable is present.

    Rows with a CSV mapping key must match their confirmed company mapping.
    Legacy CSV rows use an exact confirmed match when available, otherwise keep
    the existing bank mapping/fallback contract. Conflicting bank mappings or
    changed keyed CSV mappings require review, never display-name fallback.
    """
    account_id = str(txn.get("accountId") or "")
    csv_account = None
    mapping_key = txn.get("csvMappingKey")
    legacy_csv = str(txn.get("id") or "").startswith("csv:") and txn.get("bankId") == "amex-csv"
    if mapping_key is not None or legacy_csv:
        # Source fields identify a mapping; only confirmed company config grants
        # authority to choose a ledger account. Never trust a raw accountId alone.
        if mapping_key is not None:
            mapping = (csv_mappings or {}).get(mapping_key) if isinstance(mapping_key, str) else None
            candidates = [mapping] if isinstance(mapping, dict) else []
        else:
            candidates = list((csv_mappings or {}).values())
        for candidate in candidates:
            if (isinstance(candidate, dict) and candidate.get("confirmed") is True
                    and not candidate.get("proposed") and account_id
                    and candidate.get("ledger_account") == account_id):
                csv_account = candidate["ledger_account"]
                break
        if csv_account is None and mapping_key is not None:
            raise ValueError("CSV account mapping is missing, unconfirmed, or changed; confirm the mapping and parse the CSV again.")

    mapped_account = None
    if mappings:
        # A mapping value is the account name (flat) or an object with a
        # ``ledger_account`` (rich); the key is the provider's account ID.
        if account_id and account_id in mappings:
            mapped_account = bank_mapping_account(mappings[account_id])
        account_name_key = str(txn.get("accountName") or "")
        if mapped_account is None and account_name_key and account_name_key in mappings:
            mapped_account = bank_mapping_account(mappings[account_name_key])
    if csv_account is not None:
        if mapped_account is not None and mapped_account != csv_account:
            raise ValueError("Confirmed CSV and bank account mappings conflict; resolve the mappings before posting.")
        return csv_account
    if mapped_account is not None:
        return mapped_account
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
    """Check canonical store integrity, accepting a not-yet-created store."""
    store_path = default_store_path(entity.path)
    if not store_path.exists():
        return IntegrityResult(status="ok", message="No ledger store yet.")
    return check_store_integrity(LedgerStore(store_path))


def check_store_integrity(store: LedgerStore) -> IntegrityResult:
    """Verify audit history and the latest seal using the supplied store.

    The caller controls connection policy, including read-only access. This
    function does not initialize the store. Empty audit history remains accepted;
    callers requiring history for populated stores must enforce that separately.

    Returns ``ok`` for a matching seal without pending writes,
    ``incomplete-write`` for a missing seal or subsequent write events, and
    ``halt`` for a broken chain, unreadable history, or invalid seal digest.
    """
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

    if not events:
        return IntegrityResult(status="ok", message="Ledger store integrity confirmed.")

    last_seal_idx = -1
    last_seal: dict[str, Any] | None = None
    for idx, event in enumerate(events):
        if event.get("type") == "ledger-store-sealed":
            last_seal_idx = idx
            last_seal = event

    if last_seal is None:
        return IntegrityResult(
            status="incomplete-write",
            message="Store audit history has ledger events but no sealed write.",
        )
    trailing_types = {str(event.get("type") or "") for event in events[last_seal_idx + 1:]}
    if trailing_types.intersection({"intent", "entry-written"}):
        return IntegrityResult(
            status="incomplete-write",
            message="Store audit history has ledger write events after the last seal.",
        )
    payload = last_seal.get("payload")
    if not isinstance(payload, dict):
        return IntegrityResult(
            status="halt",
            message="Latest store seal has an invalid payload.",
        )
    if "store_sha256" not in payload:
        return IntegrityResult(
            status="halt",
            message="Latest store seal does not include a ledger content digest.",
        )
    expected_digest = payload["store_sha256"]
    if not isinstance(expected_digest, str) or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
        return IntegrityResult(
            status="halt",
            message="Latest store seal has an invalid ledger content digest.",
        )
    current_digest = store.content_digest()
    if current_digest != expected_digest:
        return IntegrityResult(
            status="halt",
            message="Ledger store content does not match the latest sealed digest.",
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
    from .corrections import _require_unmanaged_entry
    _require_unmanaged_entry(entity, original_source_id)
    staging = StagingStore(entity.staging_dir)

    store = _ensure_ledger_store(entity)

    original = _find_live_entry(store, original_source_id)

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


def _find_live_entry(store: LedgerStore, source_id: str) -> Entry:
    """Find the current non-reversal entry for a source ID."""
    entries = store.load_entries()
    # A BankSync row has two spellings (``abc`` and ``banksync:abc``); either finds it.
    spellings = source_id_variants(source_id)
    if any(dict(entry.meta).get("reverses") in spellings for entry in entries):
        raise ValueError(f"Entry with source-id={source_id!r} has already been reversed")
    original: Entry | None = None
    for entry in entries:
        if entry.source_id not in spellings or "reverses" in {key for key, _ in entry.meta}:
            continue
        original = entry
    if original is None:
        raise ValueError(f"No entry with source-id={source_id!r} found in ledger")
    return original


def reverse_entry(
    entity: Entity,
    original_source_id: str,
    session_id: str,
    ts: str | None = None,
) -> Entry:
    """Write one traceable reversing entry without modifying the original."""
    from .corrections import _require_unmanaged_entry
    _require_unmanaged_entry(entity, original_source_id)
    store = _ensure_ledger_store(entity)
    original = _find_live_entry(store, original_source_id)
    reversing_entry = Entry(
        date=original.date,
        narration=f"Reversal of: {original.narration}",
        flag="*",
        meta=tuple((k, v) for k, v in original.meta if k not in ("source-id", "import-session")) + (
            ("reverses", original_source_id),
            ("import-session", session_id),
        ),
        postings=tuple(
            Posting(account=p.account, amount=-p.amount, currency=p.currency)
            for p in original.postings
        ),
    )
    _atomic_ledger_write(
        entity=entity,
        new_opens=[],
        new_entries=[reversing_entry],
        session_id=session_id,
        ts=ts,
        intent_description=f"reverse original_source_id={original_source_id!r}",
    )
    return reversing_entry
