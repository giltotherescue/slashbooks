"""Derived cash-basis profit and loss for accrual books.

The books stay on the accrual basis. This module reads the posted entries and the
accrual supporting records (items, allocations, schedules, estimate journals) and
derives the profit and loss as if income and expense were recognized when money
moves. Nothing is written, published or locked.

How it works
------------
Registered control accounts ("Q" below) are the accounts that carry the timing
difference between accrual and cash: the accounts of receivable, payable,
accrual and credit items, of prepaid and deferred revenue schedules, and of
estimate journals.

* An entry that does not touch a Q account is included as recorded.
* An entry that touches a Q account never shows its income or expense lines.
  Instead its cash movement ``N`` (the postings to accounts that are neither
  income, expense nor Q, for example the bank) is placed in the cash view by
  the rule for that kind of event: an invoice or bill settlement, a schedule
  funding or refund, or a deposit.
* Applying a received deposit or a prepayment to an invoice or bill moves the
  amount from the "not yet applied" line to the invoice or bill lines.

Because ``cash net income = accrual net income - sum of Q postings``, the report
proves itself: the reconciling items are read from the ledger postings of the Q
accounts and must equal accrual net income minus cash net income to the cent.
If they do not, the report fails and no figure is shown.

All arithmetic uses integer cents or ``Decimal``. No floats.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

CASH_VIEW_LABEL = "Cash basis (derived from accrual books)"
CARD_RULE = "A credit card charge counts as paid on the charge date (cash-basis tax rule). Paying the card balance later is not an expense."

OPENING_RECEIVABLES = "Collected from opening receivables"
OPENING_PAYABLES = "Paid on opening payables"
UNAPPLIED_CUSTOMER = "Customer payments not yet applied to an invoice"
UNAPPLIED_SUPPLIER = "Supplier prepayments not yet applied to a bill"
UNCLASSIFIED = "Unclassified cash movements (review needed)"

_K_OPENING_IN, _K_OPENING_OUT = "~opening_receivables", "~opening_payables"
_K_UNAPPLIED_IN, _K_UNAPPLIED_OUT = "~unapplied_customer", "~unapplied_supplier"
_K_UNCLASSIFIED = "~unclassified"
_SPECIAL_LABELS = {_K_OPENING_IN: OPENING_RECEIVABLES, _K_OPENING_OUT: OPENING_PAYABLES,
                   _K_UNAPPLIED_IN: UNAPPLIED_CUSTOMER, _K_UNAPPLIED_OUT: UNAPPLIED_SUPPLIER,
                   _K_UNCLASSIFIED: UNCLASSIFIED}

_TIMING_SUBTYPES = {"accrued_liabilities", "prepaid", "deferred_revenue", "receivable", "payable", "customer_deposits"}
NONCASH_HEADING = "Entries that may not be cash"

_CASH_LIKE = ("Assets:Bank:", "Assets:Cash", "Assets:Processors:", "Liabilities:CreditCard")

_GROUP_LABELS = {
    "receivables": "Receivables: invoices recorded and not yet collected",
    "payables": "Payables: bills recorded and not yet paid",
    "prepaid": "Prepaid expenses: paid and not yet used",
    "deferred_revenue": "Deferred revenue: received and not yet earned",
    "accrued": "Accrued liabilities and estimates",
    "customer_deposits": "Customer deposits and credits held",
    "writeoffs": "Write-offs, credits and true-ups (no cash)",
    "offsets": "Credits and deposits applied to invoices and bills (no cash)",
    "opening": "Opening receivables collected and opening payables paid",
    "other_control": "Other registered control accounts",
}
_SUBTYPE_GROUPS = {"receivable": "receivables", "payable": "payables", "prepaid": "prepaid",
                   "deferred_revenue": "deferred_revenue", "accrued_liabilities": "accrued",
                   "customer_deposits": "customer_deposits"}

METHOD = {
    "label": CASH_VIEW_LABEL,
    "summary": "Income and expense are recognized when money moves. The books stay on the accrual basis and nothing is published or locked.",
    "rules": [
        {"id": "invoices_and_bills", "rule": "An invoice or bill (receivable or payable item) is income or expense when cash settles it, on the settlement date, split across the item's lines in proportion to the amount settled."},
        {"id": "rounding", "rule": "Each line's cumulative share is floored to the cent and the last line takes the remaining cents. When an item is fully settled by cash, the cumulative amount per line equals the line amount exactly."},
        {"id": "no_cash_adjustments", "rule": "Write-offs, credit memos, true-ups and credit offsets move no cash, so they have no effect on the cash view."},
        {"id": "deposits", "rule": "Cash received as a deposit or overpayment is income on the receipt date on the line 'Customer payments not yet applied to an invoice'. Applying it to an invoice moves it from that line to the invoice's lines. A supplier prepayment works the same way on the expense side."},
        {"id": "prepaid", "rule": "A prepaid schedule is expense in full on the funding date. Its monthly recognition entries are ignored."},
        {"id": "deferred", "rule": "A deferred revenue schedule is income in full on the receipt date. Its recognition entries are ignored."},
        {"id": "refunds", "rule": "Cash refunded for a prepaid or deferred schedule reverses the expense or income on the refund date."},
        {"id": "credit_card", "rule": "A credit card charge counts as paid on the charge date (cash-basis tax rule). Paying the card balance later is not an expense."},
        {"id": "depreciation", "rule": "Depreciation is shown as recorded, as in QuickBooks cash reports. The equipment purchase stays an asset."},
        {"id": "estimates", "rule": "Accrued estimates (journals with a reversal date or estimate basis, and accrual items), their reversals and their true-ups are excluded. The real bill follows the invoice and bill rule."},
        {"id": "plain_journals", "rule": "Journals and bank transactions that touch no registered control account are included as recorded. A hand-built accrual in an account that has no registered item, schedule or estimate journal is also included as recorded."},
        {"id": "opening_items", "rule": "Opening receivables and payables carry no income or expense account. Their collection or payment is shown on the lines 'Collected from opening receivables' and 'Paid on opening payables' with no category."},
        {"id": "unclassified", "rule": "Cash that moves through a registered control account with no item, schedule or deposit to explain it is shown on the line 'Unclassified cash movements (review needed)' and listed in the report issues."},
    ],
    "tie_out": "accrual net income - cash net income = sum of the reconciling items. Each reconciling item is the net change of one group of registered control accounts, read from the ledger postings.",
}


class CashViewError(ValueError):
    """The cash view does not tie to the accrual books, so no figure is shown."""


def _cents(value: Any) -> int:
    return int((Decimal(str(value)) * 100).to_integral_value())


def _dec(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(Decimal("0.01"))


def _is_ie(account: str) -> bool:
    return account.startswith(("Income:", "Expenses:"))


def _item_sign(item: dict) -> int:
    if item.get("kind") == "credit":
        return 1 if item.get("direction") == "debit" else -1
    return 1 if str(item.get("account", "")).startswith("Assets:") else -1


def _load_entries(conn: Any, to_date: date) -> list[dict]:
    """All entries through ``to_date`` with their postings and metadata, in posting order."""
    base = ("SELECT e.id, e.date, e.source_id, e.narration, {meta}, p.account, p.amount FROM entries e "
            "JOIN postings p ON p.entry_id = e.id WHERE e.date <= ? ORDER BY e.date, e.id, p.id")
    try:
        rows = conn.execute(base.format(meta="e.metadata_json"), (to_date.isoformat(),)).fetchall()
    except sqlite3.OperationalError:
        rows = conn.execute(base.format(meta="'[]'"), (to_date.isoformat(),)).fetchall()
    entries: dict[int, dict] = {}
    for entry_id, day, source_id, narration, metadata, account, amount in rows:
        entry = entries.get(entry_id)
        if entry is None:
            try:
                meta = {str(k): str(v) for k, v in json.loads(metadata or "[]")}
            except (TypeError, ValueError):
                meta = {}
            entry = entries[entry_id] = {"id": entry_id, "date": day, "source_id": source_id or "",
                                         "narration": narration or "", "meta": meta, "postings": []}
        entry["postings"].append((account, Decimal(amount)))
    return list(entries.values())


def _registered_accounts(items: list[dict], schedules: list[dict], intents: list[dict], entries: list[dict]) -> tuple[set[str], set[str]]:
    """Return (control accounts Q, source ids of estimate journals)."""
    control = {i["account"] for i in items if i.get("account")}
    control |= {s["account"] for s in schedules if s.get("kind") in {"prepaid", "deferred"} and s.get("account")}
    reversing = {r.get("original_id") for r in intents if r.get("kind") == "scheduled_reversal"}
    estimates = set()
    for entry in entries:
        original = entry["source_id"][len("accrual:"):] if entry["source_id"].startswith("accrual:") else None
        if "estimate-basis" in entry["meta"] or (original and original in reversing):
            estimates.add(entry["source_id"])
            control |= {a for a, _ in entry["postings"]
                        if a.startswith(("Assets:", "Liabilities:")) and not a.startswith(_CASH_LIKE)}
    return control, estimates


class _ItemLines:
    """Weights that split an item's cash across its income or expense lines."""

    def __init__(self, items: dict[str, dict], entries_by_source: dict[str, dict]):
        self.items, self.entries_by_source, self._cache = items, entries_by_source, {}

    def weights(self, item: dict) -> list[tuple[str, int]]:
        if item["id"] in self._cache:
            return self._cache[item["id"]]
        origin, sign = item.get("origin", {}), _item_sign(item)
        if origin.get("mode") == "opening":
            result: list[tuple[str, int]] = []
        elif origin.get("lines"):
            result = [(row["account"], _cents(row["amount"])) for row in origin["lines"]]
        elif origin.get("counter_account"):
            result = [(origin["counter_account"], _cents(item["original_amount"]))]
        else:
            source = self.entries_by_source.get(origin.get("source_id", ""))
            lines: dict[str, int] = {}
            for account, amount in (source or {"postings": []})["postings"]:
                if _is_ie(account):
                    lines[account] = lines.get(account, 0) + _cents(-sign * amount)
            result = [(a, w) for a, w in lines.items() if w]
        if sum(w for _, w in result) == 0:
            result = []
        self._cache[item["id"]] = result
        return result


def _cumulative(weights: list[tuple[str, int]], settled: int) -> list[int]:
    """Cumulative cents per line after ``settled`` cents of cash: floor each share, residual on the last line."""
    total = sum(w for _, w in weights)
    shares = [w * settled // total for _, w in weights[:-1]]
    shares.append(settled - sum(shares))
    return shares


def _allocation_events(allocations: list[dict], items: dict[str, dict], lines: _ItemLines,
                       cash_backed: set[str]) -> dict[str, list[tuple[str, Decimal]]]:
    """Net-income events per allocation id: [(account or special key, net income cents as Decimal)].

    Cash that settles an item is recognized once, in proportion to the settled amount.
    """
    settled: dict[str, int] = defaultdict(int)
    events: dict[str, list[tuple[str, int]]] = {}
    for allocation in sorted(allocations, key=lambda a: (a.get("date", ""), a["id"])):
        item = items.get(allocation.get("item_id", ""))
        if not item:
            continue
        kind, cents = allocation.get("kind"), _cents(allocation.get("amount", "0"))
        sign = _item_sign(item)
        if kind == "unapplied_receipt":
            signed = _cents(allocation["signed_amount"])
            events[allocation["id"]] = [(_K_UNAPPLIED_IN if signed > 0 else _K_UNAPPLIED_OUT, signed)]
            continue
        if kind == "settlement" and item.get("kind") == "credit":
            events[allocation["id"]] = [(_K_UNAPPLIED_IN if sign < 0 else _K_UNAPPLIED_OUT, sign * cents)]
            continue
        if kind == "settlement":
            out: list[tuple[str, int]]
            weights = lines.weights(item)
            if item.get("origin", {}).get("mode") == "opening":
                out = [(_K_OPENING_IN if sign > 0 else _K_OPENING_OUT, sign * cents)]
            elif not weights:
                out = [(_K_UNCLASSIFIED, sign * cents)]
            else:
                before = _cumulative(weights, settled[item["id"]])
                after = _cumulative(weights, settled[item["id"]] + cents)
                out = [(account, sign * (a - b)) for (account, _), a, b in zip(weights, after, before) if a != b]
            settled[item["id"]] += cents
            events[allocation["id"]] = out
            continue
        if kind == "application" and allocation.get("role") == "target" and allocation.get("linked_item_id") in cash_backed:
            weights = lines.weights(item)
            moved = sign * cents
            credit = items.get(allocation["linked_item_id"], {})
            key = _K_UNAPPLIED_IN if _item_sign(credit) < 0 else _K_UNAPPLIED_OUT
            if item.get("origin", {}).get("mode") == "opening":
                out = [(_K_OPENING_IN if sign > 0 else _K_OPENING_OUT, moved)]
            elif not weights:
                out = [(_K_UNCLASSIFIED, moved)]
            else:
                before = _cumulative(weights, settled[item["id"]])
                after = _cumulative(weights, settled[item["id"]] + cents)
                out = [(account, sign * (a - b)) for (account, _), a, b in zip(weights, after, before) if a != b]
            settled[item["id"]] += cents
            events[allocation["id"]] = [*out, (key, -moved)]
    return {k: [(a, _dec(c)) for a, c in v] for k, v in events.items()}  # type: ignore[return-value]


@dataclass(frozen=True)
class DerivedPosting:
    """One posting of the derived cash-basis ledger. ``amount`` is debit-positive, like a ledger posting.

    ``account`` is an income or expense account, or a derived line that has no account (it starts with
    ``~``: opening receivables, opening payables, unapplied customer or supplier payments, unclassified).
    ``rule`` names the mapping rule that produced it, and ``source_id`` the ledger entry it came from.
    """
    date: str
    account: str
    amount: Decimal
    rule: str
    source_id: str = ""


class CashLedger:
    """The cash-basis view of accrual books, derived once and queried by date range.

    This is the reusable layer. The profit and loss consumes it today; balance sheet, tax mapping,
    exports and questions can consume ``postings`` and ``balances`` later. Nothing here writes.
    """

    def __init__(self, postings: list[DerivedPosting], accrual: list[tuple[str, str, Decimal]],
                 reconciling: list[tuple[str, str, Decimal]], issues: list[dict[str, Any]]):
        self.postings, self._accrual, self._reconciling, self.issues = postings, accrual, reconciling, issues

    def balances(self, from_date: date, to_date: date) -> dict[str, Decimal]:
        """Net debit-positive cash-view postings per income, expense or derived account in the range."""
        start, end = from_date.isoformat(), to_date.isoformat()
        totals: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
        for posting in self.postings:
            if start <= posting.date <= end:
                totals[posting.account] += posting.amount
        return {a: v for a, v in totals.items() if v}

    def net_income(self, from_date: date, to_date: date) -> Decimal:
        return -sum(self.balances(from_date, to_date).values(), Decimal(0))

    def accrual_net_income(self, from_date: date, to_date: date) -> Decimal:
        start, end = from_date.isoformat(), to_date.isoformat()
        return -sum((n for d, _, n in self._accrual if start <= d <= end), Decimal(0))

    def tie_out(self, from_date: date, to_date: date) -> dict[str, Any]:
        """Accrual net income minus cash net income, explained by the net change of each control-account group."""
        start, end = from_date.isoformat(), to_date.isoformat()
        groups: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
        for day, group, amount in self._reconciling:
            if start <= day <= end:
                groups[group] += amount
        accrual, cash = self.accrual_net_income(from_date, to_date), self.net_income(from_date, to_date)
        reconciling = [{"key": key, "label": label, "amount": f"{groups.get(key, Decimal(0)):.2f}"} for key, label in _GROUP_LABELS.items()]
        total, difference = sum(groups.values(), Decimal(0)), accrual - cash
        return {"formula": METHOD["tie_out"], "accrual_net_income": f"{accrual:.2f}", "cash_net_income": f"{cash:.2f}",
                "difference": f"{difference:.2f}", "reconciling_items": reconciling, "reconciling_total": f"{total:.2f}",
                "ties": difference == total}


def derive(conn: Any, through: date) -> CashLedger:
    """Derive the cash-basis ledger from the accrual books for every entry dated on or before ``through``."""
    from ..accrual.accounts import _subtype, _usage_subtypes
    from ..accrual.reports import _records
    items_list, schedules = _records(conn, "items"), _records(conn, "schedules")
    allocations, intents = _records(conn, "allocations"), _records(conn, "intents")
    items = {i["id"]: i for i in items_list}
    entries = _load_entries(conn, through)
    entries_by_source = {e["source_id"]: e for e in entries if e["source_id"]}
    control, _ = _registered_accounts(items_list, schedules, intents, entries)
    usage = _usage_subtypes({"items": items_list, "schedules": schedules})

    # A deposit or prepayment is cash-backed when cash created it. A credit memo is not.
    cash_backed = {a["item_id"] for a in allocations if a.get("kind") == "unapplied_receipt"}
    for item in items_list:
        if item.get("kind") == "credit" and item.get("origin", {}).get("mode") == "existing":
            source = entries_by_source.get(item["origin"].get("source_id", ""))
            if source and any(not _is_ie(a) and a not in control for a, _ in source["postings"]):
                cash_backed.add(item["id"])
    events = _allocation_events(allocations, items, _ItemLines(items, entries_by_source), cash_backed)
    settlements_by_source: dict[str, list[dict]] = defaultdict(list)
    for allocation in allocations:
        if allocation.get("kind") in {"settlement", "unapplied_receipt"} and allocation.get("source_id"):
            settlements_by_source[allocation["source_id"]].append(allocation)
    schedule_by_id = {s["id"]: s for s in schedules if s.get("kind") in {"prepaid", "deferred"}}
    schedule_by_source = {s.get("origin", {}).get("source_id"): s for s in schedule_by_id.values()}
    credit_by_source = {i["origin"]["source_id"]: i for i in items_list
                        if i.get("id") in cash_backed and i.get("origin", {}).get("mode") == "existing"
                        and i.get("origin", {}).get("source_id")}

    postings: list[DerivedPosting] = []
    accrual: list[tuple[str, str, Decimal]] = []
    reconciling: list[tuple[str, str, Decimal]] = []
    issues: list[dict[str, Any]] = []
    applied: set[str] = set()

    def emit(day: str, account: str, amount: Decimal, rule: str, source: str = "") -> None:
        if amount:
            postings.append(DerivedPosting(day, account, amount, rule, source))

    def add_events(allocation_id: str, day: str, rule: str, source: str) -> Decimal:
        """Place an allocation's net income events. Returns the net income placed."""
        rows = events.get(allocation_id, [])
        applied.add(allocation_id)
        for account, net in rows:
            emit(day, account, -net, rule, source)
        return sum((n for _, n in rows), Decimal(0))

    def unclassified(entry: dict, amount: Decimal, why: str) -> None:
        emit(entry["date"], _K_UNCLASSIFIED, -amount, "unclassified", entry["source_id"])
        issues.append({"code": "cash_view_unclassified", "date": entry["date"], "amount": f"{amount:.2f}",
                       "refs": [entry["source_id"]] if entry["source_id"] else [],
                       "message": f"{entry['date']} {entry['narration'] or 'entry'}: {why}"})

    for entry in entries:
        day, source, entry_postings = entry["date"], entry["source_id"], entry["postings"]
        for account, amount in entry_postings:
            if _is_ie(account):
                accrual.append((day, account, amount))
        touched = [(a, n) for a, n in entry_postings if a in control]
        if not touched:
            for account, amount in entry_postings:
                if _is_ie(account):
                    emit(day, account, amount, "as_recorded", source)
            timing = [a for a, _ in entry_postings if not _is_ie(a) and not a.startswith(_CASH_LIKE)
                      and (_subtype(a, usage) in _TIMING_SUBTYPES or "accrued" in a.lower())]
            effect = -sum((n for a, n in entry_postings if _is_ie(a)), Decimal(0))
            if timing and effect:
                issues.append({"code": "cash_view_noncash_entry", "date": day, "amount": f"{abs(effect):.2f}",
                               "accounts": sorted({a for a, _ in entry_postings if a in timing or _is_ie(a)}),
                               "description": entry["narration"],
                               "message": f"{day} {entry['narration'] or 'entry'}: {abs(effect):.2f} of {'income' if effect > 0 else 'expense'} "
                                          f"is included as recorded but may not be cash ({', '.join(sorted(set(timing)))} has no registered item, schedule or estimate journal)."})
            continue
        for account, amount in touched:
            if source.startswith("adjustment:"):
                group = "writeoffs"
            elif source.startswith("offset:"):
                group = "offsets"
            else:
                group = _SUBTYPE_GROUPS.get(_subtype(account, usage), "other_control")
            reconciling.append((day, group, amount))
        cash = sum((n for a, n in entry_postings if not _is_ie(a) and a not in control), Decimal(0))
        if not cash:
            continue
        allocated = settlements_by_source.get(source, [])
        schedule = schedule_by_id.get(entry["meta"].get("schedule-id", "")) or schedule_by_source.get(source)
        credit = credit_by_source.get(source)
        if allocated:
            net = sum((add_events(a["id"], day, "settlement", source) for a in allocated), Decimal(0))
            if net != cash:
                unclassified(entry, cash - net, "cash from this payment is not matched to an invoice, bill or credit")
        elif schedule:
            emit(day, schedule["recognition_account"], -cash, "schedule_cash", source)
        elif credit:
            emit(day, _K_UNAPPLIED_IN if _item_sign(credit) < 0 else _K_UNAPPLIED_OUT, -cash, "deposit", source)
        else:
            unclassified(entry, cash, "cash moved through a registered control account with no item, schedule or deposit to explain it")

    # Applying a cash-backed deposit or prepayment moves it from the "not yet applied" line to the invoice or bill lines.
    for allocation in allocations:
        if allocation.get("kind") == "application" and allocation["id"] in events:
            add_events(allocation["id"], allocation.get("date", ""), "application", allocation.get("source_id", "") or allocation["id"])

    # Opening items are shown on their own lines, so they leave the account groups they came from.
    for allocation in allocations:
        item = items.get(allocation.get("item_id", ""))
        if allocation.get("kind") == "settlement" and item and item.get("origin", {}).get("mode") == "opening" and allocation["id"] in applied:
            moved = sum((n for _, n in events[allocation["id"]]), Decimal(0))
            reconciling.append((allocation["date"], _SUBTYPE_GROUPS.get(_subtype(item["account"], usage), "other_control"), moved))
            reconciling.append((allocation["date"], "opening", -moved))
    return CashLedger(postings, accrual, reconciling, issues)


def compute(conn: Any, from_date: date, to_date: date) -> dict[str, Any]:
    """Return the cash view of ``[from_date, to_date]`` or raise ``CashViewError`` if it does not tie."""
    ledger = derive(conn, to_date)
    tie_out = ledger.tie_out(from_date, to_date)
    if not tie_out["ties"]:
        raise CashViewError(
            f"The cash view does not tie to the accrual books for {from_date} to {to_date}: accrual net income {tie_out['accrual_net_income']} minus "
            f"cash net income {tie_out['cash_net_income']} is {tie_out['difference']}, but the reconciling items add up to {tie_out['reconciling_total']}. "
            "No cash-basis figures are shown. Run books accrual report --kind reconciliation to find the unexplained control balance.")
    start, end = from_date.isoformat(), to_date.isoformat()
    return {"balances": ledger.balances(from_date, to_date), "accrual_net_income": Decimal(tie_out["accrual_net_income"]),
            "cash_net_income": Decimal(tie_out["cash_net_income"]), "tie_out": tie_out,
            "issues": [i for i in ledger.issues if start <= i["date"] <= end]}


def profit_and_loss(entity_path: Path | str, from_date: date, to_date: date):
    """Cash-basis profit and loss derived from accrual books."""
    from ..accrual.reports import CoverageError, basis, financial_metadata, opening_coverage
    from .cache import open_cache
    from .statements import StatementResult, _render_account
    if from_date > to_date:
        raise ValueError("Report start date must be on or before end date.")
    entity_path = Path(entity_path)
    conn = open_cache(entity_path)
    try:
        if basis(conn) != "accrual":
            raise ValueError("Cash books keep no data for an accrual view: there are no invoices, bills, prepaid or deferred records. "
                             "Run books report pnl without --basis, or with --basis cash.")
        opening = opening_coverage(conn)
        cutover = opening.get("cutover_date") if opening else None
        if not cutover:
            try:
                from ..entity import load_entity
                cutover = load_entity(entity_path).entity_config.get("cutover_date") or None
            except Exception:
                cutover = None
        if cutover and from_date.isoformat() < cutover:
            raise CoverageError(f"A cash-basis view is not available before the accrual cutover. The books hold no cash-basis detail before {cutover}.",
                                cutover, "from")
        metadata = financial_metadata(conn, "pnl", from_date, to_date)
        result = compute(conn, from_date, to_date)
    finally:
        conn.close()
    balances = result["balances"]
    income = [{"label": _render_account(a), "amount": -b, "indent": 1} for a, b in sorted(balances.items()) if a.startswith("Income:")]
    expense = [{"label": _render_account(a), "amount": b, "indent": 1} for a, b in sorted(balances.items()) if a.startswith("Expenses:")]
    for key in (_K_UNAPPLIED_IN, _K_OPENING_IN):
        if balances.get(key):
            income.append({"label": _SPECIAL_LABELS[key], "amount": -balances[key], "indent": 1, "special": key[1:]})
    for key in (_K_UNAPPLIED_OUT, _K_OPENING_OUT):
        if balances.get(key):
            expense.append({"label": _SPECIAL_LABELS[key], "amount": balances[key], "indent": 1, "special": key[1:]})
    total_income = sum((r["amount"] for r in income), Decimal("0.00"))
    total_expense = sum((r["amount"] for r in expense), Decimal("0.00"))
    sections = [{"label": "Revenue", "rows": income, "total": total_income, "total_label": "Total Revenue"},
                {"label": "Expenses", "rows": expense, "total": total_expense, "total_label": "Total Expenses"}]
    if balances.get(_K_UNCLASSIFIED):
        amount = -balances[_K_UNCLASSIFIED]
        sections.append({"label": "Needs review", "rows": [{"label": UNCLASSIFIED, "amount": amount, "indent": 1, "special": "unclassified"}],
                         "total": amount, "total_label": "Total unclassified"})
    metadata = {**metadata, "basis": "cash", "books_basis": "accrual", "derived_from": "accrual", "view": "cash",
                "view_label": CASH_VIEW_LABEL, "method": METHOD, "tie_out": result["tie_out"]}
    metadata["unresolved_issues"] = [*metadata.get("unresolved_issues", []), *result["issues"]]
    noncash_total, noncash_rows = noncash_summary(result["issues"])
    metadata["noncash_entries_total"] = f"{noncash_total:.2f}"
    metadata["noncash_entries_count"] = len(noncash_rows)
    return StatementResult(kind="pnl", from_date=from_date, to_date=to_date, sections=sections,
                           totals={"net_income": result["cash_net_income"]}, meta=metadata)


def noncash_summary(issues: list[dict[str, Any]]) -> tuple[Decimal, list[dict[str, Any]]]:
    rows = [i for i in issues if i.get("code") == "cash_view_noncash_entry"]
    return sum((Decimal(i["amount"]) for i in rows), Decimal("0.00")), rows


def noncash_lines(issues: list[dict[str, Any]]) -> list[str]:
    total, rows = noncash_summary(issues)
    if not rows:
        return []
    lines = ["", NONCASH_HEADING, "-" * 40]
    for row in rows:
        lines.append(f"  {row['date']}  {row['amount']:>12}  {row['description'] or 'entry'}  ({', '.join(row['accounts'])})")
    lines.append("  Remedy: record such an accrual as an estimate journal (reversal_date or estimate_basis) or an accrual item, so the cash view excludes it.")
    return lines


def tie_out_lines(tie_out: dict[str, Any]) -> list[str]:
    """Plain-text footer for the tie-out."""
    lines = ["", "Credit cards: " + CARD_RULE, "", "Tie-out to the accrual books",
             f"  Accrual net income                       {Decimal(tie_out['accrual_net_income']):>14,.2f}",
             f"  Cash net income                          {Decimal(tie_out['cash_net_income']):>14,.2f}",
             f"  Difference                               {Decimal(tie_out['difference']):>14,.2f}",
             "  The difference is explained by these changes in the period:"]
    shown = [r for r in tie_out["reconciling_items"] if Decimal(r["amount"])]
    for row in shown or [{"label": "No timing differences", "amount": "0.00"}]:
        lines.append(f"    {row['label']:<52} {Decimal(row['amount']):>12,.2f}")
    lines.append(f"  Reconciling items total                  {Decimal(tie_out['reconciling_total']):>14,.2f}  (ties to the cent)")
    return lines
