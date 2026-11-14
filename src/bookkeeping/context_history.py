"""Context-only feed history: rows a feed account sends for dates the books already cover.

A person can declare that the books already hold an account's history through a
date from an earlier source. They set ``cutover_date`` and
``history_before_cutover: context_only`` on the feed account's mapping in
``bank_account_mappings`` (``books entity bank-account-map ... --cutover-date D
--history-before-cutover context_only``).

A posted row from that feed dated on or before the cutover date is then:

- stored here, in ``staging/context-history.json``, for audit and so that a
  later sync of the same row is recognized and skipped;
- never posted to the ledger;
- never a review item. It is not in ``books queue list``, not in
  ``books queue duplicates``, and it does not block a period close.

One summary per account (``books queue context-history --json``) tells a host
what was left out, so a person sees one notice and not one item per row.

A person can still release a row on purpose (``books queue context-release``),
and can convert rows that an earlier import already held for review
(``books queue context-adopt``). ``--backfill`` on ``books ingest`` bypasses the
cutover for one import, as before.

The default mode is ``review``: nothing here applies, and each such row is held
in "To check" as it always was. This module changes no posting rule.
"""

from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .source_ids import source_family, source_id_variants

CONTEXT_FILE = "context-history.json"
CONTEXT_STATUS = "context-only"
RELEASED_STATUS = "released"
_ALIASES_FILE = "source-id-aliases.json"
_DUPLICATES_FILE = "duplicate-candidates.json"


def _path(entity: Any) -> Path:
    return Path(entity.staging_dir) / CONTEXT_FILE


def _load_list(path: Path) -> list[dict[str, Any]]:
    """Read a JSON list. A missing file is empty; a damaged file raises, so it is never overwritten."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read {path.name}; repair it before importing: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(f"{path.name} is not a JSON list.")
    return [item for item in data if isinstance(item, dict)]


def _save_list(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(items, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_records(entity: Any) -> list[dict[str, Any]]:
    return _load_list(_path(entity))


def known_ids(entity: Any) -> set[str]:
    """Every spelling of every source ID that is stored as context (released rows excluded)."""
    found: set[str] = set()
    for record in load_records(entity):
        if record.get("status") == CONTEXT_STATUS:
            found |= source_id_variants(record.get("source_id"))
    return found


def _amount_text(txn: dict[str, Any]) -> str:
    try:
        if txn.get("amount") is None:
            value = Decimal(str(txn.get("creditAmount") or "0")) - Decimal(str(txn.get("debitAmount") or "0"))
        else:
            value = Decimal(str(txn["amount"]))
        return format(value, "f")
    except (ArithmeticError, ValueError):
        return str(txn.get("amount") or "")


def make_record(txn: dict[str, Any], *, account: str, cutover_date: str, session_id: str, created_at: str) -> dict[str, Any]:
    source_id = str(txn.get("id") or "")
    return {
        "source_id": source_id, "status": CONTEXT_STATUS, "account": account,
        "date": str(txn.get("date") or "")[:10], "amount": _amount_text(txn),
        "description": str(txn.get("description") or ""), "feed": source_family(source_id),
        "cutover_date": cutover_date, "session_id": session_id, "created_at": created_at,
        "transaction": dict(txn),
    }


def append_records(entity: Any, records: list[dict[str, Any]]) -> None:
    """Add records once. A source ID that is already stored as context is not added again."""
    if not records:
        return
    path = _path(entity)
    current = _load_list(path)
    have: set[str] = set()
    for item in current:
        if item.get("status") == CONTEXT_STATUS:
            have |= source_id_variants(item.get("source_id"))
    fresh = [r for r in records if not (source_id_variants(r["source_id"]) & have)]
    if fresh:
        _save_list(path, current + fresh)


def list_context_history(entity: Any, account: str | None = None, include_released: bool = False) -> list[dict[str, Any]]:
    """Context rows, oldest first. Read-only."""
    rows = [r for r in load_records(entity)
            if (include_released or r.get("status") == CONTEXT_STATUS) and (not account or r.get("account") == account)]
    return sorted(rows, key=lambda r: (str(r.get("date") or ""), str(r.get("source_id") or "")))


def _feed_label(feed: str) -> str:
    return {"unprefixed": "the earlier feed", "other": "the file import"}.get(feed, feed)


def summarize_context_history(entity: Any) -> dict[str, Any]:
    """One summary per account and feed. A host shows at most one notice per entry.

    Read-only. ``message`` is plain text for the owner; ``count`` and the dates are
    the values it states.
    """
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for record in list_context_history(entity):
        groups.setdefault((str(record.get("account")), str(record.get("feed")), str(record.get("cutover_date"))),
                          []).append(record)
    accounts = []
    for (account, feed, cutover), rows in sorted(groups.items()):
        n = len(rows)
        noun = "transaction" if n == 1 else "transactions"
        verb = "was" if n == 1 else "were"
        accounts.append({
            "account": account, "feed": feed, "cutover_date": cutover, "count": n,
            "first_date": rows[0]["date"], "last_date": rows[-1]["date"],
            "message": (f"{n} earlier {noun} from {_feed_label(feed)} {verb} not imported because the books "
                        f"already cover them through {cutover}."),
        })
    return {"schema_version": 1, "kind": "context_history_summary",
            "total_count": sum(item["count"] for item in accounts), "accounts": accounts}


def _mode_ids(entity: Any, txns: list[dict[str, Any]]) -> dict[str, str]:
    from .bank_accounts import context_only_ids
    return context_only_ids(entity, txns)


def release_context_rows(entity: Any, source_ids: list[str], session_id: str = "context-release",
                         *, duplicate_guard: bool = False) -> dict[str, Any]:
    """Release stored context rows into the normal import path, on purpose.

    By default the person has decided that the row is real, distinct activity: it
    skips the duplicate plan, like ``books queue resolve-duplicate distinct``, and then
    posts or queues as any row does. With ``duplicate_guard`` the duplicate plan still
    runs, so a row that repeats a posted or queued row is held in "To check".
    Every ID must be stored context; the call changes nothing when one is not.
    """
    from .ledger.importer import _entity_write_lock, import_transactions
    from .queue import make_categorizer

    wanted = list(dict.fromkeys(str(s) for s in source_ids))
    with _entity_write_lock(entity.path):
        records = load_records(entity)
        chosen = []
        for sid in wanted:
            found = next((r for r in records if r.get("status") == CONTEXT_STATUS
                          and source_id_variants(r.get("source_id")) & source_id_variants(sid)), None)
            if found is None:
                raise ValueError(f"No context-only row found for source ID '{sid}'.")
            chosen.append(found)
        txns = [r["transaction"] for r in chosen if isinstance(r.get("transaction"), dict)]
        if len(txns) != len(chosen):
            raise ValueError("A context row lacks its source transaction. Re-download and ingest it again.")
        result = import_transactions(entity, txns, session_id, categorizer=make_categorizer(entity),
                                     legacy_duplicate_guard=duplicate_guard)
        if result.errors:
            raise ValueError("Could not release context rows: " + "; ".join(result.errors))
        if result.before_opening:
            first = result.before_opening[0]
            raise ValueError(f"Could not release context rows: {first['source_id']} is dated before the opening "
                             f"balance of {first['account']} on {first['opening_date']}, which already includes it.")
        stamp = _now()
        for record in chosen:
            record["status"] = RELEASED_STATUS
            record["released_at"] = stamp
            record["release_session"] = session_id
        aliases_path = Path(entity.staging_dir) / _ALIASES_FILE
        aliases = _load_list(aliases_path)
        if not duplicate_guard:
            for record in chosen:
                aliases.append({"source_id": record["source_id"], "status": "confirmed-distinct", "resolved_at": stamp})
            _save_list(aliases_path, aliases)
        _save_list(_path(entity), records)
    return {"schema_version": 1, "kind": "context_release", "released": len(chosen),
            "posted": result.new_entries, "queued_for_review": result.pending_categorization,
            "held_as_duplicate": result.duplicate_candidates, "skipped_duplicate": result.skipped_duplicate}


def rows_after(entity: Any, account: str, after: date) -> list[str]:
    """Source IDs of stored context rows on *account* dated after *after*.

    For a person who moved a cutover date earlier: these rows are no longer before it.
    """
    out = []
    for record in list_context_history(entity, account):
        try:
            if date.fromisoformat(str(record.get("date"))) > after:
                out.append(str(record["source_id"]))
        except ValueError:
            continue
    return out


def adopt_open_holds(entity: Any, account: str | None = None) -> dict[str, Any]:
    """Move rows that an earlier import held for review into context history.

    Only an open duplicate candidate qualifies, and only when its mapping is now
    ``context_only`` and its date is on or before that mapping's cutover date. The
    candidate is closed with status ``moved-to-context-history``, which keeps the
    record for audit. The row is not posted. Rows that do not qualify stay in "To check".
    """
    from .ledger.importer import _entity_write_lock

    with _entity_write_lock(entity.path):
        path = Path(entity.staging_dir) / _DUPLICATES_FILE
        candidates = _load_list(path)
        open_items = [c for c in candidates if c.get("status") == "duplicate-candidate"
                      and isinstance(c.get("transaction"), dict) and (not account or c.get("account") == account)]
        ids = _mode_ids(entity, [c["transaction"] for c in open_items])
        stamp = _now()
        moved = []
        for item in open_items:
            sid = str(item["transaction"].get("id") or "")
            if sid not in ids:
                continue
            moved.append(make_record(item["transaction"], account=str(item.get("account") or ""),
                                     cutover_date=ids[sid], session_id="context-adopt", created_at=stamp))
            item["status"] = "moved-to-context-history"
            item["resolved_at"] = stamp
        if moved:
            append_records(entity, moved)
            _save_list(path, candidates)
    return {"schema_version": 1, "kind": "context_adopt", "moved": len(moved),
            "left_in_review": len(open_items) - len(moved)}


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
