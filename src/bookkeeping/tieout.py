"""Tie-out proof: do a statement's rows add up, and do the books agree with it.

Everything here is read-only and deterministic. Sums are Decimal and happen in
this module, never in a host. Nothing in this module writes to the ledger.

Sign convention
---------------
Ledger amounts use the ledger sign: a charge on a card is negative. A statement
balance uses the statement presentation: money held for an asset account and
money owed for a card or loan. ``presentation_sign`` is the one place that
relates the two. Every figure that leaves this module is in presentation sign.

Three checks
------------
- ``statement_tieout``: opening balance plus the rows of one file equal the
  closing balance. Used at import time.
- ``period_tieout``: the live ledger agrees with a recorded statement. The
  books movement in the statement period is added to the statement's opening
  balance and compared with the statement's closing balance. Used by coverage.
  Staged rows that are not yet in the ledger are counted as ``waiting_rows``.
- ``balance_check``: a balance that a bank feed reported agrees with the books
  balance for the mapped ledger account on the same day.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import date
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from .entity import _locked_entity_operation
from .ledger.store import LedgerStore, default_store_path

_CENT = Decimal("0.01")
_ZERO = Decimal("0.00")

TIEOUT_STATUSES = ("matches", "differs", "no_balances", "not_aligned")
BALANCE_STATUSES = ("matches", "waiting_in_review", "differs", "bank_newer",
                    "unmapped", "no_bank_balance", "not_aligned")


def presentation_sign(ledger_account: str) -> int:
    """The one place that relates the two sign conventions.

    Transactions and ledger balances use the ledger sign: a charge on a card
    is negative, because it lowers the account. A statement record and a
    printed statement use the statement presentation: money held for a bank
    account, money owed for a card or loan, so a charge is positive. The
    sign is ``-1`` for a liability account and ``1`` for an asset account.
    Multiply by it to convert in either direction.
    """
    return -1 if ledger_account.startswith("Liabilities:") else 1


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result.quantize(_CENT) if result.is_finite() else None


def _fmt(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(_CENT) + _ZERO, "f")


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Import time: one statement file
# ---------------------------------------------------------------------------

def statement_tieout(record: Mapping[str, Any], ledger_sign: int) -> dict[str, Any]:
    """Check that opening balance plus the file's rows equals the closing balance.

    ``record`` holds ``opening_balance`` and ``closing_balance`` in statement
    presentation, ``transactions_total`` in ledger sign and ``transaction_count``.
    ``ledger_sign`` is ``presentation_sign(account)``: it converts the rows
    total to presentation once. Returns ``status`` of ``matches``, ``differs``
    or ``no_balances`` (a balance is missing, so nothing can be proved).
    """
    opening = _dec(record.get("opening_balance"))
    closing = _dec(record.get("closing_balance"))
    total = (_dec(record.get("transactions_total")) or _ZERO) * ledger_sign
    expected = None if opening is None else opening + total
    difference = None
    if opening is None or closing is None:
        status = "no_balances"
    else:
        difference = closing - expected
        status = "matches" if difference == 0 else "differs"
    return {
        "status": status,
        "opening": _fmt(opening),
        "rows_total": _fmt(total),
        "expected_closing": _fmt(expected),
        "closing": _fmt(closing),
        "difference": _fmt(difference),
        "row_count": int(record.get("transaction_count") or 0),
    }


# ---------------------------------------------------------------------------
# Books side: postings and staged rows, read without writing
# ---------------------------------------------------------------------------

class Series:
    """Dated amounts with cumulative sums, so a period sum is two lookups."""

    def __init__(self, items: Sequence[tuple[date, Decimal]] = ()) -> None:
        self.opening_states: dict[date, Series] = {}
        ordered = sorted(items, key=lambda item: item[0])
        self._days = [day for day, _ in ordered]
        self._cumulative: list[Decimal] = []
        running = _ZERO
        for _, amount in ordered:
            running += amount
            self._cumulative.append(running)

    def through(self, day: date | None) -> Decimal:
        """Sum of amounts dated on or before ``day`` (all amounts when None)."""
        if not self._days:
            return _ZERO
        if day is None:
            return self._cumulative[-1]
        index = bisect_right(self._days, day)
        return self._cumulative[index - 1] if index else _ZERO

    def between(self, start: date, end: date) -> Decimal:
        before = bisect_left(self._days, start)
        before_sum = self._cumulative[before - 1] if before else _ZERO
        return self.through(end) - before_sum

    def count_between(self, start: date | None, end: date | None) -> int:
        low = 0 if start is None else bisect_left(self._days, start)
        high = len(self._days) if end is None else bisect_right(self._days, end)
        return max(0, high - low)


def source_root(metadata: Mapping[str, str], by_source: Mapping[str, Mapping[str, str]]) -> str | None:
    """Original source identity through corrections and source-less reversals."""
    current = metadata.get("reverses") or metadata.get("correction-of") or metadata.get("source-id")
    seen: set[str] = set()
    while current in by_source:
        if current in seen:
            raise ValueError("Cyclic entry correction lineage.")
        seen.add(current)
        parent = by_source[current].get("correction-of")
        if not parent:
            break
        current = parent
    return current


def load_postings(entity_path: Path | str, accounts: Sequence[str]) -> dict[str, Series]:
    """All postings, with opening state retained separately from period flow."""
    items: dict[str, list[tuple[date, Decimal]]] = {account: [] for account in accounts}
    openings: dict[str, dict[date, list[tuple[date, Decimal]]]] = {account: {} for account in accounts}
    store_path = default_store_path(Path(entity_path))
    if store_path.is_file() and accounts:
        placeholders = ",".join("?" for _ in accounts)
        with LedgerStore(store_path).connection() as conn:
            entries = {str(sid): (date.fromisoformat(day), dict(json.loads(meta)))
                       for sid, day, meta in conn.execute(
                           "SELECT source_id,date,metadata_json FROM entries WHERE source_id IS NOT NULL")}
            metadata = {sid: facts[1] for sid, facts in entries.items()}
            rows = conn.execute(
                f"SELECT p.account,e.date,p.amount,e.metadata_json FROM postings p JOIN entries e ON e.id=p.entry_id "
                f"WHERE p.account IN ({placeholders})", list(accounts)).fetchall()
        for account, raw_day, raw_amount, raw_meta in rows:
            day, amount = _day(raw_day), _dec(raw_amount)
            if day is not None and amount is not None:
                items[str(account)].append((day, amount))
                root = source_root(dict(json.loads(raw_meta)), metadata)
                opening = entries.get(root)
                if opening and opening[1].get("import-source") in {
                        "quickbooks-opening", "accrual-opening", "accrual-tracking-opening"}:
                    openings[str(account)].setdefault(opening[0], []).append((day, amount))
    result = {account: Series(values) for account, values in items.items()}
    for account, states in openings.items():
        result[account].opening_states = {day: Series(values) for day, values in states.items()}
    return result


def load_waiting(entity: Any, accounts: Sequence[str]) -> dict[str, Series]:
    """Staged rows (not yet posted) per ledger account, in ledger sign."""
    from .ledger.importer import _ledger_account_for_txn

    items: dict[str, list[tuple[date, Decimal]]] = {account: [] for account in accounts}
    path = Path(entity.staging_dir) / "pending-categorization.json"
    try:
        staged = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        staged = []
    config = entity.entity_config
    for txn in staged if isinstance(staged, list) else []:
        if not isinstance(txn, dict):
            continue
        try:
            account = _ledger_account_for_txn(
                txn, config.get("bank_account_mappings"), config.get("csv_account_mappings"))
        except ValueError:
            continue
        if account not in items:
            continue
        day = _day(txn.get("date"))
        if txn.get("amount") is None:
            amount = (_dec(txn.get("creditAmount")) or _ZERO) - (_dec(txn.get("debitAmount")) or _ZERO)
        else:
            amount = _dec(txn.get("amount"))
        if day is not None and amount is not None:
            items[account].append((day, amount))
    return {account: Series(values) for account, values in items.items()}


# ---------------------------------------------------------------------------
# Coverage: a recorded statement against the live books
# ---------------------------------------------------------------------------

def period_tieout(*, account: str, statement_id: str, start: date, end: date,
                  opening: Decimal | None, closing: Decimal | None,
                  postings: Series, waiting: Series) -> dict[str, Any]:
    """Compare a statement's closing balance with what the books imply.

    ``books_balance_through`` is the statement's opening balance plus the books
    movement from ``start`` to ``end``, in presentation sign. Only the movement
    in the period is used, so a company whose books begin after the account
    did is not penalized for the balance before its books. ``difference`` is
    the statement closing balance minus that figure. ``waiting_rows`` counts
    staged rows dated in the period for the account, and ``waiting_total`` is
    their sum in presentation sign.
    """
    sign = presentation_sign(account)
    result: dict[str, Any] = {
        "status": "no_balances", "statement_id": statement_id,
        "statement_balance": _fmt(closing), "books_balance_through": None,
        "difference": None, "waiting_rows": waiting.count_between(start, end),
        "waiting_total": _fmt(waiting.between(start, end) * sign),
    }
    if opening is None or closing is None:
        return result
    state = postings.opening_states.get(start)
    movement = postings.between(start, end) - (state.between(start, end) if state else _ZERO)
    books = opening + movement * sign
    difference = closing - books
    result.update(status="matches" if difference == 0 else "differs",
                  books_balance_through=_fmt(books), difference=_fmt(difference))
    return result


def not_aligned(statement_id: str | None = None) -> dict[str, Any]:
    return {"status": "not_aligned", "statement_id": statement_id, "statement_balance": None,
            "books_balance_through": None, "difference": None, "waiting_rows": None,
            "waiting_total": None}


# ---------------------------------------------------------------------------
# Bank balance against books
# ---------------------------------------------------------------------------

@_locked_entity_operation
def balance_check(entity_path: Path | str, items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare balances that a bank feed reported with the books.

    Each input item is ``{account_id, balance, as_of, data_through}`` with
    optional ``retrieved_at`` and ``sign``. ``balance`` is in statement
    presentation (money held, or money owed for a card or loan) unless ``sign``
    is ``"ledger"`` (a card debt is negative). ``account_id`` is looked up in
    the entity ``bank_account_mappings``. Items that map to the same ledger
    account are summed into one row, because the books hold one balance for
    that account. The books balance is every posting through the compare date.
    """
    from .entity import flat_bank_account_mappings, load_entity

    entity = load_entity(entity_path)
    mappings = flat_bank_account_mappings(entity.entity_config)
    rows: list[dict[str, Any]] = []
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for item in items:
        account_id = str(item.get("account_id") or "")
        ledger = mappings.get(account_id) if isinstance(mappings, Mapping) else None
        if not isinstance(ledger, str) or not ledger.startswith(("Assets:", "Liabilities:")):
            rows.append(_row(status="unmapped", account_ids=[account_id]))
        else:
            groups.setdefault(ledger, []).append(item)
    postings = load_postings(entity.path, sorted(groups))
    waiting = load_waiting(entity, sorted(groups))
    for ledger, members in sorted(groups.items()):
        rows.append(_compare(ledger, members, postings[ledger], waiting[ledger]))
    rows.sort(key=lambda row: (str(row["ledger_account"] or ""), row["account_ids"]))
    return {"status": "ok", "accounts": rows}


def _row(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": None, "account_ids": [], "ledger_account": None, "bank_balance": None,
        "books_balance": None, "difference": None, "waiting_rows": None, "waiting_total": None,
        "compared_through": None, "compared_basis": None,
    }
    base.update(fields)
    return base


def _compare(ledger: str, members: Sequence[Mapping[str, Any]], postings: Series,
             waiting: Series) -> dict[str, Any]:
    sign = presentation_sign(ledger)
    ids = sorted(str(m.get("account_id") or "") for m in members)
    if len(set(ids)) != len(ids):
        return _row(status="not_aligned", account_ids=ids, ledger_account=ledger,
                    reason="duplicate_account_id")
    effective_days = [(_day(m.get("as_of")) or _day(m.get("retrieved_at")) or _day(m.get("data_through")))
                      for m in members]
    if len(members) > 1 and (None in effective_days or len(set(effective_days)) != 1):
        return _row(status="not_aligned", account_ids=ids, ledger_account=ledger,
                    reason="balance_dates_not_aligned")
    total = _ZERO
    for member in members:
        balance = _dec(member.get("balance"))
        if balance is None:
            return _row(status="no_bank_balance", account_ids=ids, ledger_account=ledger)
        total += balance * (sign if member.get("sign") == "ledger" else 1)
    # Compare date: the bank's as-of day, else the day it was retrieved, else what the books hold.
    as_of_days = [(_day(m.get("as_of")), "as_of") if _day(m.get("as_of"))
                  else (_day(m.get("retrieved_at")), "retrieved_at") for m in members]
    known = [(day, basis) for day, basis in as_of_days if day is not None]
    through = [day for day in (_day(m.get("data_through")) for m in members) if day is not None]
    compare, basis = (min(known, key=lambda item: item[0]) if known else
                      ((min(through), "data_through") if through else (None, "latest")))
    result = _row(account_ids=ids, ledger_account=ledger, bank_balance=_fmt(total),
                  compared_through=None if compare is None else compare.isoformat(),
                  compared_basis=basis)
    books = postings.through(compare) * sign
    wait_rows = waiting.count_between(None, compare)
    wait_total = waiting.through(compare) * sign
    difference = total - books
    result.update(books_balance=_fmt(books), difference=_fmt(difference),
                  waiting_rows=wait_rows, waiting_total=_fmt(wait_total))
    if known and through and compare > min(through):
        result["status"] = "bank_newer"
    elif difference == 0:
        result["status"] = "matches"
    elif wait_rows and difference == wait_total:
        result["status"] = "waiting_in_review"
    else:
        result["status"] = "differs"
    return result


# ---------------------------------------------------------------------------
# CLI: books balance check
# ---------------------------------------------------------------------------

def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser("balance", help="Compare a reported bank balance with the books (read-only)")
    sub = parser.add_subparsers(dest="balance_command", required=True)
    check = sub.add_parser(
        "check", help="Compare bank-reported balances with the books balance for each mapped account")
    check.add_argument("--entity", required=True, type=Path)
    check.add_argument("--input", required=True, type=Path,
                       help='JSON list of {"account_id", "balance", "as_of", "data_through"}; '
                            'optional "retrieved_at" and "sign" ("presentation" or "ledger")')
    check.add_argument("--format", choices=["json", "text"], default="json", dest="fmt")


def run(args: Any) -> int:
    import sys

    try:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
        items = payload.get("accounts") if isinstance(payload, dict) else payload
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ValueError("The input must be a JSON list of objects.")
        document = balance_check(args.entity, items)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.fmt == "json":
        print(json.dumps(document, indent=2, sort_keys=True))
    else:
        for row in document["accounts"]:
            label = row["ledger_account"] or ", ".join(row["account_ids"])
            print(f"{label}: {row['status']} (bank {row['bank_balance']}, books {row['books_balance']}, "
                  f"difference {row['difference']})")
    return 0
