"""Month-end proof from a bank feed: when the feed balance stands in for a statement.

Everything here is read-only and deterministic. It never writes a reconciliation
record: it says, for each checking or savings account and each finished month,
whether the bank feed proves the month-end balance, and gives the exact figures
for ``books reconcile``. A host stores the balance readings as evidence and runs
that command with them.

Input
-----
``{"accounts": [{"account_id", "account_type", "time_zone", "currency"?,
"readings": [{"balance", "as_of", "retrieved_at", "data_through", "partial"}]}]}``

- ``balance`` is the current balance the bank reported when it was read (not an
  available balance and not a statement balance), as money held.
- ``retrieved_at`` is a full ISO time with an offset. ``as_of`` is optional: a
  date, or a full ISO time with an offset that is not later than
  ``retrieved_at``. ``data_through`` is a date.
  ``partial`` must be the JSON value ``false``. Any other value, or no value,
  means the read was incomplete, and the reading is not used.
- ``currency`` is optional. When it is given it must equal the currency of the
  ledger account.
- ``time_zone`` is required: an IANA name such as ``America/New_York`` for the
  bank. Every day of that account's readings is a day in this time zone. An
  account without it is ``time_zone_required``; an unknown name is
  ``unknown_time_zone``.

The rule
--------
For one ledger account and one month (``start`` to ``end``), the month is
proven when every condition is true:

1. Exactly one feed account maps to the ledger account, the ledger account is
   an ``Assets:`` account, and the feed says the account is checking or
   savings. A card, loan, treasury or other account is never proven here.
2. The month is over: ``end`` is before the day of the check.
3. A usable balance reading exists. Its balance day ``D`` (the ``as_of`` day,
   else the day it was read) is on or after ``end``. It was read at least
   ``SAFETY_MARGIN_HOURS`` (6) hours after the bank's midnight that starts the
   day after ``end``, so the bank day of the month end was over, and on or
   before the day of the check. The reading is not partial, and when it was read
   the feed already had every transaction through ``D`` (``data_through`` on or
   after ``D``). All days are days in the bank time zone.
4. Feed windows (``feed_sync`` coverage records) cover every day from
   ``start`` through ``D`` with no gap. A feed that started in the month, or
   a missing window, leaves the month unproven.
5. No bank row for the account waits in review on or before ``D``. When the
   review file does not parse, or a row of the account (or a row whose account
   cannot be decided) has a date or amount that cannot be read, the account is
   ``waiting_unreadable``.
6. The books balance through ``D`` equals the bank balance on ``D``. Only
   postings in the account's currency count. A posting in another currency, or
   a posting whose date or amount cannot be read, leaves the account unproven.
7. The opening balance comes from a source: it is the closing balance of the month
   before, from a month this check proved before it, else from a reconciliation
   record (clean or resolved) or a statement record that ends on the day before
   ``start``. When a record and a statement both give it, they must agree
   (``opening_conflict``). The books balance on the day before ``start`` must
   equal it. With none of these, the opening is zero only in the first month of
   the feed windows, when nothing in the books is dated before that month, and
   when the month has postings and every one is a row that the bank connection
   delivered (see rule 7b).

7b. When rule 7 has no source and its zero opening does not apply, the opening is
   computed back from the reading: the balance on ``D`` less the books postings
   from ``start`` through ``D``. Let ``W`` be the first day of the first feed
   window, and ``N`` the newest reading whose day the feed windows cover from
   ``W`` with no gap. The computed opening is used only when all of these are true:

   - The feed windows cover every day from ``W`` through ``D``
     (else ``feed_incomplete``).
   - The books hold at most one opening entry: a posting dated before ``W``, or
     one on ``W`` that the bank connection did not deliver (else
     ``opening_entry_unclear``).
   - Every other posting from ``W`` through the later of ``D`` and ``N``'s day
     is a row that the bank connection delivered (else ``unsourced_postings``).
     A row is delivered by the connection when ``books ingest`` wrote it from a
     connected feed: its entry has the ``feed-provider`` mark, which an entry
     gets only from the ingest's ``feed-intake`` audit record of that unchanged
     row, it is the only posting of its source ID on the account, the mark names
     a provider in
     ``source_ids.CONNECTED_FEED_PROVIDERS``, and the source ID is
     ``<that provider>:<id>`` (or has no prefix, with the mark ``banksync``).
     It is not an opening import. A correction (``correction-of``) is judged
     only through what it corrects: it counts when it is the only posting of
     its source ID on the account and keeps the day and amount of a delivered
     row. Any other row is not delivered: a generic ingest, a CSV, statement or
     manual row, a row written before the mark existed, or a correction that
     adds or changes money.
   - At least one such posting from ``W`` through ``D`` exists (else
     ``no_feed_rows``).
   - Nothing waits in review on or before ``N``'s day (else
     ``waiting_in_review``), and the books balance through ``N``'s day equals
     ``N``'s balance (else ``newest_reading_differs``).

   A reversal without a source and the only posting it reverses (``reverses``
   names its source ID) are left out when they fall on one day with opposite
   amounts and either the reversed posting is an opening import, or a
   correction of it keeps the row (same day, same amount). A reversal that
   removes a delivered row without such a replacement is not from the connection.

   The opening that rule 7b computes always equals the books' opening. Each
   ``feed_computed`` row has ``opening_evidence``, derived from the books only
   (``opening_evidence``): ``statement`` when a statement backs the books on or
   before the month end (an opening entry with ``opening-source: printed``, or a
   statement balance that equals the books on its date), else ``derived``. A typed
   opening is ``derived`` whoever typed it.

A month end with a statement record (a closing balance on ``end``) is left to the
statement (``statement_available``): a statement outranks the feed.

The readings after the month end are taken in order (balance day, then the time
it was read). The first one that meets conditions 4 to 6 is the anchor, and its
day is ``D``. Every reading before it, and every other reading for ``D``, must
also equal the books: the first one that does not meet the conditions gives the
reason, and a later reading that agrees does not prove the month
(``later_reading_agrees`` says so). A host that knows a reading is stale must
mark it partial or remove it.

Every dated balance of a statement for the account (its opening balance on the
day before its first day, and its closing balance on its last day), from the day
before ``start`` through ``D``, must equal the books on that day
(``statement_differs``). This includes a statement whose cycle does not end on a
month end.

Then the bank balance at month end is the balance on ``D`` less the posted
activity after ``end`` through ``D``, and the activity of the month is that
balance less the proven opening. A missing, extra or wrong transaction from
``start`` through ``D`` makes condition 6 or 7 fail.

Basis
-----
Each proven row has ``basis``, and its ``reconcile`` figures have the same value as
``proof_basis`` for ``books reconcile --proof-basis``:

- ``feed_reading``: ``D`` is the month end, so the month-end balance is a balance
  the bank reported for that day, and the opening came from a statement, a record
  whose basis is ``statement`` or ``feed_reading``, a month proven here as
  ``feed_reading``, or the zero opening of rule 7.
- ``feed_computed``: every other proven month. The month-end balance was computed
  back from a later reading, or the opening was computed (rule 7b, or a month or
  record whose basis is ``feed_computed`` or not known).

What a proof from the feed shows: the books equal what the bank connection
delivered and reported. It is not evidence from outside the connection. With a
``derived`` opening (typed by anyone, or computed to fit the current balance), a
transaction that the connection never delivered, before or after ``W``, is inside
that opening entry, and no check here finds it. With a ``statement`` opening the
books differ from the bank by that transaction, so it is found.

Limits that remain (a statement finds the first three):

- A transaction that the books date on the other side of the month end than the
  bank does is not found when the bank and the books agree again on the reading
  day and no reading falls on the month end itself. The month end is then off by
  that transaction, and the next month's opening by the same amount.
- Errors that cancel each other inside the checked span (for example a missing
  deposit and a duplicated deposit of the same amount) are found only when a
  month end proven by a statement or by a reading for that day falls between
  them. A month end computed back from a later reading is off by the amount when
  one error falls before it and the other after it. When both fall inside one
  month, every month end is still right, but the transaction detail is not.
- The proof shows balances, not transactions: a transaction posted to the wrong
  category, or two rows swapped between accounts that are not checked, is not
  found.
- The balance used is the current balance the host read from the bank. A bank
  that includes pending items in it makes the reading differ, and the month
  waits (it does not prove a wrong figure).
- Only rows waiting in review (``staging/pending-categorization.json``) count as
  waiting. Rows in ``staging/pending.json`` and held duplicate candidates are not
  checked. A transaction that is really missing from the books still shows as a
  difference between the books and the bank, so it cannot prove a wrong month.
- The readings, ``data_through``, ``partial``, the time zone and the feed windows
  are facts from the host. The engine checks that they agree with each other and
  with the books; it cannot check them against the bank.

A month that already has a clean reconciliation record that still agrees with
the books is ``already_reconciled`` (with the record's ``basis``). A month with
any other record that a statement or a person made is left alone
(``existing_record``). A record that an earlier feed proof made is checked again:
a transaction dated in that month later on makes it differ from the books, and the
month is proven again only when the feed still proves it.

Reasons
-------
A row that is not proven has a ``reason`` code and the figures for one sentence:

- ``feed_incomplete``: ``feed_from`` (the first day the feed covers, or None) and
  ``uncovered_from`` / ``uncovered_to`` (the first days the feed does not cover).
  When ``feed_from`` is after the month start, the feed does not go back to the month.
- ``books_differ``, ``newest_reading_differs`` and ``opening_differs``:
  ``difference`` (bank less books) on ``difference_on``. ``books_differ`` also
  has ``later_reading_agrees``.
- ``not_checking_or_savings``: ``account_type`` (the type the host gave).
- ``unsourced_postings``: ``unsourced_count`` and the first date, ``unsourced_from``.
- ``audit_chain_invalid``: no field. The audit hash chain of the ledger store has an error, so no row counts as
  delivered by the bank connection and no month is proven from the feed.
- ``opening_entry_unclear``: ``opening_entries`` (how many).
- ``opening_conflict``: ``opening_balances`` (the different figures).
- ``statement_available``: ``statement_ids``.
- ``statement_differs``: ``statement_id``, ``statement_date``,
  ``statement_balance``, ``books_balance``, ``difference`` and
  ``period_confirmed`` (False for a statement whose period nobody confirmed).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping, Sequence

from .ledger.store import LedgerStore, default_store_path
from .reconcile import FEED_EVIDENCE_PREFIX, from_feed as _from_feed, record_basis
from .source_ids import CONNECTED_FEED_PROVIDERS, FEED_PROVIDER_META, LEGACY_UNPREFIXED_PROVIDER
from .tieout import Series, _dec, _fmt, load_waiting_checked

__all__ = ["FEED_EVIDENCE_PREFIX", "REASONS", "SAFETY_MARGIN_HOURS", "day_closed_at", "opening_evidence", "prove"]

DEPOSITORY_TYPES = frozenset({"checking", "savings"})
MAX_MONTHS = 36
REASONS = (
    "proven", "already_reconciled", "existing_record", "unmapped", "shared_ledger_account",
    "not_checking_or_savings", "time_zone_required", "unknown_time_zone", "currency_mismatch", "other_currency",
    "unreadable_postings", "waiting_unreadable", "no_balance_after_month_end", "feed_incomplete", "waiting_in_review",
    "books_differ", "opening_unproven", "opening_differs", "opening_conflict", "statement_available",
    "opening_entry_unclear", "unsourced_postings", "no_feed_rows", "newest_reading_differs", "statement_differs",
    "audit_chain_invalid",
)
#: Opening sources whose figure a bank or a statement gave for the day, rather than one computed back.
_READING_GRADE_BASES = frozenset({"statement", "feed_reading"})
#: The ``import-source`` of an entry that states an opening balance (the same set as ``tieout.load_postings``).
#: Rule 7b treats such an entry as an opening entry, also when it has a source ID.
_OPENING_SOURCES = frozenset({"quickbooks-opening", "accrual-opening", "accrual-tracking-opening", "statement-opening"})
_TYPE_CLEAN = re.compile(r"[_\- ]")
_CURRENCY = re.compile(r"[A-Z]{3}")
_ZERO = Decimal("0.00")
#: A reading proves a month only when it was read at least this many hours after the bank's midnight that ends the
#: month. Late postings and a host that reads a little early cannot then turn an open bank day into a closed one.
SAFETY_MARGIN_HOURS = 6
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
    """The bank's time zone, and whether the value was usable. The caller refuses a missing value first."""
    if not isinstance(value, str) or not value or len(value) > 64:
        return None, False
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(value), True
    except Exception:  # noqa: BLE001 - an unknown or unloadable zone is not usable
        return None, False


def day_closed_at(day: date, zone: tzinfo) -> datetime:
    """The first instant at which a reading counts as read after ``day`` at the bank.

    It is ``SAFETY_MARGIN_HOURS`` real hours after the bank's midnight that starts the next day. The midnight is
    turned into UTC first, so a daylight-saving change does not make the margin shorter or longer.
    """
    midnight = datetime.combine(day + timedelta(days=1), time(0), tzinfo=zone).astimezone(timezone.utc)
    return midnight + timedelta(hours=SAFETY_MARGIN_HOURS)


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


def _readings(raw: Any, zone: tzinfo) -> list[dict[str, Any]]:
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
            if moment is not None and moment > retrieved:
                continue  # A balance cannot be as of a time after it was read.
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
        #: Each posting's day and kind: ``source`` (a row that the bank connection delivered, see
        #: ``_classify``), ``opening`` (an entry that an opening import made, with or without a source ID) or ``none``
        #: (anything else), with the entry's ``opening-source``. Reversal pairs that ``_classify`` cancels are left out.
        self.kinds: list[tuple[date, str, str | None]] = []
        self.unreadable = False
        #: The audit hash chain of the ledger store has an error (see ``_load_books``).
        self.chain_invalid = False
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
        store = LedgerStore(store_path)
        # The mark comes from the audit record of the feed ingest. If the audit chain has an error, no record can be
        # trusted, so no row counts as delivered by the bank connection. One check for each proof.
        chain_valid = not store.verify_audit_chain()
        for item in books.values():
            item.chain_invalid = not chain_valid
        with store.connection() as conn:
            for name, currency in conn.execute(f"SELECT name, currency FROM accounts WHERE name IN ({marks})", list(ledgers)):
                books[str(name)].currency = str(currency or "") or None
            rows = conn.execute(
                f"SELECT p.account, e.date, p.amount, p.currency, e.source_id, e.metadata_json FROM postings p "
                f"JOIN entries e ON e.id = p.entry_id WHERE p.account IN ({marks})", list(ledgers)).fetchall()
    except sqlite3.DatabaseError:
        for item in books.values():
            item.unreadable = True
        return books
    postings: dict[str, list[_Posting]] = {ledger: [] for ledger in ledgers}
    for account, raw_day, raw_amount, currency, source_id, raw_meta in rows:
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
        amount = amount.quantize(_CENT)
        item.items.append((day, amount))
        meta = _meta(raw_meta)
        text = lambda key: meta.get(key) if isinstance(meta.get(key), str) and meta.get(key) else None  # noqa: E731
        postings[str(account)].append(_Posting(
            day, amount, str(source_id) if source_id else None, text("import-source") in _OPENING_SOURCES,
            text("opening-source"), None if source_id else text("reverses"), text("correction-of"),
            text(FEED_PROVIDER_META) if chain_valid else None))
    for ledger, item in books.items():
        item.kinds = [(posting.day, kind, posting.opening_source) for posting, kind in _classify(postings[ledger])]
        item.series = Series(item.items)
    return books


class _Posting:
    """One posting on the checked account, with the entry facts that decide where it came from."""

    __slots__ = ("day", "amount", "source_id", "opening", "opening_source", "reverses", "correction_of",
                 "feed_provider")

    def __init__(self, day: date, amount: Decimal, source_id: str | None, opening: bool, opening_source: str | None,
                 reverses: str | None, correction_of: str | None, feed_provider: str | None = None) -> None:
        self.day, self.amount, self.source_id = day, amount, source_id
        self.opening, self.opening_source = opening, opening_source
        self.reverses, self.correction_of = reverses, correction_of
        self.feed_provider = feed_provider


def _ingested_from_feed(posting: "_Posting") -> bool:
    """Whether ``books ingest`` wrote this row from a connected feed, by its ID and its ``feed-provider`` mark.

    The ID is ``<provider>:<id>`` and the mark names that provider, which is in ``CONNECTED_FEED_PROVIDERS``; or the
    ID has no prefix and the mark is ``banksync``. Only ``books ingest`` with a connected-feed provider sets the mark
    (``source_ids.FEED_PROVIDER_META``). A row without it (any row imported before the mark existed, a generic
    ingest, a CSV, statement or manual row) does not count.
    """
    if not posting.source_id or posting.feed_provider not in CONNECTED_FEED_PROVIDERS:
        return False
    head, separator, rest = posting.source_id.partition(":")
    if separator:
        return head == posting.feed_provider and bool(rest)
    return posting.feed_provider == LEGACY_UNPREFIXED_PROVIDER


def _classify(postings: Sequence[_Posting]) -> list[tuple[_Posting, str]]:
    """Each posting with its kind: ``opening``, ``source`` (delivered by the bank connection) or ``none``.

    - ``opening``: an entry that an opening import made (``import-source`` in ``_OPENING_SOURCES``).
    - ``source``: a row that ``books ingest`` wrote from a connected feed (``_ingested_from_feed``), when it is the
      only posting of its source ID on this account. A correction
      (``correction-of``) is judged only through what it corrects: it counts when it corrects such a row on this
      account (through any number of corrections), it is the only posting of its source ID on this account, and it
      has the same day and amount. Its own source ID and marks do not count.
    - ``none``: anything else, such as a generic ingest, a CSV, statement or manual row, or a reversal.

    A reversal (no source ID, ``reverses`` names one) and the only posting it reverses are left out when they fall on
    one day with opposite amounts and either the reversed posting is an opening, or a correction of it on this
    account keeps the same day and amount. A reversal that removes a delivered row without a replacement stays,
    as ``none``.
    """
    by_source: dict[str, list[int]] = {}
    for index, posting in enumerate(postings):
        if posting.source_id:
            by_source.setdefault(posting.source_id, []).append(index)

    def single(source_id: str | None) -> _Posting | None:
        found = by_source.get(source_id or "", [])
        return postings[found[0]] if len(found) == 1 else None

    def delivered(posting: _Posting) -> bool:
        for _ in range(32):
            if posting.opening or not posting.source_id:
                return False
            if not posting.correction_of:
                # One posting of a feed row on the account: a split that puts a second one there does not count.
                return single(posting.source_id) is posting and _ingested_from_feed(posting)
            original = single(posting.correction_of)
            if (single(posting.source_id) is not posting or original is None
                    or (original.day, original.amount) != (posting.day, posting.amount)):
                return False
            posting = original
        return False

    cancelled: set[int] = set()
    for index, posting in enumerate(postings):
        if not posting.reverses:
            continue
        found = by_source.get(posting.reverses, [])
        if len(found) != 1 or found[0] in cancelled:
            continue
        original = postings[found[0]]
        if (original.day, original.amount) != (posting.day, -posting.amount):
            continue
        kept = original.opening or any(
            other.correction_of == original.source_id and (other.day, other.amount) == (original.day, original.amount)
            for other in postings)
        if kept:
            cancelled.update((index, found[0]))
    return [(posting, "opening" if posting.opening else ("source" if delivered(posting) else "none"))
            for index, posting in enumerate(postings) if index not in cancelled]


def _meta(raw_meta: Any) -> dict[str, Any]:
    try:
        return dict(json.loads(raw_meta)) if raw_meta else {}
    except (TypeError, ValueError):
        return {}


def _existing(records: Any, account: str, end: date) -> Mapping[str, Any] | None:
    for record in records if isinstance(records, list) else []:
        if isinstance(record, Mapping) and record.get("account") == account and record.get("as_of") == end.isoformat():
            return record
    return None


def prove(entity_path: Path | str, accounts: Sequence[Mapping[str, Any]], *, as_of: date,
          from_month: str | None = None) -> dict[str, Any]:
    """Say, per feed account and finished month, whether the feed proves the month end.

    See the module documentation for the input shape and the rule.
    """
    from .coverage import load_statement_documents, load_statements
    from .entity import flat_bank_account_mappings, load_entity

    entity = load_entity(entity_path)
    mappings = flat_bank_account_mappings(entity.entity_config)
    by_ledger: dict[str, list[str]] = {}
    for feed_id, ledger in mappings.items():
        by_ledger.setdefault(ledger, []).append(feed_id)
    records, _ = load_statements(entity.path)
    period_confirmed = {str(document.get("statement_id")): document.get("period_confirmed")
                        if isinstance(document.get("period_confirmed"), bool) else None
                        for document in load_statement_documents(entity.path)}
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
            rows.append(_row(account_id, ledger, None, "not_checking_or_savings",
                             account_type=_account_type(item.get("account_type")) or None))
            continue
        if len(by_ledger.get(ledger, [])) != 1:
            rows.append(_row(account_id, ledger, None, "shared_ledger_account"))
            continue
        candidates.append((account_id, ledger, item))

    ledgers = sorted({ledger for _, ledger, _ in candidates})
    books = _load_books(entity.path, ledgers)
    waiting, waiting_unreadable = load_waiting_checked(entity, ledgers)
    for account_id, ledger, item in candidates:
        side = books[ledger]
        zone, zone_ok = _zone(item.get("time_zone"))
        currency = item.get("currency")
        if item.get("time_zone") in (None, ""):
            rows.append(_row(account_id, ledger, None, "time_zone_required"))
            continue
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
        if waiting_unreadable[ledger]:
            rows.append(_row(account_id, ledger, None, "waiting_unreadable"))
            continue
        windows = _merged([(r.period_start, r.period_end) for r in records
                           if r.account == ledger and r.kind == "feed_sync"])
        if not windows:
            rows.append(_row(account_id, ledger, None, "feed_incomplete", feed_from=None))
            continue
        statements: dict[date, list[tuple[str, Decimal]]] = {}
        for r in records:
            if r.account == ledger and r.kind == "statement" and r.closing_balance is not None:
                statements.setdefault(r.period_end, []).append((r.statement_id, r.closing_balance))
        balances = _statement_balances(records, ledger)
        readings = [reading for reading in _readings(item.get("readings"), zone) if reading["read_day"] <= as_of]
        context = _Context(ledger, side, waiting[ledger], windows, readings, reconciliations, statements, zone,
                           balances, period_confirmed)
        start_from = max(first_month or windows[0][0].replace(day=1), date(1900, 1, 1))
        for month, start, end in _months(start_from, as_of - timedelta(days=1)):
            row = _month(account_id, month, start, end, context)
            if row["status"] == "proven":
                context.proven[end] = (Decimal(row["reconcile"]["source_balance"]), row["basis"])
            rows.append(row)
    rows.sort(key=lambda row: (str(row["ledger_account"] or ""), row["account_id"], str(row["month"] or "")))
    return {"status": "ok", "as_of": as_of.isoformat(), "months": rows}


class _Context:
    """What the check of one feed account needs for each of its months."""

    def __init__(self, ledger: str, side: _Books, waiting: Series, windows: list[tuple[date, date]],
                 readings: list[dict[str, Any]], reconciliations: Any,
                 statements: Mapping[date, list[tuple[str, Decimal]]], zone: tzinfo,
                 statement_balances: Sequence[tuple[date, str, Decimal]] = (),
                 period_confirmed: Mapping[str, bool | None] | None = None) -> None:
        self.ledger = ledger
        self.side = side
        self.waiting = waiting
        self.windows = windows
        self.readings = readings
        self.reconciliations = reconciliations
        self.statements = statements
        self.zone = zone
        #: Every dated balance of every statement of the account: (day, statement ID, balance), earliest first.
        self.statement_balances = sorted(statement_balances)
        #: ``period_confirmed`` of each statement record that states it (True or False), else None.
        self.period_confirmed = dict(period_confirmed or {})
        #: Month ends that this check proved, with the closing balance and the basis.
        self.proven: dict[date, tuple[Decimal, str]] = {}


def _row(account_id: str, ledger: str | None, month: str | None, reason: str, **fields: Any) -> dict[str, Any]:
    return {"account_id": account_id, "ledger_account": ledger, "month": month,
            "status": "proven" if reason == "proven" else "not_proven", "reason": reason,
            "reconcile": None, "reading": None, "basis": None, **fields}


def _shown(reading: Mapping[str, Any]) -> dict[str, Any]:
    return {"balance": _fmt(reading["balance"]), "as_of": reading["as_of"], "day": reading["day"].isoformat(),
            "read_day": reading["read_day"].isoformat(), "retrieved_at": reading["retrieved_at"],
            "data_through": reading["data_through"].isoformat()}


def _gap(windows: list[tuple[date, date]], start: date, end: date) -> dict[str, Any]:
    """Where the feed windows stop covering ``start`` through ``end``: the first uncovered days."""
    cursor = start
    for first, last in windows:
        if last < cursor:
            continue
        if first > cursor:
            return {"uncovered_from": cursor.isoformat(),
                    "uncovered_to": min(first - timedelta(days=1), end).isoformat()}
        cursor = last + timedelta(days=1)
        if cursor > end:
            return {"uncovered_from": None, "uncovered_to": None}
    return {"uncovered_from": cursor.isoformat(), "uncovered_to": end.isoformat()}


def _feed_incomplete(account_id: str, ledger: str, month: str, windows: list[tuple[date, date]], start: date,
                     day: date, **fields: Any) -> dict[str, Any]:
    return _row(account_id, ledger, month, "feed_incomplete", feed_from=windows[0][0].isoformat(),
                **_gap(windows, start, day), **fields)


def _statement_balances(records: Sequence[Any], ledger: str) -> list[tuple[date, str, Decimal]]:
    """Every dated balance of every statement of the account, in the ledger sign: (day, statement ID, balance).

    A statement's opening balance is the balance on the day before its first day; its closing balance is the
    balance on its last day.
    """
    from .tieout import presentation_sign
    sign = presentation_sign(ledger)
    balances: list[tuple[date, str, Decimal]] = []
    for r in records:
        if r.account != ledger or r.kind != "statement":
            continue
        if r.closing_balance is not None:
            balances.append((r.period_end, r.statement_id, r.closing_balance * sign))
        if r.opening_balance is not None:
            balances.append((r.period_start - timedelta(days=1), r.statement_id, r.opening_balance * sign))
    return sorted(balances)


def _evidence(side: _Books, balances: Sequence[tuple[date, str, Decimal]], day: date) -> str:
    """``statement`` when a statement backs the books on or before ``day``, else ``derived``.

    A statement backs them when an opening entry on or before ``day`` has ``opening-source: printed`` (a printed
    statement balance), or when a statement balance dated on or before ``day`` equals the books on its date. Anything
    else is ``derived``: a typed opening of any caller kind, an old entry without the mark, or no opening entry.
    """
    if any(kind == "opening" and source == "printed" and d <= day for d, kind, source in side.kinds):
        return "statement"
    if any(when <= day and side.series.through(when) == balance for when, _, balance in balances):
        return "statement"
    return "derived"


def opening_evidence(entity_path: Path | str, account: str, day: date) -> str:
    """The ``opening_evidence`` of the month of ``account`` that ends on ``day``, from the books and statements only."""
    from .coverage import load_statements

    side = _load_books(Path(entity_path), [account])[account]
    records, _ = load_statements(entity_path)
    return _evidence(side, _statement_balances(records, account), day)


def _opening(start: date, context: _Context) -> tuple[Decimal | None, str, str, dict[str, Any]]:
    """The proven bank opening of a month (rule 7): the figure, the reason when there is none, its grade and fields.

    The grade is ``reading`` when a statement or a bank reading for the day gave the figure, else ``computed``. The
    reason ``no_source`` means that rule 7 has nothing, so rule 7b may compute the opening.
    """
    before = start - timedelta(days=1)
    if before in context.proven:
        balance, basis = context.proven[before]
        return balance, "", "reading" if basis == "feed_reading" else "computed", {}
    candidates: list[tuple[Decimal, str]] = []
    prior = _existing(context.reconciliations, context.ledger, before)
    if prior is not None:
        balance = _dec(prior.get("source_balance"))
        if prior.get("status") not in ("clean", "resolved") or balance is None:
            return None, "opening_unproven", "", {}
        candidates.append((balance, "reading" if record_basis(dict(prior)) in _READING_GRADE_BASES else "computed"))
    candidates.extend((closing, "reading") for _, closing in context.statements.get(before, []))
    if candidates:
        figures = {balance for balance, _ in candidates}
        if len(figures) > 1:
            # A statement and a record (or two statements) for the month before disagree: neither proves it.
            return None, "opening_conflict", "", {"opening_balances": sorted(_fmt(value) for value in figures)}
        grade = "reading" if any(grade == "reading" for _, grade in candidates) else "computed"
        return candidates[0][0], "", grade, {}
    # The first month of the feed: the account starts at zero only when nothing in the books is dated before this
    # month, the feed starts in this month, and every posting of the month is a row the bank connection delivered.
    kinds = context.side.kinds
    end = _month_end(start.year, start.month)
    first_feed_month = context.windows[0][0].replace(day=1) == start
    earlier = any(day < start for day, _, _ in kinds)
    sourced_in_month = any(start <= day <= end and kind == "source" for day, kind, _ in kinds)
    unsourced_in_month = any(start <= day <= end and kind != "source" for day, kind, _ in kinds)
    if first_feed_month and not earlier and sourced_in_month and not unsourced_in_month:
        return _ZERO, "", "reading", {}
    return None, "no_source", "", {}


def _computed_opening(start: date, anchor: Mapping[str, Any], context: _Context) -> tuple[Decimal | None, str,
                                                                                          dict[str, Any]]:
    """Rule 7b: the opening computed back from the anchor reading, or None with the reason and its fields."""
    side, windows, postings = context.side, context.windows, context.side.series
    first_day, day = windows[0][0], anchor["day"]
    if not _covers(windows, first_day, day):
        return None, "feed_incomplete", {"feed_from": first_day.isoformat(), **_gap(windows, first_day, day)}
    # The newest reading whose day the feed covers from its first day: the books must still equal the bank on it.
    newest = [reading for reading in context.readings if _covers(windows, first_day, reading["day"])][-1]
    last_day = max(day, newest["day"])
    # An opening entry: any posting before the first feed day, or one on it that the connection did not deliver.
    openings = [source for d, kind, source in side.kinds if d < first_day or (d == first_day and kind != "source")]
    if len(openings) > 1:
        return None, "opening_entry_unclear", {"opening_entries": len(openings)}
    unsourced = sorted(d for d, kind, _ in side.kinds if first_day < d <= last_day and kind != "source")
    if unsourced:
        return None, "unsourced_postings", {"unsourced_count": len(unsourced),
                                            "unsourced_from": unsourced[0].isoformat()}
    if not any(first_day <= d <= day and kind == "source" for d, kind, _ in side.kinds):
        return None, "no_feed_rows", {}
    if context.waiting.count_between(None, newest["day"]):
        return None, "waiting_in_review", {"reading": _shown(newest)}
    books_on_newest = postings.through(newest["day"])
    if books_on_newest != newest["balance"]:
        return None, "newest_reading_differs", {
            "reading": _shown(newest), "books_balance": _fmt(books_on_newest),
            "difference": _fmt(newest["balance"] - books_on_newest), "difference_on": newest["day"].isoformat()}
    return anchor["balance"] - postings.between(start, day), "", {}


def _differs(account_id: str, ledger: str, month: str, reading: Mapping[str, Any], postings: Series,
             later: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """``books_differ`` for ``reading``. ``later_reading_agrees`` says whether a later reading equals the books: the
    differing reading may be stale, and the host must mark it partial or remove it."""
    books_on_day = postings.through(reading["day"])
    agrees = any(postings.through(other["day"]) == other["balance"] for other in later)
    return _row(account_id, ledger, month, "books_differ", reading=_shown(reading),
                books_balance=_fmt(books_on_day), difference=_fmt(reading["balance"] - books_on_day),
                difference_on=reading["day"].isoformat(), later_reading_agrees=agrees)


def _month(account_id: str, month: str, start: date, end: date, context: _Context) -> dict[str, Any]:
    ledger, windows, postings = context.ledger, context.windows, context.side.series
    existing = _existing(context.reconciliations, ledger, end)
    if existing is not None:
        ledger_balance = _dec(existing.get("ledger_balance"))
        if existing.get("status") == "clean" and ledger_balance == postings.through(end):
            return _row(account_id, ledger, month, "already_reconciled", basis=record_basis(dict(existing)))
        if not _from_feed(dict(existing)):
            return _row(account_id, ledger, month, "existing_record")
    if context.statements.get(end):
        # A statement outranks the feed: reconcile the month from the statement.
        return _row(account_id, ledger, month, "statement_available",
                    statement_ids=sorted(statement_id for statement_id, _ in context.statements[end]))
    if context.side.chain_invalid:
        return _row(account_id, ledger, month, "audit_chain_invalid")
    # The bank day of the month end must be over, with a margin: read at least SAFETY_MARGIN_HOURS after the bank's
    # midnight that starts the next day.
    closed = day_closed_at(end, context.zone)
    after_end = [r for r in context.readings if r["day"] >= end and r["instant"] >= closed]
    if not after_end:
        return _row(account_id, ledger, month, "no_balance_after_month_end")
    # Readings in order: the first one that the feed covers, with nothing waiting and equal to the books, is the anchor.
    # Every reading before it, and every other reading for its day, must also equal the books.
    chosen: dict[str, Any] | None = None
    for index, reading in enumerate(after_end):
        day = reading["day"]
        later = [other for other in after_end[index + 1:] if _covers(windows, start, other["day"])]
        if chosen is not None:
            if day != chosen["day"]:
                break
            if postings.through(day) != reading["balance"]:
                return _differs(account_id, ledger, month, reading, postings, later)
            continue
        if not _covers(windows, start, day):
            return _feed_incomplete(account_id, ledger, month, windows, start, day)
        if context.waiting.count_between(None, day):
            return _row(account_id, ledger, month, "waiting_in_review")
        if postings.through(day) != reading["balance"]:
            return _differs(account_id, ledger, month, reading, postings, later)
        chosen = reading
    reading = chosen
    day = reading["day"]
    shown = _shown(reading)
    # A statement balance dated from the day before the month through the reading day must equal the books on its day,
    # also for a statement whose cycle does not end on a month end.
    for when, statement_id, balance in context.statement_balances:
        if start - timedelta(days=1) <= when <= day and postings.through(when) != balance:
            return _row(account_id, ledger, month, "statement_differs", reading=shown, statement_id=statement_id,
                        statement_date=when.isoformat(), statement_balance=_fmt(balance),
                        books_balance=_fmt(postings.through(when)),
                        difference=_fmt(balance - postings.through(when)), difference_on=when.isoformat(),
                        period_confirmed=context.period_confirmed.get(statement_id))
    opening, reason, grade, fields = _opening(start, context)
    if reason == "no_source":
        opening, reason, fields = _computed_opening(start, reading, context)
        grade = "computed"
    if opening is None:
        return _row(account_id, ledger, month, reason, **{"reading": shown, **fields})
    books_opening = postings.through(start - timedelta(days=1))
    if opening != books_opening:
        return _row(account_id, ledger, month, "opening_differs", reading=shown,
                    opening_balance=_fmt(opening), books_opening=_fmt(books_opening),
                    difference=_fmt(opening - books_opening), difference_on=(start - timedelta(days=1)).isoformat())
    after = postings.between(end + timedelta(days=1), day) if day > end else _ZERO
    closing = reading["balance"] - after
    basis = "feed_reading" if day == end and grade == "reading" else "feed_computed"
    figures = {"account": ledger, "as_of": end.isoformat(), "source_balance": _fmt(closing),
               "source_opening_balance": _fmt(opening), "source_transaction_total": _fmt(closing - opening),
               "source_snapshot_at": reading["retrieved_at"], "source_through": end.isoformat(), "proof_basis": basis}
    extra: dict[str, Any] = {}
    if basis == "feed_computed":
        extra["opening_evidence"] = _evidence(context.side, context.statement_balances, end)
    return _row(account_id, ledger, month, "proven", reading=shown, books_balance=_fmt(postings.through(day)),
                difference=_fmt(_ZERO), basis=basis, reconcile=figures, **extra)


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
