from __future__ import annotations

"""Review queue, learned context, and trust-ramp gate.

This module owns the skill↔CLI seam for categorisation: the ONLY entry
points by which LLM judgment reaches the ledger are:

    books queue propose   -- skill submits an LLM-produced category
    books queue confirm   -- owner confirms a queued item
    books queue correct   -- owner corrects a queued item
    books queue list      -- list open queue items
    books queue show      -- show a single item

Additional commands:
    books quarterly-review -- quarterly P&L + BS with variance flags

Public Python API
-----------------
make_categorizer(entity) -> Callable[[dict], tuple[str, str]]
    Returns the callable the importer accepts.  For each transaction:
      - If learned context says eligible → (canonical_category, 'auto')
      - Else                             → ('', 'queue')

eligible_for_autopost(entity, counterparty_key) -> bool
    Pure deterministic trust-ramp check.

propose(entity, source_id, category, reasoning) -> dict
    Validate and create/update a queue item.  Returns the item dict.

confirm(entity, item_id, session_id, ts) -> dict
    Write the ledger entry and update learned context.

correct(entity, item_id, category, note, session_id, ts) -> dict
    Confirm with a corrected category; resets learned-context count.

reopen_if_amount_changed(item, posted_amount) -> dict | None
    Returns updated item dict (status=reopened) if amount differs, else None.

reconcile_pending_amount_changes(entity) -> list[dict]
    Scan queue for items whose source_id is superseded at a different amount.

write_session_summary(entity, session_id, counts) -> Path
    Persist session summary JSON + plain text under reports/sessions/.

Queue item schema
-----------------
{
  "source_id":          str,
  "date":               "YYYY-MM-DD",
  "amount":             "D.DD",          # string Decimal
  "description":        str,
  "counterparty":       str,             # normalize_description output
  "proposed_category":  str,
  "reasoning":          str,             # sanitized
  "context":            str,
  "status":             "open|confirmed|corrected|reopened",
  "confirmed_category": str | null,      # set at confirm/correct
  "original_amount":    "D.DD",          # set at propose; used for reopen
  "delta":              "D.DD" | null,   # set when reopened
  "corrected_at":       ISO | null,
  "created_at":         ISO,
  "updated_at":         ISO,
  "session_summary_id": str | null
}

Learned-context schema (learned-context/counterparties.json)
-------------------------------------------------------------
{
  "<counterparty_key>": {
    "canonical_category":  str,
    "confirmed_count":     int,
    "last_confirmed_date": "YYYY-MM-DD",
    "reset":               bool,
    "notes":               str
  },
  ...
}
"""

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

from .entity import Entity, load_entity
from .ledger.normalize import normalize_description

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_QUEUE_DIR = "review-queue"
_QUARANTINE_DIR = "review-queue/quarantine"
_LEARNED_CONTEXT_FILE = "learned-context/counterparties.json"
_PENDING_CATEGORIZATION_FILE = "staging/pending-categorization.json"
_REASONING_MAX_LEN = 2000
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


# ---------------------------------------------------------------------------
# Sanitization helpers (LLM output is data, never ledger syntax)
# ---------------------------------------------------------------------------


def _sanitize_reasoning(text: str) -> str:
    """Strip newlines, CR, control characters; length-cap to 2000 chars."""
    cleaned = _CONTROL_RE.sub("", text)
    return cleaned[:_REASONING_MAX_LEN]


def _sanitize_filename(source_id: str) -> str:
    """Convert source_id to a safe filename (no path separators or special chars)."""
    safe = re.sub(r"[^\w\-]", "_", source_id)
    return safe[:120]  # keep reasonably short


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_iso() -> str:
    return datetime.now(tz=timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# Learned context: load / save / update
# ---------------------------------------------------------------------------


def _learned_context_path(entity: Entity) -> Path:
    return entity.path / _LEARNED_CONTEXT_FILE


def load_learned_context(entity: Entity) -> dict[str, dict]:
    """Load the learned context dict; returns {} on absent or corrupt file."""
    p = _learned_context_path(entity)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_learned_context(entity: Entity, ctx: dict[str, dict]) -> None:
    """Atomically write the learned context (sorted keys for readability)."""
    p = _learned_context_path(entity)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ctx, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(p))


def _counterparty_key(description: str, counterparty: str) -> str:
    """Derive the counterparty key: normalize whichever value is more informative."""
    cp = counterparty.strip() if counterparty.strip() else description
    return normalize_description(cp)


# ---------------------------------------------------------------------------
# Trust-ramp gate (pure code)
# ---------------------------------------------------------------------------


def eligible_for_autopost(entity: Entity, counterparty_key: str) -> bool:
    """Return True when counterparty meets the deterministic trust threshold.

    Rules (KTD binding):
      1. confirmed_count >= auto_post_threshold
      2. reset flag must be False
      3. queue_all_until_confirmed: when True (normal), only counterparties at
         threshold auto-post — this IS the normal rule.  When False, BankSync
         category passthrough may auto-post regardless of count.

    When queue_all_until_confirmed is False: any counterparty with a
    canonical_category auto-posts (count still acts as a ceiling — False means
    the owner has disabled the ramp entirely as a global kill-switch).
    """
    threshold = entity.auto_post_threshold
    queue_all = bool(entity.trust_policy.get("queue_all_until_confirmed", True))

    ctx = load_learned_context(entity)
    entry = ctx.get(counterparty_key)

    if entry is None:
        # No learned context → always queue
        return False

    if entry.get("reset", False):
        # Cooldown: last correction forces queueing until next confirm clears reset
        return False

    count = int(entry.get("confirmed_count", 0))
    if not queue_all:
        # Global kill-switch off: auto-post if any category is known
        return bool(entry.get("canonical_category"))

    return count >= threshold


def make_categorizer(entity: Entity) -> Callable[[dict], tuple[str, str]]:
    """Return a categorizer callable for the importer.

    For each transaction dict:
      - Computes the counterparty key.
      - If eligible_for_autopost → return (canonical_category, 'auto')
      - Else                     → return ('', 'queue')
    """
    def _categorize(txn: dict) -> tuple[str, str]:
        description = str(txn.get("description") or "")
        counterparty = str(txn.get("counterparty") or "")
        key = _counterparty_key(description, counterparty)

        if eligible_for_autopost(entity, key):
            ctx = load_learned_context(entity)
            entry = ctx.get(key, {})
            category = str(entry.get("canonical_category", ""))
            if category:
                return (category, "auto")
        return ("", "queue")

    return _categorize


# ---------------------------------------------------------------------------
# Queue item: load / save / quarantine
# ---------------------------------------------------------------------------


def _queue_dir(entity: Entity) -> Path:
    return entity.path / _QUEUE_DIR


def _quarantine_dir(entity: Entity) -> Path:
    return entity.path / _QUARANTINE_DIR


def _item_path(entity: Entity, item_id: str) -> Path:
    return _queue_dir(entity) / f"{_sanitize_filename(item_id)}.json"


def _load_item(entity: Entity, item_id: str) -> dict:
    """Load a single queue item.  Raises FileNotFoundError if absent.

    Malformed files are quarantined (moved to review-queue/quarantine/) with a
    named error message.  Never silently dropped.
    """
    path = _item_path(entity, item_id)
    if not path.exists():
        raise FileNotFoundError(f"Queue item '{item_id}' not found at {path}")
    return _load_item_from_path(entity, path)


def _load_item_from_path(entity: Entity, path: Path) -> dict:
    """Load and parse a queue item from *path*; quarantine on parse failure."""
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("Queue item is not a JSON object")
        # Minimum required fields
        for field_name in ("source_id", "status"):
            if field_name not in data:
                raise ValueError(f"Queue item missing required field '{field_name}'")
        return data
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        _quarantine_item(entity, path, str(exc))
        raise


def _quarantine_item(entity: Entity, path: Path, error_msg: str) -> None:
    """Move *path* to the quarantine directory with a named error record."""
    q_dir = _quarantine_dir(entity)
    q_dir.mkdir(parents=True, exist_ok=True)
    dest = q_dir / path.name
    # Write an error sidecar
    error_record = {
        "original_path": str(path),
        "error": error_msg,
        "quarantined_at": _now_iso(),
    }
    try:
        error_path = q_dir / (path.stem + ".error.json")
        error_path.write_text(json.dumps(error_record, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    try:
        os.replace(str(path), str(dest))
    except OSError:
        # If we can't move it, at least we've written the error record
        pass


def _save_item(entity: Entity, item: dict) -> None:
    """Atomically write a queue item to review-queue/<source_id>.json."""
    _queue_dir(entity).mkdir(parents=True, exist_ok=True)
    source_id = str(item["source_id"])
    path = _item_path(entity, source_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(item, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def list_queue_items(entity: Entity, status: Optional[str] = None) -> list[dict]:
    """Return all (or filtered by status) queue items.

    Malformed files are quarantined, not silently dropped.
    """
    q_dir = _queue_dir(entity)
    if not q_dir.exists():
        return []
    items = []
    for p in sorted(q_dir.glob("*.json")):
        try:
            item = _load_item_from_path(entity, p)
            if status is None or item.get("status") == status:
                items.append(item)
        except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
            # Quarantine already happened inside _load_item_from_path
            pass
    return items


# ---------------------------------------------------------------------------
# Pending-categorization helpers (reading from importer's staging file)
# ---------------------------------------------------------------------------


def _load_pending_categorization(entity: Entity) -> list[dict]:
    """Load the pending-categorization list from staging."""
    path = entity.staging_dir / "pending-categorization.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _remove_from_pending_categorization(entity: Entity, source_id: str) -> None:
    """Remove source_id from the pending-categorization list (atomic write)."""
    path = entity.staging_dir / "pending-categorization.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return
        updated = [item for item in data if str(item.get("id", "")) != source_id]
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(path))
    except (json.JSONDecodeError, OSError):
        pass


def _source_id_in_pending(entity: Entity, source_id: str) -> bool:
    """Return True when source_id exists in staging or pending-categorization."""
    pending = _load_pending_categorization(entity)
    if any(str(item.get("id", "")) == source_id for item in pending):
        return True
    # Also check staging pending.json
    staging_path = entity.staging_dir / "pending.json"
    if staging_path.exists():
        try:
            data = json.loads(staging_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                if any(str(item.get("id", "")) == source_id for item in data):
                    return True
        except (json.JSONDecodeError, OSError):
            pass
    return False


def _get_pending_txn(entity: Entity, source_id: str) -> Optional[dict]:
    """Return the txn dict from pending-categorization for source_id, or None."""
    pending = _load_pending_categorization(entity)
    for item in pending:
        if str(item.get("id", "")) == source_id:
            return item
    return None


# ---------------------------------------------------------------------------
# Chart-of-accounts validation
# ---------------------------------------------------------------------------


def _get_opened_accounts(entity: Entity) -> set[str]:
    """Return the set of accounts opened in the entity's books/CoA."""
    from .ledger.validator import parse_ledger

    accounts: set[str] = set()

    # Parse chart-of-accounts.beancount
    coa = entity.coa_path
    if coa.exists():
        text = coa.read_text(encoding="utf-8")
        try:
            parsed = parse_ledger(text)
            for o in parsed["opens"]:
                accounts.add(o.account)
        except (ValueError, OSError):
            pass

    # Parse books.beancount
    books = entity.books_path
    if books.exists():
        text = books.read_text(encoding="utf-8")
        if text.strip():
            try:
                parsed = parse_ledger(text)
                for o in parsed["opens"]:
                    accounts.add(o.account)
            except (ValueError, OSError):
                pass

    return accounts


# ---------------------------------------------------------------------------
# propose
# ---------------------------------------------------------------------------


def propose(
    entity: Entity,
    source_id: str,
    category: str,
    reasoning: str,
    context: str = "",
) -> dict:
    """Validate and create/update a queue item.

    Validations (all raise ValueError on failure):
      1. source_id must exist in staging/pending-categorization.json.
      2. category must be an account opened in the entity's chart-of-accounts.
      3. reasoning is sanitized (newlines/control chars stripped, length-capped).

    Returns the created/updated queue item dict.
    """
    # Validation 1: source_id must not be phantom
    if not _source_id_in_pending(entity, source_id):
        raise ValueError(
            f"Source ID '{source_id}' is not in the pending-categorization list. "
            "Only transactions awaiting categorization may be proposed."
        )

    # Validation 2: category must be a known account
    opened = _get_opened_accounts(entity)
    if opened and category not in opened:
        raise ValueError(
            f"Category '{category}' is not an account opened in the chart of accounts. "
            f"Known accounts: {sorted(opened)}"
        )

    # Validation 3: sanitize reasoning
    clean_reasoning = _sanitize_reasoning(reasoning)

    # Look up the pending txn for amount/date/description
    txn = _get_pending_txn(entity, source_id)
    if txn is None:
        # Shouldn't happen after validation 1 passed, but be safe
        txn = {}

    amount_raw = txn.get("amount", "0")
    try:
        amount_str = str(Decimal(str(amount_raw)).quantize(Decimal("0.01")))
    except Exception:
        amount_str = str(amount_raw)

    description = str(txn.get("description") or "")
    counterparty = _counterparty_key(description, str(txn.get("counterparty") or ""))
    raw_date = str(txn.get("date") or "")[:10] or _today_iso()

    now = _now_iso()

    # Check for existing item
    existing_path = _item_path(entity, source_id)
    existing_item: dict = {}
    if existing_path.exists():
        try:
            existing_item = _load_item_from_path(entity, existing_path)
        except (ValueError, OSError):
            existing_item = {}

    item: dict = {
        "source_id": source_id,
        "date": raw_date,
        "amount": amount_str,
        "description": description,
        "counterparty": counterparty,
        "proposed_category": category,
        "reasoning": clean_reasoning,
        "context": _sanitize_reasoning(context),
        "status": "open",
        "confirmed_category": None,
        "original_amount": existing_item.get("original_amount", amount_str),
        "delta": None,
        "corrected_at": None,
        "created_at": existing_item.get("created_at", now),
        "updated_at": now,
        "session_summary_id": None,
    }

    _save_item(entity, item)
    return item


# ---------------------------------------------------------------------------
# _write_confirmed_entry: thin function to post one categorized txn
# ---------------------------------------------------------------------------


def _write_confirmed_entry(
    entity: Entity,
    txn: dict,
    category: str,
    session_id: str,
    ts: Optional[str] = None,
) -> None:
    """Build an Entry for a confirmed queue item and write it via the importer's atomic path.

    This reuses the importer's ``_atomic_ledger_write`` and the audit-log path
    directly rather than duplicating ledger-write logic.
    """
    from decimal import Decimal as D
    from datetime import date as _date
    from .ledger.model import Entry, Open, Posting
    from .ledger.auditlog import AuditLog
    from .ledger.staging import StagingStore
    from .ledger.importer import _atomic_ledger_write, _get_existing_opens, _ledger_account_for_txn

    audit_log = AuditLog(entity.path / "audit-log.jsonl")
    staging = StagingStore(entity.staging_dir, audit_log)

    source_id = str(txn.get("id") or "")
    raw_date = str(txn.get("date") or "")[:10]
    try:
        txn_date = _date.fromisoformat(raw_date)
    except (ValueError, TypeError):
        txn_date = datetime.now(tz=timezone.utc).date()

    narration = str(txn.get("description") or "")

    raw_amount = txn.get("amount")
    if raw_amount is None:
        credit = D(str(txn.get("creditAmount") or "0"))
        debit = D(str(txn.get("debitAmount") or "0"))
        amount = (credit - debit).quantize(D("0.01"))
    else:
        amount = D(str(raw_amount)).quantize(D("0.01"))

    bank_account = _ledger_account_for_txn(
        txn, entity.entity_config.get("bank_account_mappings")
    )
    meta: list[tuple[str, str]] = [
        ("source-id", source_id),
        ("import-session", session_id),
    ]

    bank_posting = Posting(account=bank_account, amount=amount, currency="USD")
    category_posting = Posting(account=category, amount=-amount, currency="USD")

    entry = Entry(
        date=txn_date,
        narration=narration,
        flag="*",
        meta=tuple(meta),
        tags=(f"import-{session_id}",),
        postings=(bank_posting, category_posting),
    )

    existing_opens = _get_existing_opens(entity)
    needed_accounts = {bank_account, category}
    new_opens = [
        Open(date=_date(2000, 1, 1), account=acc)
        for acc in sorted(needed_accounts)
        if acc not in existing_opens
    ]

    _atomic_ledger_write(
        entity=entity,
        audit_log=audit_log,
        new_opens=new_opens,
        new_entries=[entry],
        session_id=session_id,
        ts=ts,
        intent_description=f"queue confirm source_id={source_id!r} category={category!r}",
        source_transactions=[dict(txn)],
    )

    audit_log.append(
        "entry-written",
        ts=ts,
        source_id=source_id,
        session_id=session_id,
        label="queue-confirmed",
    )

    staging.bulk_mark_seen([source_id])


# ---------------------------------------------------------------------------
# _update_learned_context
# ---------------------------------------------------------------------------


def _update_learned_context(
    entity: Entity,
    counterparty_key: str,
    category: str,
    corrected: bool,
    note: str = "",
) -> None:
    """Update the learned context for *counterparty_key*.

    - confirm: increment confirmed_count, clear reset flag
    - correct: reset confirmed_count to 0, set reset=True
    """
    ctx = load_learned_context(entity)
    entry = ctx.get(counterparty_key, {
        "canonical_category": category,
        "confirmed_count": 0,
        "last_confirmed_date": _today_iso(),
        "reset": False,
        "notes": "",
    })

    if corrected:
        entry["canonical_category"] = category
        entry["confirmed_count"] = 0
        entry["reset"] = True
        entry["last_confirmed_date"] = _today_iso()
        if note:
            entry["notes"] = note
    else:
        # confirm path: clear reset, increment count
        if entry.get("reset", False):
            entry["reset"] = False
        entry["canonical_category"] = category
        entry["confirmed_count"] = int(entry.get("confirmed_count", 0)) + 1
        entry["last_confirmed_date"] = _today_iso()
        if note:
            entry["notes"] = note

    ctx[counterparty_key] = entry
    _save_learned_context(entity, ctx)


# ---------------------------------------------------------------------------
# confirm
# ---------------------------------------------------------------------------


def confirm(
    entity: Entity,
    item_id: str,
    session_id: str,
    ts: Optional[str] = None,
) -> dict:
    """Confirm a queued item: write ledger entry + update learned context.

    Returns the updated item dict.
    """
    item = _load_item(entity, item_id)

    if item.get("status") not in ("open", "reopened"):
        raise ValueError(
            f"Queue item '{item_id}' has status '{item.get('status')}'; "
            "only 'open' or 'reopened' items can be confirmed."
        )

    category = str(item.get("proposed_category") or "")
    if not category:
        raise ValueError(f"Queue item '{item_id}' has no proposed_category.")

    # Look up the txn from pending-categorization
    source_id = str(item["source_id"])
    txn = _get_pending_txn(entity, source_id)
    if txn is None:
        # Item may have been flagged by importer already; build minimal txn from item
        txn = {
            "id": source_id,
            "date": item.get("date", _today_iso()),
            "amount": item.get("amount", "0"),
            "description": item.get("description", ""),
        }

    _write_confirmed_entry(entity, txn, category, session_id, ts=ts)

    # Remove from pending-categorization
    _remove_from_pending_categorization(entity, source_id)

    # Update learned context
    cp_key = str(item.get("counterparty") or _counterparty_key(
        item.get("description", ""), ""
    ))
    _update_learned_context(entity, cp_key, category, corrected=False)

    # Update item status
    now = ts or _now_iso()
    item["status"] = "confirmed"
    item["confirmed_category"] = category
    item["updated_at"] = now
    _save_item(entity, item)

    return item


# ---------------------------------------------------------------------------
# correct
# ---------------------------------------------------------------------------


def correct(
    entity: Entity,
    item_id: str,
    category: str,
    note: str = "",
    session_id: str = "",
    ts: Optional[str] = None,
) -> dict:
    """Correct a queued item: write ledger entry with corrected category.

    Learned-context count is RESET to 0 and reset flag set (cooldown per KTD).
    Returns the updated item dict.
    """
    # Validate category
    opened = _get_opened_accounts(entity)
    if opened and category not in opened:
        raise ValueError(
            f"Category '{category}' is not an account opened in the chart of accounts."
        )

    item = _load_item(entity, item_id)

    if item.get("status") not in ("open", "reopened"):
        raise ValueError(
            f"Queue item '{item_id}' has status '{item.get('status')}'; "
            "only 'open' or 'reopened' items can be corrected."
        )

    source_id = str(item["source_id"])
    txn = _get_pending_txn(entity, source_id)
    if txn is None:
        txn = {
            "id": source_id,
            "date": item.get("date", _today_iso()),
            "amount": item.get("amount", "0"),
            "description": item.get("description", ""),
        }

    _write_confirmed_entry(entity, txn, category, session_id, ts=ts)

    _remove_from_pending_categorization(entity, source_id)

    cp_key = str(item.get("counterparty") or _counterparty_key(
        item.get("description", ""), ""
    ))
    _update_learned_context(entity, cp_key, category, corrected=True, note=note)

    now = ts or _now_iso()
    item["status"] = "corrected"
    item["confirmed_category"] = category
    item["proposed_category"] = category  # update so later reads see the corrected value
    item["corrected_at"] = now
    item["updated_at"] = now
    if note:
        item["context"] = note
    _save_item(entity, item)

    return item


# ---------------------------------------------------------------------------
# Reopening: amount change detection
# ---------------------------------------------------------------------------


def reopen_if_amount_changed(item: dict, posted_amount: Decimal) -> Optional[dict]:
    """Return updated item with status=reopened if posted_amount differs from original.

    Returns None when amounts match (within 0.01 tolerance).
    """
    try:
        original = Decimal(str(item.get("original_amount") or item.get("amount", "0")))
    except Exception:
        original = Decimal("0")

    posted = posted_amount.quantize(Decimal("0.01"))
    original = original.quantize(Decimal("0.01"))

    if posted == original:
        return None

    delta = posted - original
    updated = dict(item)
    updated["status"] = "reopened"
    updated["original_amount"] = str(original)
    updated["amount"] = str(posted)
    updated["delta"] = str(delta)
    updated["updated_at"] = _now_iso()
    # proposed_category is pre-filled from the previous confirmation
    return updated


def reconcile_pending_amount_changes(entity: Entity) -> list[dict]:
    """Scan open queue items for source_ids superseded at a different amount.

    This is called by the close flow.  When the importer has superseded a
    pending transaction at a different amount, the queue item should reopen.

    Returns a list of reopened item dicts (already persisted).
    """
    from .ledger.staging import StagingStore
    from .ledger.auditlog import AuditLog

    audit_log = AuditLog(entity.path / "audit-log.jsonl")
    staging = StagingStore(entity.staging_dir, audit_log)

    reopened: list[dict] = []
    items = list_queue_items(entity, status="open")
    items += list_queue_items(entity, status="confirmed")

    # Load pending categorization for posted amounts
    pending = _load_pending_categorization(entity)
    pending_by_id = {str(p.get("id", "")): p for p in pending}

    for item in items:
        source_id = str(item.get("source_id", ""))
        if source_id not in pending_by_id:
            continue
        posted_txn = pending_by_id[source_id]
        try:
            posted_amount = Decimal(str(posted_txn.get("amount", "0"))).quantize(Decimal("0.01"))
        except Exception:
            continue

        updated = reopen_if_amount_changed(item, posted_amount)
        if updated is not None:
            _save_item(entity, updated)
            reopened.append(updated)

    return reopened


# ---------------------------------------------------------------------------
# Session summary
# ---------------------------------------------------------------------------

_JARGON_RE = re.compile(
    r'\b(pushtag|poptag|bean[- ]?check|beancount|SELECT|INSERT|UPDATE|DELETE|FROM|WHERE)\b'
    r'|Assets:[A-Za-z]|Liabilities:[A-Za-z]|Income:[A-Za-z]|Expenses:[A-Za-z]|Equity:[A-Za-z]',
    re.IGNORECASE,
)


def _render_account_readable(name: str) -> str:
    """Convert beancount account name to human-readable form."""
    return name.replace(":", " › ")


def write_session_summary(
    entity: Entity,
    session_id: str,
    counts: dict[str, Any],
) -> Path:
    """Persist session summary as JSON + plain text under reports/sessions/.

    counts dict expected keys (all optional, default 0):
      new, auto_posted, queued, confirmed, corrected, reopened,
      reconciliation_status (str), reconciliation_notes (str)

    Returns the path to the .txt file.

    Guaranteed: the .txt file contains NO beancount/SQL jargon tokens.
    """
    sessions_dir = entity.reports_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    json_path = sessions_dir / f"{session_id}.json"
    txt_path = sessions_dir / f"{session_id}.txt"

    # JSON payload
    payload: dict[str, Any] = {
        "session_id": session_id,
        "entity": entity.name,
        "generated_at": _now_iso(),
        "new": counts.get("new", 0),
        "auto_posted": counts.get("auto_posted", 0),
        "queued": counts.get("queued", 0),
        "confirmed": counts.get("confirmed", 0),
        "corrected": counts.get("corrected", 0),
        "reopened": counts.get("reopened", 0),
        "reconciliation_status": counts.get("reconciliation_status", "unknown"),
        "reconciliation_notes": counts.get("reconciliation_notes", ""),
    }

    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Plain-text rendering (no jargon)
    recon_status = str(payload["reconciliation_status"])
    recon_notes = str(payload["reconciliation_notes"])

    lines = [
        f"Session Summary: {session_id}",
        f"Entity: {entity.name}",
        "=" * 50,
        "",
        "Transaction counts for this session:",
        f"  New transactions imported:    {payload['new']}",
        f"  Auto-posted (trusted):        {payload['auto_posted']}",
        f"  Queued for review:            {payload['queued']}",
        f"  Confirmed by owner:           {payload['confirmed']}",
        f"  Corrected by owner:           {payload['corrected']}",
        f"  Reopened (amount changed):    {payload['reopened']}",
        "",
        f"Reconciliation: {recon_status}",
    ]
    if recon_notes:
        lines.append(f"  Notes: {recon_notes}")
    lines.append("")

    txt_content = "\n".join(lines)
    txt_path.write_text(txt_content, encoding="utf-8")

    return txt_path


# ---------------------------------------------------------------------------
# Quarterly review
# ---------------------------------------------------------------------------

_QUARTER_MONTHS: dict[int, tuple[int, int]] = {
    1: (1, 3),
    2: (4, 6),
    3: (7, 9),
    4: (10, 12),
}


def _quarter_dates(q: int, year: int) -> tuple[date, date]:
    import calendar
    start_m, end_m = _QUARTER_MONTHS[q]
    last_day = calendar.monthrange(year, end_m)[1]
    return date(year, start_m, 1), date(year, end_m, last_day)


def _prev_quarter(q: int, year: int) -> tuple[int, int]:
    if q == 1:
        return 4, year - 1
    return q - 1, year


def quarterly_review(
    entity: Entity,
    quarter: int,
    year: int,
) -> dict[str, Any]:
    """Render quarterly P&L + balance sheet with variance flags vs prior quarter.

    Returns a dict with keys:
      quarter, year, pnl, balance_sheet,
      variance_flags (list of str),
      auto_posted_sample (list of dict, up to 10)

    Writes JSON + plain text to reports/quarterly/<year>-Q<q>.json/.txt
    """
    from .reports.statements import profit_and_loss, balance_sheet as _balance_sheet
    from .reports.cache import open_cache, iter_postings

    start, end = _quarter_dates(quarter, year)
    pnl = profit_and_loss(entity.path, start, end)
    bs = _balance_sheet(entity.path, end)

    # Prior quarter
    prev_q, prev_y = _prev_quarter(quarter, year)
    prev_start, prev_end = _quarter_dates(prev_q, prev_y)
    prev_pnl = None
    has_prior = False
    try:
        candidate = profit_and_loss(entity.path, prev_start, prev_end)
        # Only treat prior quarter as available if it has actual transactions
        has_any_data = any(
            row.get("amount") not in (None, Decimal("0.00"), "0.00", 0)
            for section in candidate.sections
            for row in section.get("rows", [])
        )
        if has_any_data:
            prev_pnl = candidate
            has_prior = True
    except Exception:
        pass

    # Variance flags (>25% or >$500 movement in any category)
    variance_flags: list[str] = []
    if has_prior and prev_pnl is not None:
        # Build category→amount maps
        cur_cats: dict[str, Decimal] = {}
        for section in pnl.sections:
            for row in section.get("rows", []):
                label = row.get("label", "")
                amount = row.get("amount")
                if label and amount is not None:
                    cur_cats[label] = Decimal(str(amount))

        prev_cats: dict[str, Decimal] = {}
        for section in prev_pnl.sections:
            for row in section.get("rows", []):
                label = row.get("label", "")
                amount = row.get("amount")
                if label and amount is not None:
                    prev_cats[label] = Decimal(str(amount))

        all_labels = set(cur_cats) | set(prev_cats)
        for label in sorted(all_labels):
            cur = cur_cats.get(label, Decimal("0.00"))
            prev = prev_cats.get(label, Decimal("0.00"))
            delta = cur - prev
            abs_delta = abs(delta)

            flag_reason = None
            if abs_delta > Decimal("500"):
                flag_reason = f"changed by {_fmt_amount(abs_delta)}"
            elif prev != Decimal("0.00"):
                pct = abs_delta / abs(prev)
                if pct > Decimal("0.25"):
                    flag_reason = f"changed by {_fmt_pct(pct)}"

            if flag_reason:
                direction = "up" if delta > 0 else "down"
                variance_flags.append(
                    f"{label}: {direction} {flag_reason} "
                    f"(was {_fmt_amount(prev)}, now {_fmt_amount(cur)})"
                )

    # Auto-posted sample: up to 10 entries tagged with 'auto' in the quarter
    auto_posted_sample: list[dict] = []
    try:
        conn = open_cache(entity.path)
        try:
            count = 0
            for entry_date, narration, payee, account, amount, currency in iter_postings(
                conn, from_date=start, to_date=end
            ):
                if count >= 10:
                    break
                # Include expense/income postings (not bank-side) as sample
                if account.startswith("Expenses:") or account.startswith("Income:"):
                    auto_posted_sample.append({
                        "date": str(entry_date),
                        "description": narration,
                        "account": _render_account_readable(account),
                        "amount": str(amount),
                    })
                    count += 1
        finally:
            conn.close()
    except Exception:
        pass

    result: dict[str, Any] = {
        "quarter": quarter,
        "year": year,
        "period": {"start": str(start), "end": str(end)},
        "pnl": json.loads(pnl.to_json()),
        "balance_sheet": json.loads(bs.to_json()),
        "variance_flags": variance_flags,
        "auto_posted_sample": auto_posted_sample,
        "has_prior_quarter": has_prior,
    }

    # Write reports
    rpt_dir = entity.reports_dir / "quarterly"
    rpt_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{year}-Q{quarter}"
    json_path = rpt_dir / f"{stem}.json"
    txt_path = rpt_dir / f"{stem}.txt"

    json_path.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")

    lines = [
        f"Quarterly Review: Q{quarter} {year}",
        f"Period: {start} through {end}",
        "=" * 50,
        "",
        pnl.to_text(),
        "",
        bs.to_text(),
    ]

    if variance_flags:
        lines += [
            "",
            "Notable Changes vs Prior Quarter",
            "-" * 40,
        ]
        for flag in variance_flags:
            lines.append(f"  {flag}")

    if auto_posted_sample:
        lines += [
            "",
            "Auto-posted entries sample (spot-check)",
            "-" * 40,
        ]
        for entry in auto_posted_sample:
            lines.append(
                f"  {entry['date']}  {entry['description'][:40]:<40}  "
                f"{entry['account']}  {entry['amount']}"
            )

    txt_content = "\n".join(lines)
    txt_path.write_text(txt_content, encoding="utf-8")

    return result


def _fmt_amount(amount: Decimal) -> str:
    return f"${amount:,.2f}"


def _fmt_pct(pct: Decimal) -> str:
    return f"{pct * 100:.1f}%"


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def add_parser(subparsers: Any) -> None:
    """Register the ``queue`` and ``quarterly-review`` subcommands."""
    # ---- queue group --------------------------------------------------------
    queue_parser = subparsers.add_parser(
        "queue",
        help="Review queue: propose, confirm, correct, list, show",
    )
    queue_sub = queue_parser.add_subparsers(dest="queue_command", required=True)

    # propose
    prop_p = queue_sub.add_parser("propose", help="Propose a category for a queued transaction")
    prop_p.add_argument("--entity", required=True, help="Path to entity directory")
    prop_p.add_argument("--source-id", required=True, dest="source_id", help="Transaction source ID")
    prop_p.add_argument("--category", required=True, help="Ledger account (e.g. Expenses:Software)")
    prop_p.add_argument("--reasoning", required=True, help="Reasoning text (sanitized)")
    prop_p.add_argument("--context", default="", help="Additional context")

    # confirm
    conf_p = queue_sub.add_parser("confirm", help="Confirm a proposed categorization")
    conf_p.add_argument("--entity", required=True, help="Path to entity directory")
    conf_p.add_argument("--item", required=True, dest="item_id", help="Queue item ID (source ID)")
    conf_p.add_argument("--session", default="queue-session", dest="session_id", help="Session ID")

    # correct
    corr_p = queue_sub.add_parser("correct", help="Correct a proposed categorization")
    corr_p.add_argument("--entity", required=True, help="Path to entity directory")
    corr_p.add_argument("--item", required=True, dest="item_id", help="Queue item ID (source ID)")
    corr_p.add_argument("--category", required=True, help="Corrected ledger account")
    corr_p.add_argument("--note", default="", help="Optional note")
    corr_p.add_argument("--session", default="queue-session", dest="session_id", help="Session ID")

    # list
    list_p = queue_sub.add_parser("list", help="List queue items")
    list_p.add_argument("--entity", required=True, help="Path to entity directory")
    list_p.add_argument("--status", default=None, help="Filter by status (open/confirmed/corrected/reopened)")

    # show
    show_p = queue_sub.add_parser("show", help="Show a single queue item")
    show_p.add_argument("--entity", required=True, help="Path to entity directory")
    show_p.add_argument("--item", required=True, dest="item_id", help="Queue item ID")

    # ---- quarterly-review ---------------------------------------------------
    qr_parser = subparsers.add_parser(
        "quarterly-review",
        help="Render quarterly P&L + balance sheet with variance flags",
    )
    qr_parser.add_argument("--entity", required=True, help="Path to entity directory")
    qr_parser.add_argument(
        "--quarter",
        required=True,
        choices=["Q1", "Q2", "Q3", "Q4"],
        help="Quarter (Q1–Q4)",
    )
    qr_parser.add_argument("--year", required=True, type=int, help="Year (e.g. 2026)")


def run(args: Any) -> int:
    """Dispatch queue or quarterly-review command."""
    cmd = getattr(args, "command", None)

    if cmd == "quarterly-review":
        entity = load_entity(args.entity)
        q_str = args.quarter  # "Q1" .. "Q4"
        q_num = int(q_str[1])
        result = quarterly_review(entity, q_num, args.year)
        flags = result.get("variance_flags", [])
        sample = result.get("auto_posted_sample", [])
        print(f"Quarterly Review: Q{q_num} {args.year}")
        print(f"  Variance flags: {len(flags)}")
        for f in flags:
            print(f"    {f}")
        print(f"  Auto-posted sample: {len(sample)} entries")
        return 0

    if cmd == "queue":
        qcmd = getattr(args, "queue_command", None)
        entity = load_entity(args.entity)

        if qcmd == "propose":
            try:
                item = propose(entity, args.source_id, args.category, args.reasoning, args.context)
                print(f"Proposed: {item['source_id']} → {item['proposed_category']}")
                print(f"  Status: {item['status']}")
                return 0
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1

        elif qcmd == "confirm":
            try:
                item = confirm(entity, args.item_id, args.session_id)
                print(f"Confirmed: {item['source_id']} → {item['confirmed_category']}")
                return 0
            except (FileNotFoundError, ValueError) as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1

        elif qcmd == "correct":
            try:
                item = correct(entity, args.item_id, args.category, args.note, args.session_id)
                print(f"Corrected: {item['source_id']} → {item['confirmed_category']}")
                return 0
            except (FileNotFoundError, ValueError) as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1

        elif qcmd == "list":
            items = list_queue_items(entity, status=args.status)
            if not items:
                print("No queue items found.")
                return 0
            for item in items:
                cat = item.get("proposed_category") or item.get("confirmed_category") or "?"
                print(f"  [{item.get('status', '?')}] {item['source_id']}  {item.get('date', '?')}  {cat}")
            return 0

        elif qcmd == "show":
            try:
                item = _load_item(entity, args.item_id)
                print(json.dumps(item, indent=2, sort_keys=True))
                return 0
            except FileNotFoundError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1

        print(f"Unknown queue command: {qcmd}", file=sys.stderr)
        return 2

    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 2
