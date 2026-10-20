"""Month-end proof from a bank feed: when the feed balance stands in for a statement.

Everything here is read-only and deterministic. It never writes a reconciliation
record: it says, for each checking or savings account and each finished month,
whether the bank feed proves the month-end balance, and gives the exact figures
for ``books reconcile``. A host stores the balance readings as evidence and runs
that command with them.

The rule
--------
A bank feed reports a balance on a day ``D`` and the transactions of each day.
For one ledger account and one month (``start`` to ``end``), the month is
proven when every condition is true:

1. Exactly one feed account maps to the ledger account, the ledger account is
   an ``Assets:`` account, and the feed says the account is checking or
   savings. A card, loan, treasury or other account is never proven here.
2. The month is over: ``end`` is before the day of the check.
3. A balance reading exists for a day ``D`` on or after ``end`` and on or
   before the day of the check. The reading is not marked partial, and when it
   was read the feed already had every transaction through ``D``
   (``data_through`` on or after ``D``), so the balance holds no pending item.
   It needs a full read time with a time zone.
4. Feed windows (``feed_sync`` coverage records) cover every day from
   ``start`` through ``D`` with no gap. A feed that started in the month, or
   a missing window, leaves the month unproven.
5. No bank row for the account waits in review on or before ``D``.
6. The books balance through ``D`` equals the bank balance on ``D``.

The earliest reading that meets conditions 4 to 6 is used. When no reading
does, the earliest reading after the month end gives the reason.

Then the bank balance at month end is the balance on ``D`` less the posted
activity after ``end`` through ``D``. Because the feed is complete for every day
from ``start`` through ``D``, and nothing from it waits, that activity is the
same activity the bank posted. The opening balance is the month-end balance
less the activity of the month. Both come from the same feed, so a missing,
extra or wrong transaction anywhere from ``start`` through ``D`` makes condition
6 fail and the month stays unproven.

A month that already has a clean reconciliation record that still agrees with
the books is ``already_reconciled``. A month with any other record that a
statement or a person made is left alone (``existing_record``). A record that
an earlier feed proof made is checked again.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .tieout import _dec, _fmt, load_postings, load_waiting

DEPOSITORY_TYPES = frozenset({"checking", "savings"})
FEED_EVIDENCE_PREFIX = "evidence:feed-balance-"
MAX_MONTHS = 36
REASONS = (
    "proven", "already_reconciled", "existing_record", "unmapped", "shared_ledger_account",
    "not_checking_or_savings", "no_balance_after_month_end", "feed_incomplete", "waiting_in_review",
    "books_differ",
)
_TYPE_CLEAN = re.compile(r"[_\- ]")
_ZERO = Decimal("0.00")


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _timestamp(value: Any) -> str | None:
    """A full ISO time with a time zone, or None."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


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


def _readings(raw: Any) -> list[dict[str, Any]]:
    """Valid balance readings, earliest day first."""
    readings: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping) or item.get("partial") in (True, 1):
            continue
        balance = _dec(item.get("balance"))
        retrieved = _timestamp(item.get("retrieved_at"))
        on = _day(item.get("as_of")) or _day(retrieved)
        through = _day(item.get("data_through"))
        if balance is None or retrieved is None or on is None or through is None or through < on:
            continue
        if _day(retrieved) < on:
            continue
        readings.append({"balance": balance, "day": on, "as_of": item.get("as_of") or None,
                         "retrieved_at": retrieved, "data_through": through})
    readings.sort(key=lambda reading: (reading["day"], reading["retrieved_at"]))
    return readings


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

    ``accounts`` is a list of ``{account_id, account_type, readings: [{balance,
    as_of, retrieved_at, data_through, partial}]}``. ``balance`` is what the bank
    holds (a checking or savings account has one sign convention).
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
    postings = load_postings(entity.path, ledgers)
    waiting = load_waiting(entity, ledgers)
    for account_id, ledger, item in candidates:
        windows = _merged([(r.period_start, r.period_end) for r in records
                           if r.account == ledger and r.kind == "feed_sync"])
        if not windows:
            rows.append(_row(account_id, ledger, None, "feed_incomplete"))
            continue
        readings = [reading for reading in _readings(item.get("readings")) if reading["day"] <= as_of]
        start_from = max(first_month or windows[0][0].replace(day=1), date(1900, 1, 1))
        for month, start, end in _months(start_from, as_of - timedelta(days=1)):
            rows.append(_month(account_id, ledger, month, start, end, readings, windows,
                               postings[ledger], waiting[ledger], _existing(reconciliations, ledger, end)))
    rows.sort(key=lambda row: (str(row["ledger_account"] or ""), row["account_id"], str(row["month"] or "")))
    return {"status": "ok", "as_of": as_of.isoformat(), "months": rows}


def _row(account_id: str, ledger: str | None, month: str | None, reason: str, **fields: Any) -> dict[str, Any]:
    return {"account_id": account_id, "ledger_account": ledger, "month": month,
            "status": "proven" if reason == "proven" else "not_proven", "reason": reason,
            "reconcile": None, "reading": None, **fields}


def _shown(reading: Mapping[str, Any]) -> dict[str, Any]:
    return {"balance": _fmt(reading["balance"]), "as_of": reading["as_of"], "day": reading["day"].isoformat(),
            "retrieved_at": reading["retrieved_at"], "data_through": reading["data_through"].isoformat()}


def _month(account_id: str, ledger: str, month: str, start: date, end: date, readings: list[dict[str, Any]],
           windows: list[tuple[date, date]], postings: Any, waiting: Any,
           existing: Mapping[str, Any] | None) -> dict[str, Any]:
    books_end = postings.through(end)
    if existing is not None:
        ledger_balance = _dec(existing.get("ledger_balance"))
        if existing.get("status") == "clean" and ledger_balance == books_end:
            return _row(account_id, ledger, month, "already_reconciled")
        if not _from_feed(existing):
            return _row(account_id, ledger, month, "existing_record")
    after_end = [r for r in readings if r["day"] >= end]
    if not after_end:
        return _row(account_id, ledger, month, "no_balance_after_month_end")
    first: dict[str, Any] | None = None
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
            break
    else:
        reading = first["reading"]
        if first["reason"] != "books_differ":
            return _row(account_id, ledger, month, first["reason"])
        books_on_day = postings.through(reading["day"])
        return _row(account_id, ledger, month, "books_differ", reading=_shown(reading),
                    books_balance=_fmt(books_on_day), difference=_fmt(reading["balance"] - books_on_day))
    day = reading["day"]
    shown = _shown(reading)
    books_on_day = postings.through(day)
    after = postings.between(end + timedelta(days=1), day) if day > end else _ZERO
    closing = reading["balance"] - after
    activity = postings.between(start, end)
    opening = closing - activity
    return _row(account_id, ledger, month, "proven", reading=shown, books_balance=_fmt(books_on_day),
                difference=_fmt(_ZERO), reconcile={
                    "account": ledger, "as_of": end.isoformat(), "source_balance": _fmt(closing),
                    "source_opening_balance": _fmt(opening), "source_transaction_total": _fmt(activity),
                    "source_snapshot_at": reading["retrieved_at"], "source_through": end.isoformat()})


# ---------------------------------------------------------------------------
# CLI: books balance prove
# ---------------------------------------------------------------------------

def add_parser(sub: Any) -> None:
    prove_parser = sub.add_parser(
        "prove", help="Say which finished months a bank feed balance proves (read-only)")
    prove_parser.add_argument("--entity", required=True, type=Path)
    prove_parser.add_argument("--input", required=True, type=Path,
                              help='JSON {"accounts": [{"account_id", "account_type", "readings": '
                                   '[{"balance", "as_of", "retrieved_at", "data_through", "partial"}]}]}')
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
