"""Month-end proof from a bank feed: when the feed balance stands in for a statement.

Everything here is read-only and deterministic. It never writes a reconciliation
record: it says, for each checking or savings account and each finished month,
whether the bank feed proves the month-end balance, and gives the exact figures
for ``books reconcile``. A host stores the balance readings as evidence and runs
that command with them.

Input
-----
``{"accounts": [{"account_id", "account_type", "currency"?, "time_zone"?,
"readings": [{"balance", "as_of", "retrieved_at", "data_through", "partial"}]}]}``

- ``balance`` is the current balance the bank reported when it was read (not an
  available balance and not a statement balance), as money held.
- ``retrieved_at`` is a full ISO time with an offset. ``as_of`` is optional: a
  date, or a full ISO time with an offset. ``data_through`` is a date.
  ``partial`` must be the JSON value ``false``. Any other value, or no value,
  means the read was incomplete, and the reading is not used.
- ``currency`` is optional. When it is given it must equal the currency of the
  ledger account.
- ``time_zone`` is optional, an IANA name such as ``America/New_York`` for the
  bank. It is the reference time zone for every day of that account's readings.
  Without it, each time is read in its own offset (``Z`` is UTC).

The rule
--------
For one ledger account and one month (``start`` to ``end``), the month is
proven when every condition is true:

1. Exactly one feed account maps to the ledger account, the ledger account is
   an ``Assets:`` account, and the feed says the account is checking or
   savings. A card, loan, treasury or other account is never proven here.
2. The month is over: ``end`` is before the day of the check.
3. A usable balance reading exists. Its balance day ``D`` (the ``as_of`` day,
   else the day it was read) is on or after ``end``. The day it was read is
   strictly after ``end``, so the bank day of the month end was over, and on or
   before the day of the check. The reading is not partial, and when it was read
   the feed already had every transaction through ``D`` (``data_through`` on or
   after ``D``). All days use the reference time zone above.
4. Feed windows (``feed_sync`` coverage records) cover every day from
   ``start`` through ``D`` with no gap. A feed that started in the month, or
   a missing window, leaves the month unproven.
5. No bank row for the account waits in review on or before ``D``.
6. The books balance through ``D`` equals the bank balance on ``D``. Only
   postings in the account's currency count. A posting in another currency, or
   a posting whose date or amount cannot be read, leaves the account unproven.
7. The opening balance is proven: it is the closing balance of the month
   before, from a reconciliation record (clean or resolved), a statement record
   that ends on the day before ``start``, or a month this check proved before
   it. The books balance on the day before ``start`` must equal it. With none
   of these (the first month of the feed), the opening is accepted only when it
   is zero in the books and the month has at least one posting from a source.

The earliest reading that meets conditions 4 to 6 is used. A reading whose
balance day is the month end itself is decisive: when it differs, a later
reading does not prove the month. When no reading meets the conditions, the
earliest reading after the month end gives the reason.

Then the bank balance at month end is the balance on ``D`` less the posted
activity after ``end`` through ``D``, and the activity of the month is that
balance less the proven opening. A missing, extra or wrong transaction from
``start`` through ``D`` makes condition 6 or 7 fail.

The limit that remains: a transaction that the books date on the other side of
the month end than the bank does is not found when the bank and the books still
agree on the later reading day and no reading falls on the month end itself.
The month end is then off by that transaction, and the next month's opening
is off by the same amount.

A month that already has a clean reconciliation record that still agrees with
the books is ``already_reconciled``. A month with any other record that a
statement or a person made is left alone (``existing_record``). A record that
an earlier feed proof made is checked again.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, tzinfo
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping, Sequence

from .ledger.store import LedgerStore, default_store_path
from .tieout import Series, _dec, _fmt, load_waiting

DEPOSITORY_TYPES = frozenset({"checking", "savings"})
FEED_EVIDENCE_PREFIX = "evidence:feed-balance-"
MAX_MONTHS = 36
REASONS = (
    "proven", "already_reconciled", "existing_record", "unmapped", "shared_ledger_account",
    "not_checking_or_savings", "unknown_time_zone", "currency_mismatch", "other_currency",
    "unreadable_postings", "no_balance_after_month_end", "feed_incomplete", "waiting_in_review",
    "books_differ", "opening_unproven", "opening_differs",
)
_TYPE_CLEAN = re.compile(r"[_\- ]")
_CURRENCY = re.compile(r"[A-Z]{3}")
_ZERO = Decimal("0.00")
_CENT = Decimal("0.01")


def _instant(value: Any) -> datetime | None:
    """A full ISO time with an offset, or None."""
    if not isinstance(value, str) or len(value) > 64 or len(value) <= 10:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _day_only(value: Any) -> date | None:
    """A plain YYYY-MM-DD date, or None."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _local_day(moment: datetime, zone: tzinfo | None) -> date:
    return (moment.astimezone(zone) if zone is not None else moment).date()


def _zone(value: Any) -> tuple[tzinfo | None, bool]:
    """The reference time zone, and whether the value was usable (no value is usable)."""
    if value in (None, ""):
        return None, True
    if not isinstance(value, str) or len(value) > 64:
        return None, False
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(value), True
    except Exception:  # noqa: BLE001 - an unknown or unloadable zone is not usable
        return None, False


def _account_type(value: Any) -> str:
    return _TYPE_CLEAN.sub("", str(value or "").lower())


def _month_end(year: int, month: int) -> date:
    return (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))


def _months(first: date, last_end: date) -> list[tuple[str, date, date]]:
    result: list[tuple[str, date, date]] = []
    year, month = first.year, first.month
    while True:
        start, end = date(year, month, 1), _month_end(year, month)
        if end > last_end:
            break
        result.append((f"{year:04d}-{month:02d}", start, end))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return result[-MAX_MONTHS:]


def _merged(intervals: Sequence[tuple[date, date]]) -> list[tuple[date, date]]:
    merged: list[tuple[date, date]] = []
    for first, last in sorted(intervals):
        if merged and first <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return merged


def _covers(intervals: list[tuple[date, date]], start: date, end: date) -> bool:
    return any(first <= start and end <= last for first, last in intervals)


def _readings(raw: Any, zone: tzinfo | None) -> list[dict[str, Any]]:
    """Usable balance readings, earliest balance day first."""
    readings: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping) or item.get("partial") is not False:
            continue
        balance = _dec(item.get("balance"))
        retrieved = _instant(item.get("retrieved_at"))
        through = _day_only(item.get("data_through"))
        if balance is None or retrieved is None or through is None:
            continue
        read_day = _local_day(retrieved, zone)
        raw_as_of = item.get("as_of")
        if raw_as_of in (None, ""):
            on = read_day
        else:
            moment = _instant(raw_as_of)
            on = _local_day(moment, zone) if moment is not None else _day_only(raw_as_of)
            if on is None:
                continue
        if on > read_day or through < on:
            continue
        readings.append({"balance": balance, "day": on, "read_day": read_day, "as_of": raw_as_of or None,
                         "retrieved_at": item.get("retrieved_at"), "instant": retrieved, "data_through": through})
    readings.sort(key=lambda reading: (reading["day"], reading["instant"]))
    return readings


class _Books:
    """The books side of one ledger account: postings in its currency, and what could not be used."""

    def __init__(self) -> None:
        self.items: list[tuple[date, Decimal]] = []
        self.sourced: list[date] = []
        self.unreadable = False
        self.other_currency = False
        self.currency: str | None = None
        self.series = Series()


def _load_books(entity_path: Path, ledgers: Sequence[str]) -> dict[str, _Books]:
    books = {ledger: _Books() for ledger in ledgers}
    store_path = default_store_path(entity_path)
    if not ledgers or not store_path.is_file():
        return books
    marks = ",".join("?" for _ in ledgers)
    try:
        with LedgerStore(store_path).connection() as conn:
            for name, currency in conn.execute(f"SELECT name, currency FROM accounts WHERE name IN ({marks})", list(ledgers)):
                books[str(name)].currency = str(currency or "") or None
            rows = conn.execute(
                f"SELECT p.account, e.date, p.amount, p.currency, e.source_id FROM postings p "
                f"JOIN entries e ON e.id = p.entry_id WHERE p.account IN ({marks})", list(ledgers)).fetchall()
    except sqlite3.DatabaseError:
        for item in books.values():
            item.unreadable = True
        return books
    for account, raw_day, raw_amount, currency, source_id in rows:
        item = books[str(account)]
        if str(currency or "") != (item.currency or "USD"):
            item.other_currency = True
            continue
        day = _day_only(str(raw_day or ""))
        try:
            amount = Decimal(str(raw_amount))
            readable = amount.is_finite()
        except (InvalidOperation, ValueError):
            readable = False
        if day is None or not readable:
            item.unreadable = True
            continue
        item.items.append((day, amount.quantize(_CENT)))
        if source_id:
            item.sourced.append(day)
    for item in books.values():
        item.series = Series(item.items)
    return books


def _existing(records: Any, account: str, end: date) -> Mapping[str, Any] | None:
    for record in records if isinstance(records, list) else []:
        if isinstance(record, Mapping) and record.get("account") == account and record.get("as_of") == end.isoformat():
            return record
    return None


def _from_feed(record: Mapping[str, Any]) -> bool:
    refs = record.get("source_evidence_refs")
    return isinstance(refs, list) and bool(refs) and all(
        isinstance(ref, str) and ref.startswith(FEED_EVIDENCE_PREFIX) for ref in refs)


def prove(entity_path: Path | str, accounts: Sequence[Mapping[str, Any]], *, as_of: date,
          from_month: str | None = None) -> dict[str, Any]:
    """Say, per feed account and finished month, whether the feed proves the month end.

    See the module documentation for the input shape and the rule.
    """
    from .coverage import load_statements
    from .entity import flat_bank_account_mappings, load_entity

    entity = load_entity(entity_path)
    mappings = flat_bank_account_mappings(entity.entity_config)
    by_ledger: dict[str, list[str]] = {}
    for feed_id, ledger in mappings.items():
        by_ledger.setdefault(ledger, []).append(feed_id)
    records, _ = load_statements(entity.path)
    try:
        reconciliations = json.loads((entity.path / "reports" / "reconciliation.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        reconciliations = []
    first_month = None
    if from_month:
        if not re.fullmatch(r"\d{4}-\d{2}", from_month) or not 1 <= int(from_month[5:]) <= 12:
            raise ValueError("--from must use YYYY-MM.")
        first_month = date(int(from_month[:4]), int(from_month[5:]), 1)

    rows: list[dict[str, Any]] = []
    candidates: list[tuple[str, str, Mapping[str, Any]]] = []
    for item in accounts:
        if not isinstance(item, Mapping):
            continue
        account_id = str(item.get("account_id") or "")
        ledger = mappings.get(account_id)
        if not ledger:
            rows.append(_row(account_id, None, None, "unmapped"))
            continue
        if not ledger.startswith("Assets:") or _account_type(item.get("account_type")) not in DEPOSITORY_TYPES:
            rows.append(_row(account_id, ledger, None, "not_checking_or_savings"))
            continue
        if len(by_ledger.get(ledger, [])) != 1:
            rows.append(_row(account_id, ledger, None, "shared_ledger_account"))
            continue
        candidates.append((account_id, ledger, item))

    ledgers = sorted({ledger for _, ledger, _ in candidates})
    books = _load_books(entity.path, ledgers)
    waiting = load_waiting(entity, ledgers)
    for account_id, ledger, item in candidates:
        side = books[ledger]
        zone, zone_ok = _zone(item.get("time_zone"))
        currency = item.get("currency")
        if not zone_ok:
            rows.append(_row(account_id, ledger, None, "unknown_time_zone"))
            continue
        if currency not in (None, "") and (not isinstance(currency, str) or not _CURRENCY.fullmatch(currency)
                                           or currency != (side.currency or "USD")):
            rows.append(_row(account_id, ledger, None, "currency_mismatch"))
            continue
        if side.other_currency:
            rows.append(_row(account_id, ledger, None, "other_currency"))
            continue
        if side.unreadable:
            rows.append(_row(account_id, ledger, None, "unreadable_postings"))
            continue
        windows = _merged([(r.period_start, r.period_end) for r in records
                           if r.account == ledger and r.kind == "feed_sync"])
        if not windows:
            rows.append(_row(account_id, ledger, None, "feed_incomplete"))
            continue
        statement_closings = {r.period_end: r.closing_balance for r in records
                              if r.account == ledger and r.kind == "statement" and r.closing_balance is not None}
        readings = [reading for reading in _readings(item.get("readings"), zone) if reading["read_day"] <= as_of]
        start_from = max(first_month or windows[0][0].replace(day=1), date(1900, 1, 1))
        proven_closings: dict[date, Decimal] = {}
        for month, start, end in _months(start_from, as_of - timedelta(days=1)):
            row = _month(account_id, ledger, month, start, end, readings, windows, side, waiting[ledger],
                         reconciliations, statement_closings, proven_closings)
            if row["status"] == "proven":
                proven_closings[end] = Decimal(row["reconcile"]["source_balance"])
            rows.append(row)
    rows.sort(key=lambda row: (str(row["ledger_account"] or ""), row["account_id"], str(row["month"] or "")))
    return {"status": "ok", "as_of": as_of.isoformat(), "months": rows}


def _row(account_id: str, ledger: str | None, month: str | None, reason: str, **fields: Any) -> dict[str, Any]:
    return {"account_id": account_id, "ledger_account": ledger, "month": month,
            "status": "proven" if reason == "proven" else "not_proven", "reason": reason,
            "reconcile": None, "reading": None, **fields}


def _shown(reading: Mapping[str, Any]) -> dict[str, Any]:
    return {"balance": _fmt(reading["balance"]), "as_of": reading["as_of"], "day": reading["day"].isoformat(),
            "read_day": reading["read_day"].isoformat(), "retrieved_at": reading["retrieved_at"],
            "data_through": reading["data_through"].isoformat()}


def _opening(ledger: str, start: date, end: date, side: _Books, reconciliations: Any,
             statement_closings: Mapping[date, Decimal],
             proven_closings: Mapping[date, Decimal]) -> tuple[Decimal | None, str]:
    """The proven bank opening of a month, or None with the reason."""
    before = start - timedelta(days=1)
    if before in proven_closings:
        return proven_closings[before], ""
    prior = _existing(reconciliations, ledger, before)
    if prior is not None:
        balance = _dec(prior.get("source_balance"))
        if prior.get("status") in ("clean", "resolved") and balance is not None:
            return balance, ""
        return None, "opening_unproven"
    if before in statement_closings:
        return statement_closings[before], ""
    sourced_in_month = any(start <= day <= end for day in side.sourced)
    if side.series.through(before) == _ZERO and sourced_in_month:
        return _ZERO, ""
    return None, "opening_unproven"


def _month(account_id: str, ledger: str, month: str, start: date, end: date, readings: list[dict[str, Any]],
           windows: list[tuple[date, date]], side: _Books, waiting: Any, reconciliations: Any,
           statement_closings: Mapping[date, Decimal], proven_closings: Mapping[date, Decimal]) -> dict[str, Any]:
    postings = side.series
    existing = _existing(reconciliations, ledger, end)
    if existing is not None:
        ledger_balance = _dec(existing.get("ledger_balance"))
        if existing.get("status") == "clean" and ledger_balance == postings.through(end):
            return _row(account_id, ledger, month, "already_reconciled")
        if not _from_feed(existing):
            return _row(account_id, ledger, month, "existing_record")
    after_end = [r for r in readings if r["day"] >= end and r["read_day"] > end]
    if not after_end:
        return _row(account_id, ledger, month, "no_balance_after_month_end")
    first: dict[str, Any] | None = None
    chosen: dict[str, Any] | None = None
    for reading in after_end:
        day = reading["day"]
        if not _covers(windows, start, day):
            outcome = "feed_incomplete"
        elif waiting.count_between(None, day):
            outcome = "waiting_in_review"
        elif postings.through(day) != reading["balance"]:
            outcome = "books_differ"
        else:
            outcome = "proven"
        if first is None:
            first = {"reason": outcome, "reading": reading}
        if outcome == "proven":
            chosen = reading
            break
        if day == end and outcome == "books_differ":
            # A reading for the month end itself decides the month.
            first = {"reason": outcome, "reading": reading}
            break
    if chosen is None:
        reading = first["reading"]
        if first["reason"] != "books_differ":
            return _row(account_id, ledger, month, first["reason"])
        books_on_day = postings.through(reading["day"])
        return _row(account_id, ledger, month, "books_differ", reading=_shown(reading),
                    books_balance=_fmt(books_on_day), difference=_fmt(reading["balance"] - books_on_day))
    reading = chosen
    day = reading["day"]
    shown = _shown(reading)
    opening, reason = _opening(ledger, start, end, side, reconciliations, statement_closings, proven_closings)
    if opening is None:
        return _row(account_id, ledger, month, reason, reading=shown)
    books_opening = postings.through(start - timedelta(days=1))
    if opening != books_opening:
        return _row(account_id, ledger, month, "opening_differs", reading=shown,
                    opening_balance=_fmt(opening), books_opening=_fmt(books_opening),
                    difference=_fmt(opening - books_opening))
    after = postings.between(end + timedelta(days=1), day) if day > end else _ZERO
    closing = reading["balance"] - after
    return _row(account_id, ledger, month, "proven", reading=shown, books_balance=_fmt(postings.through(day)),
                difference=_fmt(_ZERO), reconcile={
                    "account": ledger, "as_of": end.isoformat(), "source_balance": _fmt(closing),
                    "source_opening_balance": _fmt(opening), "source_transaction_total": _fmt(closing - opening),
                    "source_snapshot_at": reading["retrieved_at"], "source_through": end.isoformat()})


# ---------------------------------------------------------------------------
# CLI: books balance prove
# ---------------------------------------------------------------------------

def add_parser(sub: Any) -> None:
    prove_parser = sub.add_parser(
        "prove", help="Say which finished months a bank feed balance proves (read-only)")
    prove_parser.add_argument("--entity", required=True, type=Path)
    prove_parser.add_argument("--input", required=True, type=Path,
                              help='JSON {"accounts": [{"account_id", "account_type", "currency", "time_zone", '
                                   '"readings": [{"balance", "as_of", "retrieved_at", "data_through", "partial"}]}]}')
    prove_parser.add_argument("--as-of", required=True, dest="as_of",
                              help="The day of the check (YYYY-MM-DD). Only months that end before it are checked.")
    prove_parser.add_argument("--from", default=None, dest="from_month", help="First month to check (YYYY-MM)")


def run(args: Any) -> int:
    import sys

    try:
        as_of = date.fromisoformat(args.as_of)
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
        items = payload.get("accounts") if isinstance(payload, dict) else payload
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ValueError("The input must be a JSON list of objects.")
        document = prove(args.entity, items, as_of=as_of, from_month=args.from_month)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0
