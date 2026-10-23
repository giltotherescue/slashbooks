"""``books ledger reclassify``: move posted entries from one income or expense account to another.

Each moved entry gets a reversing entry and a replacement entry, written in one ledger
transaction, like ``ledger reclassify-bank``. The replacement keeps the date, payee,
narration, flag, tags, links and metadata of the entry; only the account changes. The
reversal keeps the payee, tags and links too, so a report by payee or by tag nets to the
replacement. The original entry stays in the ledger.

The command previews first. ``--apply --plan-hash H`` writes the preview that was shown.
It refuses the whole request, before any write, when an entry is in a locked month
(``entity.json`` ``closed_periods`` or ``closed_through``, or a closed accrual period).
A source ID that names an entry that was already moved (or an earlier spelling of it)
is reported in ``already_moved``, so a repeated request writes nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from typing import Any, Sequence

from ..source_ids import source_id_variants
from .model import Entry, Posting

KIND = "ledger_reclassify"
CATEGORY_ROOTS = ("Income", "Expenses")
MAX_ENTRIES = 1000
DEFAULT_SESSION = "ledger-reclassify"
_MAX_CHAIN = 100
_LOCKED_STATUSES = ("closed", "locked")


class ReclassifyError(ValueError):
    """The request cannot be applied. ``code`` names the rule; ``refs`` lists the source IDs."""

    def __init__(self, code: str, message: str, action: str = "Correct the request and run the preview again.",
                 refs: Sequence[str] = ()) -> None:
        self.code, self.action, self.refs = code, action, list(refs)
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": str(self), "action": self.action}
        if self.refs:
            error["refs"] = self.refs
        return {"status": "blocked", "errors": [error]}


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "reclassify",
        help="Move posted entries from one income or expense account to another, keeping payee, tags and metadata. "
             "Previews unless --apply is given",
    )
    parser.add_argument("--entity", required=True)
    parser.add_argument("--from-account", required=True, help="Income or expense account the entries post to now")
    parser.add_argument("--to-account", required=True, help="Existing income or expense account to move them to")
    parser.add_argument("--source-id", action="append", required=True, dest="source_ids", metavar="ID",
                        help="Source ID of a posted entry; repeat for each entry (at most 1000)")
    parser.add_argument("--session", default=DEFAULT_SESSION)
    parser.add_argument("--apply", action="store_true", help="Make the change. Without it the command only previews")
    parser.add_argument("--plan-hash", default=None, help="With --apply: the plan_hash from the preview, to refuse a changed company")


def _variants(values: Any) -> set[str]:
    result: set[str] = set()
    for value in values:
        if value:
            result |= source_id_variants(value)
    return result


class _Ledger:
    """The live entry for a source ID, following reversal and correction lineage."""

    def __init__(self, entries: list[Entry]) -> None:
        self.reversed = _variants(dict(entry.meta).get("reverses") for entry in entries)
        self.by_source: dict[str, Entry] = {}
        self.corrected_by: dict[str, Entry] = {}
        for entry in entries:
            meta = dict(entry.meta)
            if "reverses" in meta:
                continue
            if entry.source_id:
                self.by_source[entry.source_id] = entry  # The last one wins, as _find_live_entry reads it.
            if meta.get("correction-of"):
                for spelling in source_id_variants(meta["correction-of"]):
                    self.corrected_by[spelling] = entry

    def live(self, source_id: str) -> Entry:
        current = source_id
        for _ in range(_MAX_CHAIN):
            spellings = source_id_variants(current)
            if not spellings & self.reversed:
                found = [self.by_source[s] for s in sorted(spellings) if s in self.by_source]
                if not found:
                    raise ReclassifyError("unknown_entry", f"No posted entry has source ID {source_id!r}.",
                                          "Use the source ID of a posted entry.", [source_id])
                return found[-1]
            successor = next((self.corrected_by[s] for s in sorted(spellings) if s in self.corrected_by), None)
            if successor is None or not successor.source_id:
                raise ReclassifyError("entry_reversed", f"The entry {source_id!r} was reversed and has no replacement.",
                                      "Leave it out of the request.", [source_id])
            current = successor.source_id
        raise ReclassifyError("entry_reversed", f"The correction lineage of {source_id!r} is too long to follow.",
                              "Leave it out of the request.", [source_id])


def _period_state(config: dict[str, Any]) -> tuple[frozenset[str], str | None]:
    """The locked months and the closed-through date of entity.json, validated.

    A value that is not canonical (``YYYY-MM`` for a month, ``YYYY-MM-DD`` for the date) refuses the
    request, because a malformed lock must never let a change into a locked month.
    """
    closed = config.get("closed_periods", [])
    through = config.get("closed_through")
    if not isinstance(closed, list) or any(
            not isinstance(month, str) or len(month) != 7 or not _canonical(f"{month}-01") for month in closed):
        raise ReclassifyError("invalid_period_state", "entity.json closed_periods must be a list of YYYY-MM months.",
                              "A person must correct entity.json.")
    if through is not None and (not isinstance(through, str) or not _canonical(through)):
        raise ReclassifyError("invalid_period_state", "entity.json closed_through must be a YYYY-MM-DD date.",
                              "A person must correct entity.json.")
    return frozenset(closed), through


def _canonical(value: str) -> bool:
    from datetime import date as _date
    try:
        return _date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _lock_reason(closed: frozenset[str], through: str | None, periods: dict[str, Any], day: str) -> str | None:
    if day[:7] in closed:
        return "closed_periods"
    if through is not None and day <= through:
        return "closed_through"
    if periods.get(day[:7]) in _LOCKED_STATUSES:
        return "period_closed"
    return None


def _new_source_id(live_source_id: str, from_account: str, to_account: str) -> str:
    digest = hashlib.sha256(f"{live_source_id}\n{from_account}\n{to_account}".encode("utf-8")).hexdigest()
    return f"reclassify:{digest[:24]}"


def preview(entity: Any, from_account: str, to_account: str, source_ids: Sequence[str]) -> dict[str, Any]:
    """Preview moving the entries. Writes nothing."""
    from ..accrual.intake import canonical_hash
    from ..accrual.store import list_records
    from ..entity import load_entity
    from .corrections import MANAGED_ENTRY_MESSAGE, is_managed_entry, managed_source_ids
    from .importer import check_integrity
    from .store import LedgerStore, default_store_path
    entity = load_entity(entity.path)
    for name, account in (("--from-account", from_account), ("--to-account", to_account)):
        if account.split(":")[0] not in CATEGORY_ROOTS or ":" not in account:
            raise ReclassifyError("not_category_account", f"{name} must be an income or expense account: {account!r}.",
                                  "Use ledger reclassify-bank or ledger correct for other accounts.")
    if from_account == to_account:
        raise ReclassifyError("same_account", "--from-account and --to-account are the same.")
    source_ids = list(source_ids)
    if not source_ids:
        raise ReclassifyError("no_entries", "Give at least one --source-id.")
    if len(source_ids) > MAX_ENTRIES:
        raise ReclassifyError("too_many_entries", f"Give at most {MAX_ENTRIES} entries in one request.")
    if check_integrity(entity).status != "ok":
        raise ReclassifyError("ledger_integrity", "Cannot move entries while ledger integrity is invalid.",
                              "Run the integrity check and fix the reported problem first.")
    closed, through = _period_state(entity.entity_config)
    store = LedgerStore(default_store_path(entity.path))
    if not store.path.exists():
        raise ReclassifyError("no_ledger", "The company has no ledger.", "Run books entity init first.")
    opens = {opened.account: opened for opened in store.load_opens()}
    if to_account not in opens:
        raise ReclassifyError("unknown_account", f"The account {to_account} is not in the account catalog.",
                              f"Add it first: books entity account-add PATH --account {to_account} --open-date YYYY-MM-DD")
    ledger = _Ledger(store.load_entries())
    owned = managed_source_ids(store)
    periods = {row.get("id"): row.get("status") for row in list_records(store, "periods")}
    config = entity.entity_config
    moves: list[dict[str, Any]] = []
    already: list[dict[str, str]] = []
    seen: dict[str, str] = {}
    locked: list[tuple[str, str]] = []
    too_early: list[tuple[str, str]] = []
    for source_id in source_ids:
        if not isinstance(source_id, str) or not source_id:
            raise ReclassifyError("unknown_entry", "A source ID must not be empty.")
        entry = ledger.live(source_id)
        live_id = entry.source_id or ""
        if live_id in seen:
            raise ReclassifyError("repeated_entry", f"{source_id!r} and {seen[live_id]!r} name the same entry.",
                                  "Give each entry one time.", [seen[live_id], source_id])
        seen[live_id] = source_id
        accounts = {posting.account for posting in entry.postings}
        if from_account not in accounts:
            if to_account in accounts:
                already.append({"source_id": source_id, "entry_source_id": live_id})
                continue
            raise ReclassifyError("not_in_from_account", f"The entry {source_id!r} does not post to {from_account}.",
                                  "Leave it out of the request or name the account it posts to.", [source_id])
        if is_managed_entry(entry, live_id, owned):
            raise ReclassifyError("managed_accrual_entry", f"{source_id!r}: {MANAGED_ENTRY_MESSAGE}",
                                  "Use the accrual correction route for this entry.", [source_id])
        day = entry.date.isoformat()
        reason = _lock_reason(closed, through, periods, day)
        if reason:
            locked.append((source_id, day[:7]))
            continue
        if opens[to_account].date > entry.date:
            too_early.append((source_id, day))
            continue
        amount = sum((posting.amount for posting in entry.postings if posting.account == from_account), Decimal("0"))
        moves.append({"source_id": source_id, "entry_source_id": live_id,
                      "new_source_id": _new_source_id(live_id, from_account, to_account),
                      "date": day, "payee": entry.payee, "narration": entry.narration,
                      "amount": str(amount.quantize(Decimal("0.01"))), "tags": list(entry.tags)})
    if locked:
        months = sorted({month for _, month in locked})
        raise ReclassifyError(
            "period_locked",
            f"{len(locked)} of the entries are in a locked month ({', '.join(months)}). Nothing was moved.",
            "Leave these entries out of the request. A change in a locked month is a question for the accountant.",
            [source_id for source_id, _ in locked])
    if too_early:
        earliest = min(day for _, day in too_early)
        raise ReclassifyError(
            "account_not_open",
            f"{to_account} opens on {opens[to_account].date.isoformat()}, after {len(too_early)} of the entries.",
            f"Open it earlier first: books entity account-add PATH --account {to_account} --open-date {earliest}",
            [source_id for source_id, _ in too_early])
    for move in moves:
        if store.source_exists(move["new_source_id"]):
            raise ReclassifyError("source_id_conflict", f"The source ID {move['new_source_id']} is already in use.",
                                  "Report this problem.", [move["source_id"]])
    request = {"from_account": from_account, "to_account": to_account, "source_ids": source_ids}
    body: dict[str, Any] = {
        "schema_version": 1, "kind": KIND, "request": request, "changes": bool(moves),
        "ledger_digest": store.content_digest(), "configuration_hash": canonical_hash(config),
        "moves": moves, "already_moved": already,
        "effects": {"entries_moved": len(moves), "entries_written": 2 * len(moves), "accounts_opened": 0,
                    "kept": ["date", "payee", "narration", "flag", "tags", "links", "meta"],
                    "period_locks_changed": 0}}
    body["request_hash"] = canonical_hash({"kind": KIND, "request": request})
    body["plan_hash"] = canonical_hash(body)
    return body


def _entries_for(entry: Entry, move: dict[str, Any], from_account: str, to_account: str, session: str) -> list[Entry]:
    live_id = move["entry_source_id"]
    kept_meta = [(key, value) for key, value in entry.meta if key not in ("source-id", "import-session")]
    reversal = Entry(
        date=entry.date, narration=f"Reversal of: {entry.narration}", payee=entry.payee, flag="*",
        tags=entry.tags, links=entry.links,
        meta=tuple(kept_meta) + (("reverses", live_id), ("import-session", session)),
        postings=tuple(Posting(account=p.account, amount=-p.amount, currency=p.currency, meta=p.meta)
                       for p in entry.postings),
    )
    replacement_meta = {key: value for key, value in kept_meta if key != "correction-of"}
    replacement_meta.update({"source-id": move["new_source_id"], "correction-of": live_id, "import-session": session})
    replacement = Entry(
        date=entry.date, narration=entry.narration, payee=entry.payee, flag=entry.flag,
        tags=entry.tags, links=entry.links, meta=tuple(sorted(replacement_meta.items())),
        postings=tuple(Posting(to_account, p.amount, p.currency, p.meta) if p.account == from_account else p
                       for p in entry.postings),
    )
    return [reversal, replacement]


def apply(entity: Any, from_account: str, to_account: str, source_ids: Sequence[str],
          plan_hash: str | None = None, session: str = DEFAULT_SESSION) -> dict[str, Any]:
    """Move the entries of the preview in one ledger transaction. A repeated request returns already_applied."""
    from ..entity import load_entity
    from .importer import _atomic_ledger_write_unlocked, _ensure_ledger_store, _entity_write_lock
    with _entity_write_lock(entity.path):
        fresh = preview(entity, from_account, to_account, source_ids)
        if not fresh["changes"]:
            # Every entry is already in the target account: an exact retry or a no-op. Resolve it before the
            # plan hash check, which guards writes only.
            return {"status": "already_applied", "kind": KIND, "already_moved": fresh["already_moved"],
                    "request_hash": fresh["request_hash"]}
        if plan_hash is not None and plan_hash != fresh["plan_hash"]:
            raise ReclassifyError("stale_preview", "The company changed after the preview.", "Run the preview again.")
        entity = load_entity(entity.path)
        ledger = _Ledger(_ensure_ledger_store(entity).load_entries())
        written: list[Entry] = []
        for move in fresh["moves"]:
            written += _entries_for(ledger.live(move["entry_source_id"]), move, from_account, to_account, session)
        _atomic_ledger_write_unlocked(
            entity=entity, new_opens=[], new_entries=written, session_id=session, ts=None,
            intent_description=(f"reclassify {len(fresh['moves'])} entries from {from_account} to {to_account} "
                                f"plan_hash={fresh['plan_hash']}"),
            expected_digest=fresh["ledger_digest"],
        )
        return {"status": "applied", "kind": KIND,
                "moved": [{"source_id": m["source_id"], "entry_source_id": m["entry_source_id"],
                           "new_source_id": m["new_source_id"]} for m in fresh["moves"]],
                "already_moved": fresh["already_moved"], "entries_written": len(written),
                "request_hash": fresh["request_hash"], "plan_hash": fresh["plan_hash"]}


def run(args: argparse.Namespace) -> int:
    from ..entity import load_entity
    try:
        entity = load_entity(args.entity)
        if args.apply:
            result = apply(entity, args.from_account, args.to_account, args.source_ids, args.plan_hash, args.session)
        else:
            result = preview(entity, args.from_account, args.to_account, args.source_ids)
    except (FileNotFoundError, ValueError) as exc:
        result = exc.as_dict() if hasattr(exc, "as_dict") else {
            "status": "blocked", "errors": [{"code": "invalid_operation", "message": str(exc)}]}
        print(json.dumps(result, sort_keys=True, indent=2))
        return 1
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0
