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

import hashlib
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
_DUPLICATE_CANDIDATES_FILE = "duplicate-candidates.json"
_SOURCE_ID_ALIASES_FILE = "source-id-aliases.json"
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
    items = []
    if q_dir.exists():
        for p in sorted(q_dir.glob("*.json")):
            try:
                item = _load_item_from_path(entity, p)
                if status is None or item.get("status") == status:
                    items.append(item)
            except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
                # Quarantine already happened inside _load_item_from_path
                pass

    # Pending categorization is actionable review work even before an agent has
    # produced a proposal.  Surface it alongside queue files so `queue list
    # --status open` cannot incorrectly report an empty close.
    if status in (None, "open", "staged"):
        queued_ids = {
            str(source_id)
            for item in items
            for source_id in (item.get("source_ids") or [item.get("source_id")])
            if source_id
        }
        for txn in _load_pending_categorization(entity):
            source_id = str(txn.get("id") or "")
            if not source_id or source_id in queued_ids:
                continue
            items.append({
                "source_id": source_id,
                "date": str(txn.get("date") or "")[:10],
                "amount": str(txn.get("amount") or ""),
                "description": str(txn.get("description") or ""),
                "status": "staged",
                "proposed_category": None,
                "confirmed_category": None,
            })
    if status in (None, "open", "duplicate-candidate"):
        candidate_path = entity.staging_dir / "duplicate-candidates.json"
        try:
            candidates = json.loads(candidate_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            candidates = []
        if isinstance(candidates, list):
            items.extend(
                item for item in candidates
                if isinstance(item, dict) and item.get("status") == "duplicate-candidate"
            )
    return items


def summarize_queue_items(entity: Entity, status: Optional[str] = "open") -> list[dict]:
    """Group review work by proposed treatment without changing any item.

    The queue does not infer a category simply to make a prettier group. Staged
    and guarded items remain in ``Needs review`` until an agent proposes a
    treatment; confirmed proposals stay in distinct groups.
    """
    groups: dict[str, dict[str, Any]] = {}
    for item in list_queue_items(entity, status=status):
        treatment = str(item.get("proposed_category") or "Needs review")
        group = groups.setdefault(
            treatment,
            {
                "treatment": treatment,
                "count": 0,
                "total": Decimal("0.00"),
                "dates": [],
                "sample_counterparties": [],
                "statuses": set(),
            },
        )
        group["count"] += 1
        try:
            group["total"] += Decimal(str(item.get("amount") or "0")).quantize(Decimal("0.01"))
        except Exception:
            pass
        raw_date = str(item.get("date") or "")[:10]
        if raw_date:
            group["dates"].append(raw_date)
        sample = str(item.get("counterparty") or item.get("description") or "").strip()
        if sample and sample not in group["sample_counterparties"] and len(group["sample_counterparties"]) < 3:
            group["sample_counterparties"].append(sample)
        status_name = str(item.get("status") or "")
        if status_name:
            group["statuses"].add(status_name)

    result: list[dict] = []
    for group in groups.values():
        dates = sorted(group.pop("dates"))
        statuses = sorted(group.pop("statuses"))
        group["total"] = f"{group['total']:.2f}"
        group["date_from"] = dates[0] if dates else None
        group["date_to"] = dates[-1] if dates else None
        group["statuses"] = statuses
        result.append(group)
    return sorted(result, key=lambda group: (group["treatment"] != "Needs review", group["treatment"]))


def resolve_duplicate_candidate(
    entity: Entity,
    source_id: str,
    decision: str,
    session_id: str = "duplicate-review",
) -> dict:
    """Resolve a guarded legacy-ID match as duplicate or distinct activity."""
    if decision not in {"duplicate", "distinct"}:
        raise ValueError("Duplicate decision must be 'duplicate' or 'distinct'.")

    candidate_path = entity.staging_dir / _DUPLICATE_CANDIDATES_FILE
    try:
        candidates = json.loads(candidate_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"Could not read duplicate candidates: {exc}") from exc
    if not isinstance(candidates, list):
        raise ValueError("Duplicate candidates file is not a JSON list.")

    candidate = next(
        (
            item for item in candidates
            if isinstance(item, dict)
            and str(item.get("source_id") or "") == source_id
            and item.get("status") == "duplicate-candidate"
        ),
        None,
    )
    if candidate is None:
        raise ValueError(f"No open duplicate candidate found for source ID '{source_id}'.")

    if decision == "distinct":
        txn = candidate.get("transaction")
        if not isinstance(txn, dict):
            raise ValueError(
                "This candidate predates the safe review workflow and lacks its source transaction. "
                "Re-download and ingest the source after updating Slashbooks."
            )
        from .ledger.importer import import_transactions

        result = import_transactions(
            entity,
            [txn],
            session_id,
            categorizer=make_categorizer(entity),
            legacy_duplicate_guard=False,
        )
        if result.errors:
            raise ValueError("Could not release distinct activity: " + "; ".join(result.errors))

    resolved_at = _now_iso()
    candidate["status"] = f"confirmed-{decision}"
    candidate["resolved_at"] = resolved_at
    candidate["resolution_session"] = session_id

    aliases_path = entity.staging_dir / _SOURCE_ID_ALIASES_FILE
    try:
        aliases = json.loads(aliases_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        aliases = []
    if not isinstance(aliases, list):
        aliases = []
    updated_alias = False
    for alias in reversed(aliases):
        if isinstance(alias, dict) and str(alias.get("source_id") or "") == source_id:
            alias["status"] = f"confirmed-{decision}"
            alias["resolved_at"] = resolved_at
            updated_alias = True
            break
    if not updated_alias:
        aliases.append({"source_id": source_id, "status": f"confirmed-{decision}", "resolved_at": resolved_at})
    tmp = aliases_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(aliases, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(aliases_path))

    # Write the import decision first because it controls future deduplication.
    # If the display record write is interrupted, the still-open candidate can
    # be retried safely; the reverse order could leave a hidden, unresolvable
    # candidate alias.
    tmp = candidate_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(candidates, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(candidate_path))
    return candidate


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


def _remove_many_from_pending_categorization(entity: Entity, source_ids: list[str]) -> None:
    """Atomically remove several staged transactions after one confirmed entry."""
    path = entity.staging_dir / "pending-categorization.json"
    if not path.exists():
        return
    wanted = {str(source_id) for source_id in source_ids}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return
        updated = [item for item in data if str(item.get("id", "")) not in wanted]
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


def _txn_amount(txn: dict) -> Decimal:
    """Return a normalized signed amount from a source transaction."""
    raw_amount = txn.get("amount")
    if raw_amount is None:
        credit = Decimal(str(txn.get("creditAmount") or "0"))
        debit = Decimal(str(txn.get("debitAmount") or "0"))
        return (credit - debit).quantize(Decimal("0.01"))
    return Decimal(str(raw_amount)).quantize(Decimal("0.01"))


def _txn_date(txn: dict) -> date:
    """Return the transaction date, falling back to today for malformed input."""
    try:
        return date.fromisoformat(str(txn.get("date") or "")[:10])
    except ValueError:
        return datetime.now(tz=timezone.utc).date()


def _validate_known_accounts(entity: Entity, accounts: list[str]) -> None:
    """Require every selected account to exist in the entity catalog."""
    opened = _get_opened_accounts(entity)
    unknown = sorted({account for account in accounts if opened and account not in opened})
    if unknown:
        raise ValueError(
            "These accounts are not in the account catalog: " + ", ".join(unknown) + "."
        )


def _parse_posting_specs(posting_specs: list[str]) -> list[dict[str, str]]:
    """Parse safe ``Account=SIGNED_AMOUNT`` CLI values for a proposed split."""
    postings: list[dict[str, str]] = []
    for spec in posting_specs:
        if "=" not in spec:
            raise ValueError("Each split posting must use Account=SIGNED_AMOUNT.")
        account, raw_amount = spec.split("=", 1)
        account = account.strip()
        try:
            amount = Decimal(raw_amount.strip()).quantize(Decimal("0.01"))
        except Exception as exc:
            raise ValueError(f"Invalid split amount in '{spec}'.") from exc
        if not account or amount == 0:
            raise ValueError("Split accounts and amounts must be non-empty and non-zero.")
        postings.append({"account": account, "amount": f"{amount:.2f}"})
    if not postings:
        raise ValueError("At least one split posting is required.")
    return postings


def _split_template_path(entity: Entity) -> Path:
    return entity.staging_dir / "split-templates.json"


def _load_split_templates(entity: Entity) -> dict[str, list[dict[str, str]]]:
    path = _split_template_path(entity)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    templates: dict[str, list[dict[str, str]]] = {}
    for name, postings in data.items():
        if isinstance(name, str) and isinstance(postings, list):
            templates[name] = [dict(posting) for posting in postings if isinstance(posting, dict)]
    return templates


# ---------------------------------------------------------------------------
# Account catalog validation
# ---------------------------------------------------------------------------


def _get_opened_accounts(entity: Entity) -> set[str]:
    """Return accounts from the canonical SQLite account catalog."""
    from .ledger.validator import parse_ledger
    from .ledger.store import LedgerStore, default_store_path

    store_path = default_store_path(entity.path)
    if store_path.exists():
        try:
            accounts = LedgerStore(store_path).load_account_names()
            if accounts:
                return accounts
        except Exception:
            pass

    # Legacy fallback: parse an explicit Beancount snapshot if one exists.
    books = entity.books_path
    if books.exists():
        text = books.read_text(encoding="utf-8")
        if text.strip():
            try:
                return {o.account for o in parse_ledger(text)["opens"]}
            except (ValueError, OSError):
                pass

    return set()


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
      2. category must be an account in the entity's SQLite account catalog.
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
            f"Category '{category}' is not an account in the account catalog. "
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


def propose_group(
    entity: Entity,
    source_ids: list[str],
    category: str,
    reasoning: str,
    context: str = "",
) -> list[dict]:
    """Create same-category proposals for a reviewed group of staged items."""
    if not source_ids:
        raise ValueError("At least one source ID is required for a group proposal.")
    return [propose(entity, source_id, category, reasoning, context) for source_id in source_ids]


# ---------------------------------------------------------------------------
# Review-time split transactions
# ---------------------------------------------------------------------------

def save_split_template(entity: Entity, name: str, posting_specs: list[str]) -> dict:
    """Save an exact, reusable split allocation after validating its accounts."""
    template_name = name.strip()
    if not template_name:
        raise ValueError("A split template name is required.")
    postings = _parse_posting_specs(posting_specs)
    _validate_known_accounts(entity, [posting["account"] for posting in postings])
    templates = _load_split_templates(entity)
    status = "updated" if template_name in templates else "created"
    templates[template_name] = postings
    path = _split_template_path(entity)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(templates, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))
    return {"name": template_name, "status": status, "postings": postings}


def list_split_templates(entity: Entity) -> dict[str, list[dict[str, str]]]:
    """Return exact reusable split allocations without changing any records."""
    return _load_split_templates(entity)


def _split_liability_effect(postings: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {"account": posting["account"], "amount": posting["amount"]}
        for posting in postings
        if posting["account"].startswith("Liabilities:")
    ]


def propose_split(
    entity: Entity,
    source_id: str,
    reasoning: str,
    posting_specs: list[str] | None = None,
    template: str = "",
) -> dict:
    """Prepare one balanced multi-line allocation from a staged transaction.

    The supplied postings are the non-cash legs.  Their signed sum must be the
    inverse of the source transaction's cash leg.  This is intentionally exact
    rather than percentage-based: payroll and tax allocations remain visible to
    the owner before approval.
    """
    if not _source_id_in_pending(entity, source_id):
        raise ValueError("Only staged transactions may receive a split proposal.")
    txn = _get_pending_txn(entity, source_id)
    if txn is None:
        raise ValueError(f"Could not load staged transaction '{source_id}'.")
    if template:
        templates = _load_split_templates(entity)
        try:
            postings = [dict(posting) for posting in templates[template]]
        except KeyError as exc:
            raise ValueError(f"Split template '{template}' was not found.") from exc
    else:
        postings = _parse_posting_specs(posting_specs or [])
    _validate_known_accounts(entity, [posting["account"] for posting in postings])
    allocation_total = sum((Decimal(posting["amount"]) for posting in postings), Decimal("0.00"))
    amount = _txn_amount(txn)
    if allocation_total != -amount:
        raise ValueError(
            "Split allocations must total the inverse of the source amount "
            f"({-amount:.2f}); received {allocation_total:.2f}."
        )

    now = _now_iso()
    existing_path = _item_path(entity, source_id)
    existing_item: dict = {}
    if existing_path.exists():
        existing_item = _load_item(entity, source_id)
        if existing_item.get("status") not in {"open", "reopened"}:
            raise ValueError("A confirmed transaction cannot receive a new split proposal.")
    item = {
        "source_id": source_id,
        "date": _txn_date(txn).isoformat(),
        "amount": f"{amount:.2f}",
        "description": str(txn.get("description") or ""),
        "counterparty": _counterparty_key(str(txn.get("description") or ""), str(txn.get("counterparty") or "")),
        "proposed_category": "Split transaction",
        "proposal_type": "split",
        "split_postings": postings,
        "split_template": template or None,
        "liability_effect": _split_liability_effect(postings),
        "reasoning": _sanitize_reasoning(reasoning),
        "context": "",
        "status": "open",
        "confirmed_category": None,
        "original_amount": existing_item.get("original_amount", f"{amount:.2f}"),
        "delta": None,
        "corrected_at": None,
        "created_at": existing_item.get("created_at", now),
        "updated_at": now,
        "session_summary_id": None,
    }
    _save_item(entity, item)
    return item


def _write_split_entry(entity: Entity, txn: dict, item: dict, session_id: str, ts: Optional[str]) -> None:
    """Post the already-balanced reviewed split and retain its original source ID."""
    from .ledger.importer import _atomic_ledger_write, _get_existing_opens, _ledger_account_for_txn
    from .ledger.model import Entry, Open, Posting

    source_id = str(txn.get("id") or item["source_id"])
    amount = _txn_amount(txn)
    bank_account = _ledger_account_for_txn(txn, entity.entity_config.get("bank_account_mappings"))
    allocations = [
        Posting(account=str(posting["account"]), amount=Decimal(str(posting["amount"])), currency="USD")
        for posting in item.get("split_postings", [])
    ]
    entry = Entry(
        date=_txn_date(txn),
        narration=str(txn.get("description") or "Reviewed split"),
        flag="*",
        meta=(
            ("source-id", source_id),
            ("import-session", session_id),
            ("review-workflow", "split"),
        ),
        tags=(f"import-{session_id}", "reviewed-split"),
        postings=(Posting(account=bank_account, amount=amount, currency="USD"), *allocations),
    )
    existing_opens = _get_existing_opens(entity)
    needed = {bank_account, *(posting.account for posting in allocations)}
    opens = [Open(date=date(2000, 1, 1), account=account) for account in sorted(needed) if account not in existing_opens]
    _atomic_ledger_write(
        entity, opens, [entry], session_id, ts,
        f"queue confirm split source_id={source_id!r}", [dict(txn)],
    )
    from .ledger.staging import StagingStore
    StagingStore(entity.staging_dir).mark_seen(source_id)


def confirm_split(entity: Entity, item_id: str, session_id: str, ts: Optional[str] = None) -> dict:
    """Approve and post a prepared split without an interim single-category entry."""
    item = _load_item(entity, item_id)
    if item.get("proposal_type") != "split":
        raise ValueError(f"Queue item '{item_id}' is not a split proposal.")
    if item.get("status") not in {"open", "reopened"}:
        raise ValueError("Only open split proposals can be confirmed.")
    txn = _get_pending_txn(entity, str(item["source_id"]))
    if txn is None:
        raise ValueError("The staged transaction is no longer available for this split.")
    # Re-run the full validation at the approval boundary, including any chart changes.
    expected = -_txn_amount(txn)
    allocations = item.get("split_postings") or []
    _validate_known_accounts(entity, [str(posting.get("account") or "") for posting in allocations])
    total = sum((Decimal(str(posting.get("amount") or "0")) for posting in allocations), Decimal("0.00"))
    if total != expected:
        raise ValueError("The saved split no longer balances to the staged source amount.")
    _write_split_entry(entity, txn, item, session_id, ts)
    _remove_from_pending_categorization(entity, str(item["source_id"]))
    item["status"] = "confirmed"
    item["confirmed_category"] = "Split transaction"
    item["updated_at"] = ts or _now_iso()
    _save_item(entity, item)
    return item


# ---------------------------------------------------------------------------
# Native transfer pairing
# ---------------------------------------------------------------------------

def _transfer_item_id(source_ids: list[str]) -> str:
    digest = hashlib.sha256("\x1f".join(sorted(source_ids)).encode("utf-8")).hexdigest()[:20]
    return f"transfer-pair-{digest}"


def _transfer_details(entity: Entity, source_ids: list[str]) -> tuple[dict, dict, Decimal, str, str]:
    if len(source_ids) != 2 or source_ids[0] == source_ids[1]:
        raise ValueError("A transfer pair must contain two distinct source IDs.")
    first = _get_pending_txn(entity, source_ids[0])
    second = _get_pending_txn(entity, source_ids[1])
    if first is None or second is None:
        raise ValueError("Both transfer sides must still be staged transactions.")
    first_amount, second_amount = _txn_amount(first), _txn_amount(second)
    if first_amount + second_amount != Decimal("0.00"):
        raise ValueError("Transfer sides must have equal and opposite amounts.")
    from .ledger.importer import _ledger_account_for_txn
    mappings = entity.entity_config.get("bank_account_mappings")
    first_account = _ledger_account_for_txn(first, mappings)
    second_account = _ledger_account_for_txn(second, mappings)
    if first_account == second_account:
        raise ValueError("Transfer sides must come from different ledger accounts.")
    return first, second, first_amount, first_account, second_account


def find_transfer_candidates(entity: Entity, date_tolerance_days: int = 3) -> list[dict]:
    """Return non-mutating, amount-exact transfer-pair candidates from staged feeds."""
    if date_tolerance_days < 0:
        raise ValueError("Date tolerance must be zero or greater.")
    pending = _load_pending_categorization(entity)
    candidates: list[dict] = []
    for index, first in enumerate(pending):
        for second in pending[index + 1:]:
            try:
                amount = _txn_amount(first)
                if amount + _txn_amount(second) != Decimal("0.00"):
                    continue
                days_apart = abs((_txn_date(first) - _txn_date(second)).days)
                if days_apart > date_tolerance_days:
                    continue
                from .ledger.importer import _ledger_account_for_txn
                mappings = entity.entity_config.get("bank_account_mappings")
                first_account = _ledger_account_for_txn(first, mappings)
                second_account = _ledger_account_for_txn(second, mappings)
                if first_account == second_account:
                    continue
            except (ValueError, ArithmeticError):
                continue
            first_cp = _counterparty_key(str(first.get("description") or ""), str(first.get("counterparty") or ""))
            second_cp = _counterparty_key(str(second.get("description") or ""), str(second.get("counterparty") or ""))
            counterparty_match = bool(first_cp and first_cp == second_cp)
            account_types_differ = first_account.split(":", 1)[0] != second_account.split(":", 1)[0]
            score = 60 + (25 if counterparty_match else 0) + (15 if account_types_differ else 0) - min(days_apart * 5, 15)
            candidates.append({
                "source_ids": [str(first.get("id") or ""), str(second.get("id") or "")],
                "amount": f"{abs(amount):.2f}",
                "date_days_apart": days_apart,
                "accounts": [first_account, second_account],
                "counterparties": [first_cp, second_cp],
                "counterparty_match": counterparty_match,
                "score": score,
                "reason": "equal and opposite amount; different accounts; "
                          + ("counterparty matches" if counterparty_match else "counterparty differs"),
            })
    return sorted(candidates, key=lambda candidate: (-candidate["score"], candidate["date_days_apart"], candidate["source_ids"]))


def find_transfer_exceptions(entity: Entity, date_tolerance_days: int = 3) -> list[dict]:
    """Name transfer-like staged rows whose equal-and-opposite side is absent.

    This deliberately reports only candidate transfer rows without a pair. A
    confirmed or merely suggested pair is not a coverage exception, avoiding a
    misleading blanket count of every bank/card feed difference.
    """
    paired_ids = {
        source_id
        for candidate in find_transfer_candidates(entity, date_tolerance_days)
        for source_id in candidate["source_ids"]
    }
    transfer_words = re.compile(r"\b(transfer|payment|card|ach|wire)\b", re.IGNORECASE)
    exceptions: list[dict] = []
    from .ledger.importer import _ledger_account_for_txn
    mappings = entity.entity_config.get("bank_account_mappings")
    for txn in _load_pending_categorization(entity):
        source_id = str(txn.get("id") or "")
        description = str(txn.get("description") or "")
        if not source_id or source_id in paired_ids or not transfer_words.search(description):
            continue
        exceptions.append({
            "source_id": source_id,
            "date": _txn_date(txn).isoformat(),
            "amount": f"{_txn_amount(txn):.2f}",
            "account": _ledger_account_for_txn(txn, mappings),
            "description": description,
            "exception": "timing-or-missing-source",
            "expected_counterpart_window_days": date_tolerance_days,
        })
    return sorted(exceptions, key=lambda exception: (exception["date"], exception["source_id"]))


def propose_transfer(entity: Entity, source_ids: list[str], reasoning: str) -> dict:
    """Create an owner-reviewable transfer proposal for two staged feed rows."""
    first, second, amount, first_account, second_account = _transfer_details(entity, source_ids)
    item_id = _transfer_item_id(source_ids)
    existing_path = _item_path(entity, item_id)
    if existing_path.exists() and _load_item(entity, item_id).get("status") not in {"open", "reopened"}:
        raise ValueError("This transfer pair was already confirmed.")
    # A normal category proposal would otherwise be hidden by the pair and lead
    # to an ambiguous approval record.
    for source_id in source_ids:
        path = _item_path(entity, source_id)
        if path.exists() and _load_item(entity, source_id).get("status") in {"open", "reopened"}:
            raise ValueError(f"Source '{source_id}' already has a category proposal; resolve it first.")
    now = _now_iso()
    item = {
        "source_id": item_id,
        "source_ids": list(source_ids),
        "date": max(_txn_date(first), _txn_date(second)).isoformat(),
        "amount": f"{abs(amount):.2f}",
        "description": f"Transfer pair: {first.get('description') or source_ids[0]} / {second.get('description') or source_ids[1]}",
        "counterparty": "Internal transfer",
        "proposed_category": "Internal transfer",
        "proposal_type": "transfer-pair",
        "transfer_accounts": [first_account, second_account],
        "reasoning": _sanitize_reasoning(reasoning),
        "context": "",
        "status": "open",
        "confirmed_category": None,
        "original_amount": f"{abs(amount):.2f}",
        "delta": None,
        "corrected_at": None,
        "created_at": now,
        "updated_at": now,
        "session_summary_id": None,
    }
    _save_item(entity, item)
    return item


def confirm_transfer(entity: Entity, item_id: str, session_id: str, ts: Optional[str] = None) -> dict:
    """Post one audited two-account transfer and retain both source payloads."""
    item = _load_item(entity, item_id)
    if item.get("proposal_type") != "transfer-pair":
        raise ValueError(f"Queue item '{item_id}' is not a transfer pair.")
    if item.get("status") not in {"open", "reopened"}:
        raise ValueError("Only open transfer proposals can be confirmed.")
    source_ids = [str(source_id) for source_id in item.get("source_ids") or []]
    first, second, first_amount, first_account, second_account = _transfer_details(entity, source_ids)
    from .ledger.importer import _atomic_ledger_write, _get_existing_opens
    from .ledger.model import Entry, Open, Posting
    entry = Entry(
        date=max(_txn_date(first), _txn_date(second)),
        narration="Internal transfer: " + " / ".join(str(txn.get("description") or source_id) for txn, source_id in ((first, source_ids[0]), (second, source_ids[1]))),
        flag="*",
        meta=(
            ("source-id", source_ids[0]),
            ("paired-source-id", source_ids[1]),
            ("import-session", session_id),
            ("review-workflow", "transfer-pair"),
        ),
        tags=(f"import-{session_id}", "internal-transfer"),
        postings=(
            Posting(account=first_account, amount=first_amount, currency="USD"),
            Posting(account=second_account, amount=-first_amount, currency="USD"),
        ),
    )
    existing_opens = _get_existing_opens(entity)
    opens = [Open(date=date(2000, 1, 1), account=account) for account in (first_account, second_account) if account not in existing_opens]
    _atomic_ledger_write(
        entity, opens, [entry], session_id, ts,
        f"queue confirm transfer source_ids={source_ids!r}", [dict(first), dict(second)],
    )
    from .ledger.staging import StagingStore
    StagingStore(entity.staging_dir).bulk_mark_seen(source_ids)
    _remove_many_from_pending_categorization(entity, source_ids)
    item["status"] = "confirmed"
    item["confirmed_category"] = "Internal transfer"
    item["updated_at"] = ts or _now_iso()
    _save_item(entity, item)
    return item


# ---------------------------------------------------------------------------
# Related-entity policies
# ---------------------------------------------------------------------------

def propose_related_entity(entity: Entity, source_id: str, related_entity_name: str, reasoning: str) -> dict:
    """Apply an owner-authorized related-entity policy to one staged item.

    This writes only a review proposal. It never treats a related receipt as
    income merely because a counterparty name looks related.
    """
    from .entity import get_related_entity, load_entity

    txn = _get_pending_txn(entity, source_id)
    if txn is None:
        raise ValueError("Only staged transactions may receive a related-entity proposal.")
    # Reload the small configuration surface so a policy set immediately before
    # this proposal is honored even when a long-running caller holds Entity.
    policy = get_related_entity(load_entity(entity.path), related_entity_name)
    amount = _txn_amount(txn)
    if amount > 0:
        if policy["inbound_policy"] == "income":
            account = str(policy.get("inbound_income_account") or "")
            treatment = "owner-authorized inbound income fallback"
        else:
            account = str(policy["receivable_account"])
            treatment = "settle intercompany receivable"
    else:
        if policy["outbound_policy"] == "create-receivable":
            account = str(policy["receivable_account"])
            treatment = "create intercompany receivable"
        else:
            account = str(policy["payable_account"])
            treatment = "settle intercompany payable"
    item = propose(entity, source_id, account, reasoning, context=f"Related entity: {policy['name']}; {treatment}.")
    item["proposal_type"] = "related-entity"
    item["related_entity"] = policy["name"]
    item["related_entity_treatment"] = treatment
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

    This reuses the importer's ``_atomic_ledger_write`` directly rather than
    duplicating ledger-write logic.
    """
    from decimal import Decimal as D
    from datetime import date as _date
    from .ledger.model import Entry, Open, Posting
    from .ledger.staging import StagingStore
    from .ledger.importer import _atomic_ledger_write, _get_existing_opens, _ledger_account_for_txn

    staging = StagingStore(entity.staging_dir)

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
        new_opens=new_opens,
        new_entries=[entry],
        session_id=session_id,
        ts=ts,
        intent_description=f"queue confirm source_id={source_id!r} category={category!r}",
        source_transactions=[dict(txn)],
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

    if item.get("proposal_type") == "split":
        return confirm_split(entity, item_id, session_id, ts=ts)
    if item.get("proposal_type") == "transfer-pair":
        return confirm_transfer(entity, item_id, session_id, ts=ts)

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

    # Related-entity policies remain explicit for every item.  Learning this
    # counterparty as a normal category could silently bypass the policy and
    # turn later migration activity into income or an intercompany balance.
    if item.get("proposal_type") != "related-entity":
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
            f"Category '{category}' is not an account in the account catalog."
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


def confirm_group(entity: Entity, category: str, session_id: str, ts: Optional[str] = None) -> list[dict]:
    """Confirm all open proposals with one explicitly selected category.

    The caller must propose the group first, so the operation is auditable and
    cannot sweep unrelated staged activity into a bulk approval.
    """
    item_ids = [
        str(item["source_id"])
        for item in list_queue_items(entity)
        if item.get("status") in ("open", "reopened") and item.get("proposed_category") == category
    ]
    if not item_ids:
        raise ValueError(f"No open proposals found for category '{category}'.")
    return [confirm(entity, item_id, session_id, ts=ts) for item_id in item_ids]


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

    staging = StagingStore(entity.staging_dir)

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

    group_p = queue_sub.add_parser("propose-group", help="Propose one category for several staged items")
    group_p.add_argument("--entity", required=True, help="Path to entity directory")
    group_p.add_argument("--source-id", action="append", required=True, dest="source_ids", help="Transaction source ID; repeat for each item")
    group_p.add_argument("--category", required=True, help="Ledger account")
    group_p.add_argument("--reasoning", required=True, help="Reasoning text (sanitized)")
    group_p.add_argument("--context", default="", help="Additional context")

    split_p = queue_sub.add_parser("propose-split", help="Propose a balanced multi-line split from one staged transaction")
    split_p.add_argument("--entity", required=True, help="Path to entity directory")
    split_p.add_argument("--source-id", required=True, dest="source_id", help="Staged transaction source ID")
    split_p.add_argument("--posting", action="append", default=[], help="Non-cash posting as Account=SIGNED_AMOUNT; repeat")
    split_p.add_argument("--template", default="", help="Saved exact split template to use instead of --posting")
    split_p.add_argument("--reasoning", required=True, help="Reasoning text (sanitized)")

    split_template_p = queue_sub.add_parser("split-template-save", help="Save an exact reusable split allocation")
    split_template_p.add_argument("--entity", required=True, help="Path to entity directory")
    split_template_p.add_argument("--name", required=True, help="Template name")
    split_template_p.add_argument("--posting", action="append", required=True, help="Posting as Account=SIGNED_AMOUNT; repeat")
    split_template_list_p = queue_sub.add_parser("split-template-list", help="List saved split allocations")
    split_template_list_p.add_argument("--entity", required=True, help="Path to entity directory")

    transfer_candidates_p = queue_sub.add_parser("transfer-candidates", help="Find staged equal-and-opposite transfer candidates")
    transfer_candidates_p.add_argument("--entity", required=True, help="Path to entity directory")
    transfer_candidates_p.add_argument("--date-tolerance-days", type=int, default=3, help="Maximum date gap; default 3")
    transfer_exceptions_p = queue_sub.add_parser("transfer-exceptions", help="List unmatched transfer-like rows as timing or missing-source exceptions")
    transfer_exceptions_p.add_argument("--entity", required=True, help="Path to entity directory")
    transfer_exceptions_p.add_argument("--date-tolerance-days", type=int, default=3, help="Maximum date gap; default 3")
    transfer_p = queue_sub.add_parser("propose-transfer", help="Propose one audited pair of staged transfer rows")
    transfer_p.add_argument("--entity", required=True, help="Path to entity directory")
    transfer_p.add_argument("--source-id", action="append", required=True, dest="source_ids", help="One of exactly two staged source IDs")
    transfer_p.add_argument("--reasoning", required=True, help="Reasoning text (sanitized)")

    # confirm
    conf_p = queue_sub.add_parser("confirm", help="Confirm a proposed categorization")
    conf_p.add_argument("--entity", required=True, help="Path to entity directory")
    conf_p.add_argument("--item", required=True, dest="item_id", help="Queue item ID (source ID)")
    conf_p.add_argument("--session", default="queue-session", dest="session_id", help="Session ID")

    confirm_group_p = queue_sub.add_parser("confirm-group", help="Confirm every open proposal for one category")
    confirm_group_p.add_argument("--entity", required=True, help="Path to entity directory")
    confirm_group_p.add_argument("--category", required=True, help="Proposed ledger account to confirm")
    confirm_group_p.add_argument("--session", default="queue-session", dest="session_id", help="Session ID")

    confirm_split_p = queue_sub.add_parser("confirm-split", help="Confirm and post a proposed split")
    confirm_split_p.add_argument("--entity", required=True, help="Path to entity directory")
    confirm_split_p.add_argument("--item", required=True, dest="item_id", help="Split queue item ID")
    confirm_split_p.add_argument("--session", default="queue-session", dest="session_id", help="Session ID")
    confirm_transfer_p = queue_sub.add_parser("confirm-transfer", help="Confirm and post a proposed transfer pair")
    confirm_transfer_p.add_argument("--entity", required=True, help="Path to entity directory")
    confirm_transfer_p.add_argument("--item", required=True, dest="item_id", help="Transfer-pair queue item ID")
    confirm_transfer_p.add_argument("--session", default="queue-session", dest="session_id", help="Session ID")

    # correct
    corr_p = queue_sub.add_parser("correct", help="Correct a proposed categorization")
    corr_p.add_argument("--entity", required=True, help="Path to entity directory")
    corr_p.add_argument("--item", required=True, dest="item_id", help="Queue item ID (source ID)")
    corr_p.add_argument("--category", required=True, help="Corrected ledger account")
    corr_p.add_argument("--note", default="", help="Optional note")
    corr_p.add_argument("--session", default="queue-session", dest="session_id", help="Session ID")

    duplicate_p = queue_sub.add_parser("resolve-duplicate", help="Resolve a possible legacy-ID duplicate")
    duplicate_p.add_argument("--entity", required=True, help="Path to entity directory")
    duplicate_p.add_argument("--source-id", required=True, dest="source_id", help="Candidate source ID")
    duplicate_p.add_argument("--decision", required=True, choices=["duplicate", "distinct"])
    duplicate_p.add_argument("--session", default="duplicate-review", dest="session_id", help="Session ID")

    related_p = queue_sub.add_parser("propose-related", help="Propose one owner-authorized related-entity treatment")
    related_p.add_argument("--entity", required=True, help="Path to entity directory")
    related_p.add_argument("--source-id", required=True, dest="source_id", help="Staged transaction source ID")
    related_p.add_argument("--related-entity", required=True, dest="related_entity", help="Configured related entity name")
    related_p.add_argument("--reasoning", required=True, help="Reasoning text (sanitized)")

    # list
    list_p = queue_sub.add_parser("list", help="List queue items")
    list_p.add_argument("--entity", required=True, help="Path to entity directory")
    list_p.add_argument("--status", default=None, help="Filter by status (open/confirmed/corrected/reopened)")

    summary_p = queue_sub.add_parser(
        "summary",
        help="Summarize review work by proposed treatment without posting changes",
    )
    summary_p.add_argument("--entity", required=True, help="Path to entity directory")
    summary_p.add_argument("--status", default="open", help="Filter by status (default: open)")

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

        elif qcmd == "propose-group":
            try:
                items = propose_group(entity, args.source_ids, args.category, args.reasoning, args.context)
                print(f"Proposed {len(items)} item(s) → {args.category}")
                return 0
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1

        elif qcmd == "propose-split":
            try:
                if args.template and args.posting:
                    raise ValueError("Use either --template or --posting, not both.")
                item = propose_split(entity, args.source_id, args.reasoning, args.posting, args.template)
                print(f"Proposed split: {item['source_id']}")
                for effect in item["liability_effect"]:
                    print(f"  Liability effect: {effect['account']} {effect['amount']}")
                return 0
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1

        elif qcmd == "split-template-save":
            try:
                report = save_split_template(entity, args.name, args.posting)
                verb = "Updated" if report["status"] == "updated" else "Saved"
                print(f"{verb} split template {report['name']} with {len(report['postings'])} posting(s).")
                return 0
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1

        elif qcmd == "split-template-list":
            templates = list_split_templates(entity)
            if not templates:
                print("No split templates saved.")
                return 0
            for name, postings in sorted(templates.items()):
                print(f"{name}: " + ", ".join(f"{p['account']}={p['amount']}" for p in postings))
            return 0

        elif qcmd == "transfer-candidates":
            try:
                candidates = find_transfer_candidates(entity, args.date_tolerance_days)
                if not candidates:
                    print("No staged transfer candidates found.")
                    return 0
                for candidate in candidates:
                    print(
                        f"{candidate['source_ids'][0]} + {candidate['source_ids'][1]}: "
                        f"{candidate['amount']} across {candidate['accounts'][0]} / {candidate['accounts'][1]}; "
                        f"{candidate['date_days_apart']} day(s), score {candidate['score']}"
                    )
                return 0
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1

        elif qcmd == "transfer-exceptions":
            try:
                exceptions = find_transfer_exceptions(entity, args.date_tolerance_days)
                if not exceptions:
                    print("No unmatched transfer-like rows found.")
                    return 0
                for exception in exceptions:
                    print(
                        f"{exception['source_id']}: {exception['date']} {exception['amount']} "
                        f"{exception['account']} — {exception['exception']} "
                        f"(counterpart window ±{exception['expected_counterpart_window_days']} days)"
                    )
                return 0
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1

        elif qcmd == "propose-transfer":
            try:
                item = propose_transfer(entity, args.source_ids, args.reasoning)
                print(f"Proposed transfer pair: {item['source_id']}")
                print("  Sources: " + ", ".join(item["source_ids"]))
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

        elif qcmd == "confirm-group":
            try:
                items = confirm_group(entity, args.category, args.session_id)
                print(f"Confirmed {len(items)} item(s) → {args.category}")
                return 0
            except (FileNotFoundError, ValueError) as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1

        elif qcmd == "confirm-split":
            try:
                item = confirm_split(entity, args.item_id, args.session_id)
                print(f"Confirmed split: {item['source_id']}")
                return 0
            except (FileNotFoundError, ValueError) as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1

        elif qcmd == "confirm-transfer":
            try:
                item = confirm_transfer(entity, args.item_id, args.session_id)
                print("Confirmed transfer pair: " + ", ".join(item["source_ids"]))
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

        elif qcmd == "resolve-duplicate":
            try:
                candidate = resolve_duplicate_candidate(
                    entity,
                    args.source_id,
                    args.decision,
                    args.session_id,
                )
                print(f"Resolved duplicate candidate {candidate['source_id']} as {args.decision}.")
                if args.decision == "distinct":
                    print("Distinct activity was released to the normal categorization workflow.")
                return 0
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1

        elif qcmd == "propose-related":
            try:
                item = propose_related_entity(entity, args.source_id, args.related_entity, args.reasoning)
                print(f"Proposed related-entity treatment: {item['source_id']} → {item['proposed_category']}")
                print(f"  {item['related_entity']}: {item['related_entity_treatment']}")
                return 0
            except ValueError as exc:
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

        elif qcmd == "summary":
            groups = summarize_queue_items(entity, status=args.status)
            if not groups:
                print("No review items found.")
                return 0
            print("Review groups:")
            for group in groups:
                date_range = group["date_from"] or "unknown date"
                if group["date_to"] and group["date_to"] != group["date_from"]:
                    date_range += f" through {group['date_to']}"
                samples = ", ".join(group["sample_counterparties"]) or "no sample counterparty"
                print(
                    f"  {group['treatment']}: {group['count']} item(s), "
                    f"{group['total']}, {date_range}; samples: {samples}"
                )
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
