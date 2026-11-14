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

A record has a status: ``context-only`` (stored, not in the books), ``released`` (a person
released it, or a later sync sent it down the normal path and it is now posted, queued or
held). The file is ``{"schema_version": 1, "rows": [...]}``. It is read in full once for
each ingest and rewritten once for each ingest that adds rows. A two-year history of a busy
account is a few thousand rows, a few megabytes at most; that is the size this design expects.

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


SCHEMA_VERSION = 1


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


def _save_list(path: Path, items: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(items, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _load_rows(path: Path) -> list[dict[str, Any]]:
    """Read the context file. A missing file is empty; a damaged file or an unknown schema raises."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read {path.name}; repair it before importing: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION or not isinstance(data.get("rows"), list):
        raise ValueError(f"{path.name} has an unknown format; repair it before importing.")
    return [item for item in data["rows"] if isinstance(item, dict)]


def _save_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    _save_list(path, {"schema_version": SCHEMA_VERSION, "rows": rows})  # type: ignore[arg-type]


def load_records(entity: Any) -> list[dict[str, Any]]:
    return _load_rows(_path(entity))


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
    current = _load_rows(path)
    have: set[str] = set()
    for item in current:
        if item.get("status") == CONTEXT_STATUS:
            have |= source_id_variants(item.get("source_id"))
    fresh = [r for r in records if not (source_id_variants(r["source_id"]) & have)]
    if fresh:
        _save_rows(path, current + fresh)


def list_context_history(entity: Any, account: str | None = None, include_released: bool = False) -> list[dict[str, Any]]:
    """Context rows, oldest first. Read-only."""
    rows = [r for r in load_records(entity)
            if (include_released or r.get("status") == CONTEXT_STATUS) and (not account or r.get("account") == account)]
    return sorted(rows, key=lambda r: (str(r.get("date") or ""), str(r.get("source_id") or "")))


def _feed_label(feed: str) -> str:
    return {"unprefixed": "the earlier feed", "other": "the file import"}.get(feed, feed)


def _active_cutovers(entity: Any) -> dict[str, str]:
    """``{ledger account: latest cutover date}`` over the mappings that are context only now."""
    from .entity import bank_mapping_account, bank_mapping_details

    found: dict[str, str] = {}
    mappings = entity.entity_config.get("bank_account_mappings")
    for value in (mappings.values() if isinstance(mappings, dict) else []):
        details = bank_mapping_details(value)
        account = bank_mapping_account(value)
        if account and details.get("history_before_cutover") == "context_only" and details.get("cutover_date"):
            found[account] = max(str(details["cutover_date"]), found.get(account, ""))
    return found


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def summarize_context_history(entity: Any) -> dict[str, Any]:
    """One summary per account and feed. A host shows at most one notice per entry.

    Read-only. ``count`` is the number of stored source IDs; ``distinct_count`` is the number
    of different (date, amount, description) rows among them, because a provider that issues new
    IDs for the same transaction is stored once for each ID. An entry is ``active`` when the
    account's mapping is still ``context_only`` and the row is on or before its cutover date. Only an
    active entry says the rows are "not imported". An inactive entry says the setting no longer
    covers the rows; they go to review or the normal path when the provider sends them again.
    """
    active_cutover = _active_cutovers(entity)
    groups: dict[tuple[str, str, str, bool], list[dict[str, Any]]] = {}
    for record in list_context_history(entity):
        account = str(record.get("account"))
        limit = active_cutover.get(account)
        is_active = limit is not None and str(record.get("date")) <= limit
        shown_cutover = limit if is_active else str(record.get("cutover_date"))
        groups.setdefault((account, str(record.get("feed")), shown_cutover or "", is_active), []).append(record)
    accounts = []
    for (account, feed, cutover, is_active), rows in sorted(groups.items()):
        n = len(rows)
        distinct = len({(r.get("date"), r.get("amount"), r.get("description")) for r in rows})
        label = _feed_label(feed)
        if is_active:
            if distinct == n:
                message = (f"{n} earlier {_plural(n, 'transaction', 'transactions')} from {label} "
                           f"{_plural(n, 'was', 'were')} not imported because the books already cover them through {cutover}.")
            else:
                message = (f"{n} earlier transaction records from {label} ({distinct} different by date, amount and "
                           f"description) were not imported because the books already cover them through {cutover}.")
        else:
            message = (f"{n} stored earlier transaction {_plural(n, 'record', 'records')} from {label} "
                       f"{_plural(n, 'is', 'are')} not in the books, and this account's setting no longer covers "
                       f"{_plural(n, 'it', 'them')}. If the provider sends {_plural(n, 'it', 'them')} again, "
                       f"{_plural(n, 'it goes', 'they go')} to review or the normal path.")
        accounts.append({
            "account": account, "feed": feed, "cutover_date": cutover, "count": n, "distinct_count": distinct,
            "active": is_active, "first_date": rows[0]["date"], "last_date": rows[-1]["date"], "message": message,
        })
    return {"schema_version": 1, "kind": "context_history_summary",
            "total_count": sum(item["count"] for item in accounts if item["active"]),
            "inactive_count": sum(item["count"] for item in accounts if not item["active"]),
            "accounts": accounts}


def _mode_ids(entity: Any, txns: list[dict[str, Any]]) -> dict[str, str]:
    from .bank_accounts import context_only_ids
    return context_only_ids(entity, txns)


def outcome_ids(entity: Any, source_ids: list[str]) -> set[str]:
    """Which of *source_ids* are now posted, queued for review or held in "To check"."""
    from .ledger.importer import _load_pending_categorization
    from .ledger.store import LedgerStore, default_store_path

    store_path = default_store_path(entity.path)
    store = LedgerStore(store_path) if store_path.exists() else None
    queued: set[str] = set()
    for txn in _load_pending_categorization(entity):
        queued |= source_id_variants(txn.get("id"))
    held: set[str] = set()
    for item in _load_list(Path(entity.staging_dir) / _DUPLICATES_FILE):
        if item.get("status") == "duplicate-candidate":
            held |= source_id_variants(item.get("source_id"))
    done = set()
    for sid in source_ids:
        spellings = source_id_variants(sid)
        if spellings & queued or spellings & held or (store is not None and any(store.source_exists(x) for x in spellings)):
            done.add(sid)
    return done


def mark_released(entity: Any, source_ids: list[str], *, session_id: str, how: str) -> int:
    """Mark stored context rows ``released`` (``how``: ``released`` by a person, or ``normal-path``)."""
    if not source_ids:
        return 0
    wanted: set[str] = set()
    for sid in source_ids:
        wanted |= source_id_variants(sid)
    path = _path(entity)
    records = _load_rows(path)
    stamp = _now()
    count = 0
    for record in records:
        if record.get("status") == CONTEXT_STATUS and source_id_variants(record.get("source_id")) & wanted:
            record.update(status=RELEASED_STATUS, released_at=stamp, release_session=session_id, release_how=how)
            count += 1
    if count:
        _save_rows(path, records)
    return count


def _opening_refusals(entity: Any, chosen: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Rows dated before their account's recorded opening balance, which already includes them."""
    from .statement_opening import opening_dates

    openings = opening_dates(entity.path)
    refused = {}
    for record in chosen:
        opening = openings.get(str(record.get("account")))
        try:
            day = date.fromisoformat(str(record.get("date")))
        except ValueError:
            continue
        if opening is not None and day < opening:
            refused[str(record["source_id"])] = {"reason": "before_opening", "opening_date": opening.isoformat()}
    return refused


def release_context_rows(entity: Any, source_ids: list[str], session_id: str = "context-release",
                         *, duplicate_guard: bool = False, force: bool = False) -> dict[str, Any]:
    """Release stored context rows into the normal import path, on purpose.

    Every ID must be stored context, or the call changes nothing. Before the import the call
    reads the state files strictly and sets aside (``refused``, with a reason) each row that

    - is dated before its account's recorded opening balance (``before_opening``), or
    - without ``duplicate_guard`` and without ``force``, matches a posted entry or a queued
      review item on the same account by the statement duplicate plan (``matches_existing_entry``,
      with ``match_rule`` and the matched entry).

    The other rows are imported. With ``duplicate_guard`` the duplicate plan runs in the import
    and holds a match in "To check". Without it (and with ``force`` for a refused match) the
    person has decided the row is real, and it posts or queues like any row. After the import
    a record is marked ``released`` exactly when its ID is posted, queued or held; any other
    row stays context only and is listed in ``not_released``. Rows already released are not
    undone, so a rerun after a failure is safe.
    """
    from .ledger.importer import _entity_write_lock, _plan_feed_holds, import_transactions
    from .queue import make_categorizer

    wanted = list(dict.fromkeys(str(s) for s in source_ids))
    with _entity_write_lock(entity.path):
        records = load_records(entity)
        aliases_path = Path(entity.staging_dir) / _ALIASES_FILE
        aliases = _load_list(aliases_path)  # strict: a damaged file stops the call before any write
        chosen = []
        for sid in wanted:
            found = next((r for r in records if r.get("status") == CONTEXT_STATUS
                          and source_id_variants(r.get("source_id")) & source_id_variants(sid)), None)
            if found is None:
                raise ValueError(f"No context-only row found for source ID '{sid}'.")
            chosen.append(found)
        if any(not isinstance(r.get("transaction"), dict) for r in chosen):
            raise ValueError("A context row lacks its source transaction. Re-download and ingest it again.")
        refused = _opening_refusals(entity, chosen)
        if not duplicate_guard and not force:
            rest = [r["transaction"] for r in chosen if str(r["source_id"]) not in refused]
            for sid, hold in _plan_feed_holds(entity, rest).items():
                refused[sid] = {"reason": "matches_existing_entry", "match_rule": hold.get("match_rule"),
                                "existing_source_id": hold.get("existing_source_id"),
                                "existing_date": hold.get("existing_date") or hold.get("date"),
                                "amount": hold.get("amount"), "description": hold.get("description")}
        to_import = [r for r in chosen if str(r["source_id"]) not in refused]
        result = None
        if to_import:
            result = import_transactions(entity, [r["transaction"] for r in to_import], session_id,
                                         categorizer=make_categorizer(entity), legacy_duplicate_guard=duplicate_guard)
        done = outcome_ids(entity, [str(r["source_id"]) for r in to_import])
        released = [sid for sid in (str(r["source_id"]) for r in to_import) if sid in done]
        mark_released(entity, released, session_id=session_id, how="released")
        if released and not duplicate_guard:
            stamp = _now()
            aliases.extend({"source_id": sid, "status": "confirmed-distinct", "resolved_at": stamp} for sid in released)
            _save_list(aliases_path, aliases)
    not_released = [{"source_id": str(r["source_id"]), "reason": "not_imported"} for r in to_import
                    if str(r["source_id"]) not in done]
    return {"schema_version": 1, "kind": "context_release", "released": len(released),
            "posted": result.new_entries if result else 0,
            "queued_for_review": result.pending_categorization if result else 0,
            "held_as_duplicate": result.duplicate_candidates if result else 0,
            "skipped_duplicate": result.skipped_duplicate if result else 0,
            "refused": [{"source_id": sid, **info} for sid, info in sorted(refused.items())],
            "not_released": not_released, "errors": list(result.errors) if result else []}


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


def adopt_open_holds(entity: Any, account: str | None = None, *, include_unmatched: bool = False) -> dict[str, Any]:
    """Move rows that an earlier import held for review into context history.

    Only an open duplicate candidate qualifies, and only when its mapping is now
    ``context_only`` and its date is on or before that mapping's cutover date. A hold
    that matched a book entry (a known repeat) moves. A hold with ``match_rule``
    ``before_cutover_not_in_books`` is a row that the books never had: it may be a real
    transaction that the earlier source never sent, so it stays in "To check" unless
    ``include_unmatched`` is given. A moved candidate is closed with status
    ``moved-to-context-history`` (kept for audit). Nothing is posted. The result counts
    ``moved`` and ``left_in_review`` by ``match_rule``.
    """
    from .ledger.importer import _entity_write_lock

    with _entity_write_lock(entity.path):
        path = Path(entity.staging_dir) / _DUPLICATES_FILE
        candidates = _load_list(path)
        open_items = [c for c in candidates if c.get("status") == "duplicate-candidate"
                      and isinstance(c.get("transaction"), dict) and (not account or c.get("account") == account)]
        ids = _mode_ids(entity, [c["transaction"] for c in open_items])
        stamp = _now()
        moved: list[dict[str, Any]] = []
        by_moved: dict[str, int] = {}
        by_left: dict[str, int] = {}
        for item in open_items:
            sid = str(item["transaction"].get("id") or "")
            rule = str(item.get("match_rule") or "unknown")
            if sid not in ids or (rule == "before_cutover_not_in_books" and not include_unmatched):
                by_left[rule] = by_left.get(rule, 0) + 1
                continue
            moved.append(make_record(item["transaction"], account=str(item.get("account") or ""),
                                     cutover_date=ids[sid], session_id="context-adopt", created_at=stamp))
            by_moved[rule] = by_moved.get(rule, 0) + 1
            item["status"] = "moved-to-context-history"
            item["resolved_at"] = stamp
        if moved:
            append_records(entity, moved)
            _save_list(path, candidates)
    return {"schema_version": 1, "kind": "context_adopt", "moved": len(moved),
            "left_in_review": sum(by_left.values()), "moved_by_match_rule": dict(sorted(by_moved.items())),
            "left_by_match_rule": dict(sorted(by_left.items()))}


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
