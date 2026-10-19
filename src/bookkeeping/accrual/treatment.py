"""Decide which bank or card activity needs an explicit accrual treatment.

``needs_treatment(entity, txn)`` is the one place that answers this question.
The importer hold, queue auto-post, queue confirm, routine posting and Stripe
planning all call it.

Result by discipline (see ``scope.discipline``):

``off``
    Never. The cash path stays exactly as it was.
``all``
    Always. Every bank transaction needs a treatment, as before.
``per_account``
    Only for a transaction that could settle or fund an accrual record. Every
    other transaction takes the ordinary cash path, so a company that turns
    accrual on later keeps its normal automation for ordinary spending.

Double counting is the risk. If a bill is entered (expense and payable) and its
payment then auto-posts as a plain expense, the cost counts twice. The same
holds for an invoice and its deposit counted as income. The per_account rule
therefore holds a transaction for review when it plausibly belongs to an open
record. If the match is uncertain, hold the transaction. A held transaction
costs the owner one click. A wrong auto-post costs a correction.

A transaction needs treatment when all of these are true:

1. Its date is on or after ``accrual_tracking_start``. Earlier activity is
   outside the accrual books and is never held for this reason.
2. At least one of these matches an open record:

   a. Source: a record already names the transaction as ``origin.source_id``.
   b. Counterparty: every significant word of an open item's or active
      schedule's counterparty or reference appears in the transaction's
      counterparty or description. Case, punctuation and filler words such as
      "inc", "llc", "ach" and "payment" are ignored.
   c. Amount: the absolute transaction amount equals the remaining amount of an
      open item, or the remaining or original amount of an active schedule.
      The sign is not compared, because card and bank accounts use opposite
      signs. A same-amount charge from an unrelated vendor is also held. That
      is a deliberate false hold.

Open means status "open" for an item and "active" for a schedule, with money
remaining. Settled, closed and reversed records never match.

Examples, with tracking start 2026-03-01 and an open bill "Acme Supplies" of
500.00:

- Bank payment "ACH PMT ACME SUPPLIES 4432" of -500.00 on 2026-03-10: held
  (counterparty and amount match).
- Bank payment "Acme Supplies" of -125.00 on 2026-03-10: held (counterparty).
- Card charge "Mystery Vendor" of -500.00 on 2026-03-10: held (amount).
- Card charge "Coffee Shop" of -4.50 on 2026-03-10: not held, auto-posts as
  today.
- Bank payment "ACME SUPPLIES" of -500.00 on 2026-02-27: not held (before the
  tracking start).
- The same payment after the bill is fully settled: not held.

The function reads records and never writes. A malformed transaction does not
need treatment here; the ordinary path rejects it with its normal error.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from . import scope

_FILLER = frozenset({
    "inc", "llc", "ltd", "corp", "co", "company", "the", "and", "ach", "pmt", "payment", "pymt", "card",
    "purchase", "debit", "credit", "pos", "online", "transfer", "web", "ppd", "ccd", "bill", "invoice", "inv",
})
_DONE_ITEM = {"settled", "closed", "reversed"}
_DONE_SCHEDULE = {"completed", "closed", "reversed", "cancelled", "canceled", "settled"}


def _words(*values: Any) -> frozenset[str]:
    text = " ".join(str(v or "") for v in values).lower()
    return frozenset(w for w in re.findall(r"[a-z0-9]+", text) if len(w) >= 3 and w not in _FILLER and not w.isdigit())


def _money(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)).copy_abs().quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _open_records(entity: Any) -> list[tuple[str, Mapping[str, Any]]]:
    from ..ledger.store import LedgerStore
    from .store import list_records
    path = entity.path / "ledger.sqlite"
    if not path.is_file():
        return []
    store = LedgerStore(path)
    rows: list[tuple[str, Mapping[str, Any]]] = []
    for row in list_records(store, "items"):
        if row.get("status", "open") not in _DONE_ITEM and (_money(row.get("remaining_amount")) or Decimal(0)) > 0:
            rows.append(("items", row))
    for row in list_records(store, "schedules"):
        if row.get("status", "active") not in _DONE_SCHEDULE and (_money(row.get("remaining_amount")) or Decimal(0)) > 0:
            rows.append(("schedules", row))
    return rows


def _all_sources(entity: Any) -> set[str]:
    """Source ids named by any record, open or settled."""
    from ..ledger.store import LedgerStore
    from .store import list_records
    path = entity.path / "ledger.sqlite"
    if not path.is_file():
        return set()
    store = LedgerStore(path)
    return {str((row.get("origin") or {}).get("source_id") or "")
            for table in ("items", "schedules") for row in list_records(store, table)} - {""}


def treatment_checker(entity: Any):
    """Return ``check(txn)``, which reads the records once for a batch of transactions."""
    mode = scope.discipline(entity)
    if mode == scope.DISCIPLINE_ALL:
        return lambda txn: True
    if mode != scope.DISCIPLINE_PER_ACCOUNT:
        return lambda txn: False
    start = scope.tracking_start(entity)
    first = date.fromisoformat(start) if start else None
    state: dict[str, Any] = {}

    def check(txn: Mapping[str, Any]) -> bool:
        try:
            when = date.fromisoformat(str(txn.get("date") or ""))
        except ValueError:
            return False
        if first is None or when < first:
            return False
        if "records" not in state:
            state["records"], state["sources"] = _open_records(entity), _all_sources(entity)
        source_id = str(txn.get("id") or txn.get("source_id") or "")
        if source_id and source_id in state["sources"]:
            return True
        amount = _money(txn.get("amount"))
        txn_words = _words(txn.get("counterparty"), txn.get("description"))
        for table, row in state["records"]:
            record_words = _words(row.get("counterparty"), row.get("reference"))
            if record_words and record_words <= txn_words:
                return True
            if amount is not None and amount > 0:
                candidates = [row.get("remaining_amount")]
                if table == "schedules":
                    candidates.append(row.get("amount"))
                if any(_money(value) == amount for value in candidates):
                    return True
        return False

    return check


def needs_treatment(entity: Any, txn: Mapping[str, Any]) -> bool:
    """True when this bank or card transaction must be held for an explicit accrual treatment."""
    return treatment_checker(entity)(txn)
