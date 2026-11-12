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
      counterparty or description. Numeric references match whole tokens and
      require an adjacent invoice/inv/bill/#/no. marker; dates and check numbers
      do not count. Matching counterparties or amounts remain independent signals. Case, punctuation and filler words such as
      "inc", "llc", "ach" and "payment" are ignored.
   c. Amount: the absolute transaction amount equals the remaining or original
      amount of an open item, or their positive difference (already paid), or
      the remaining or original amount of an active schedule.
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

from ..source_ids import source_id_variants
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
        if source_id and source_id_variants(source_id) & state["sources"]:
            return True
        return matches_open_records(txn, state["records"])


    return check


def needs_treatment(entity: Any, txn: Mapping[str, Any]) -> bool:
    """True when this bank or card transaction must be held for an explicit accrual treatment."""
    return treatment_checker(entity)(txn)


def matches_open_records(txn: Mapping[str, Any], records: list[tuple[str, Mapping[str, Any]]]) -> bool:
    """Conservative name/reference or amount match shared by import and cutover review."""
    amount = _money(txn.get("amount"))
    txn_words = _words(txn.get("counterparty"), txn.get("description"))
    tokens = frozenset(re.findall(r"[a-z0-9]+", " ".join(str(txn.get(k) or "") for k in ("counterparty", "description")).lower()))
    text = " ".join(str(txn.get(k) or "") for k in ("counterparty", "description")).lower()
    # Strip dates and check numbers before looking for a document-local numeric reference.
    reference_text = re.sub(r"\b\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}\b", " ", text)
    reference_text = re.sub(r"\b(?:check|cheque)\s*(?:(?:no\.?|number)\s*)?#?\s*\d+\b", " ", reference_text)
    for table, row in records:
        party_words = _words(row.get("counterparty"))
        party_match = bool(party_words and party_words <= txn_words)
        if party_match:
            return True
        reference = tuple(token for token in re.findall(r"[a-z0-9]+", str(row.get("reference") or "").lower())
                          if token not in _FILLER)
        # Match whole tokens (1001 does not match 10010). A purely numeric
        # reference needs a document or party signal; arbitrary short numbers
        # in unrelated bank descriptions are not identifiers by themselves.
        numeric = bool(reference) and all(token.isdigit() for token in reference)
        if numeric:
            number = r"\s+".join(re.escape(token) for token in reference)
            marker = r"(?:\b(?:invoice|inv|bill)\b[.: -]*(?:(?:no\.?|number|#)\s*)?|#\s*|\bno\.\s*)"
            if re.search(marker + number + r"(?![a-z0-9]|[-/])", reference_text):
                return True
        elif reference and set(reference) <= tokens:
            return True
        if amount is not None and amount > 0:
            remaining = _money(row.get("remaining_amount"))
            candidates = [remaining]
            if table == "schedules":
                candidates.append(_money(row.get("amount")))
            else:
                original = _money(row.get("original_amount"))
                candidates.append(original)
                if original is not None and remaining is not None and original > remaining:
                    candidates.append(original - remaining)
            if amount in candidates:
                return True
    return False
