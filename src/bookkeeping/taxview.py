"""Tax outputs that follow the company's tax method, not the recorded books.

Two entity.json keys control it:

``tax_method``
    ``"cash"`` or ``"accrual"``: the method the company files its tax return on.
    Absent means unknown, and tax outputs read the recorded books as before.
``tax_method_view``
    ``true`` turns the tax-method view on. Without it every tax output is the
    same as before, even when ``tax_method`` is set. A person sets both keys with
    ``books entity set-tax-method`` (preview, then ``--apply``).

When the view is on, ``books tax map`` and ``books tax export`` (and the tax
Read Me, manifest and workbook) read:

* cash method, accrual capability 1: the cash view derived from the accrual
  records (``reports.cashview``). Receivables, payables, accrued and deferred
  balances leave the trial balance; one computed equity row keeps it balanced.
* cash method, cash books (capability 0): the recorded books, which are cash books.
* accrual method: the recorded books, refused before the accrual tracking start
  or for a partial imported summary period.

The view never falls back. If the chosen view cannot be computed for the period,
the command refuses and names the reason (``CoverageError`` or ``CashViewError``).

A prepaid schedule has a ``tax_treatment`` (``accrual.schedules``). A cash-method
filer deducts ``expense_when_paid`` schedules on payment and ``capitalize``
schedules as they are used. The tax export lists every difference that a
preparer must know about in ``flags``. Nothing here computes tax.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Mapping

TAX_METHOD_KEY = "tax_method"
TAX_VIEW_KEY = "tax_method_view"
TAX_METHODS = ("cash", "accrual")
ADJUSTMENT_ACCOUNT = "Equity:Accrual-To-Cash-Adjustment"
_ZERO = Decimal("0.00")
_EARLIEST = date(1900, 1, 1)

# Derived cash-view lines that carry no ledger account get a stable account name in the tax trial balance.
DERIVED_ACCOUNTS = {
    "~opening_receivables": "Income:Opening-Receivables-Collected",
    "~unapplied_customer": "Income:Customer-Payments-Not-Applied",
    "~opening_payables": "Expenses:Opening-Payables-Paid",
    "~unapplied_supplier": "Expenses:Supplier-Prepayments-Not-Applied",
    "~unclassified": "Expenses:Unclassified-Cash-Movements",
}

LABELS = {
    "derived": ("Cash method (tax): cash view derived from the accrual records. Income when received, expenses when paid; "
                "capitalized prepaid expenses are deducted as used. Computed by the Slashbooks engine"),
    "recorded_cash": "Cash method (tax): the recorded books are cash books. Book balances computed by the Slashbooks engine",
    "accrual": "Accrual method (tax): accrual view of the recorded books. Book balances computed by the Slashbooks engine",
}


def tax_method(config: Mapping[str, Any]) -> str | None:
    value = config.get(TAX_METHOD_KEY)
    if value is None:
        return None
    if value not in TAX_METHODS:
        raise ValueError(f"entity.json {TAX_METHOD_KEY} must be cash or accrual. Run books entity set-tax-method to fix it.")
    return value


def view_enabled(config: Mapping[str, Any]) -> bool:
    value = config.get(TAX_VIEW_KEY, False)
    if not isinstance(value, bool):
        raise ValueError(f"entity.json {TAX_VIEW_KEY} must be true or false. Run books entity set-tax-method to fix it.")
    return value


def active_method(config: Mapping[str, Any]) -> str | None:
    """The tax method that tax outputs follow, or None for the old behaviour (flag off or method unknown)."""
    method = tax_method(config)
    enabled = view_enabled(config)
    if enabled and method is None:
        raise ValueError("The tax-method view is on but no tax method is recorded. Run books entity set-tax-method.")
    return method if enabled else None


@dataclass
class TaxView:
    tax_method: str
    view: str            # "cash" or "accrual"
    source: str          # "derived" (cash view of accrual records) or "recorded"
    label: str
    tracking_start: str | None = None
    capitalized: dict[str, str] = field(default_factory=dict)   # schedule id -> "recorded" or "not_recorded"
    refundable: dict[str, str] = field(default_factory=dict)    # deposit id -> "schedules" or "items" (recorded refundable)
    ledger: Any = None
    control: set[str] = field(default_factory=set)
    fiscal_year_start: str = "01-01"

    def stamp(self) -> dict[str, Any]:
        return {"tax_method": self.tax_method, "view": self.view, "source": self.source, "label": self.label, "fiscal_year_start": self.fiscal_year_start}


def _records(conn: Any, table: str) -> list[dict[str, Any]]:
    from .accrual.reports import _records as records
    return records(conn, table)


def plan(conn: Any, config: Mapping[str, Any], from_date: date, to_date: date) -> TaxView | None:
    """Choose the view for a tax output, or return None when the tax-method view is off.

    Refuses (fail closed) when the chosen view cannot be computed for the period.
    """
    method = active_method(config)
    if method is None:
        return None
    from .fiscal import validate_start
    fiscal_start = validate_start(config.get("fiscal_year_start", "01-01"))
    from .accrual.reports import CoverageError, coverage_metadata, opening_coverage
    from .accrual.scope import DISCIPLINE_ALL, DISCIPLINE_OFF, DISCIPLINE_PER_ACCOUNT, capability, discipline, tracking_start
    kind = discipline(config)
    started = tracking_start(config)
    if method == "accrual":
        if kind == DISCIPLINE_OFF:
            raise CoverageError("The tax method is accrual, but these books keep no accrual records, so the accrual view is not available. "
                                "Turn accrual on with books accrual enable, or set the tax method to cash. Nothing was exported.", None, "from")
        coverage_metadata(conn, "tax_handoff", from_date, to_date, view="accrual")
        label = LABELS["accrual"] + (f"; accrual figures start on {started}" if started else "")
        return TaxView("accrual", "accrual", "recorded", label, tracking_start=started, fiscal_year_start=fiscal_start)
    if kind == DISCIPLINE_OFF:
        coverage_metadata(conn, "tax_handoff", from_date, to_date, view="cash")
        return TaxView("cash", "cash", "recorded", LABELS["recorded_cash"], fiscal_year_start=fiscal_start)
    if capability(config) != 1:
        raise CoverageError("The tax method is cash, but these accrual books cannot produce a cash view (accrual capability is off). "
                            "Nothing was exported. Ask your accountant how to prepare the cash-method figures.", None, "from")
    coverage_metadata(conn, "tax_handoff", from_date, to_date, view="cash")
    opening = opening_coverage(conn)
    cutover = opening.get("cutover_date") if opening else None
    if not cutover and kind == DISCIPLINE_ALL:
        cutover = config.get("cutover_date") or None
    if cutover and from_date.isoformat() < cutover:
        raise CoverageError(f"The cash view for tax is not available before the accrual cutover {cutover}: the books hold no cash-basis detail "
                            f"before it (an imported or opening summary). Ask for a period that starts on or after {cutover}. Nothing was exported.",
                            cutover, "from")
    return _derived(conn, from_date, to_date, started if kind == DISCIPLINE_PER_ACCOUNT else None, fiscal_start)


def _derived(conn: Any, from_date: date, to_date: date, started: str | None, fiscal_year_start: str) -> TaxView:
    from .accrual.schedules import effective_deposit_kind, effective_tax_treatment, is_deposit
    from .reports import cashview
    from .reports.cache import iter_postings
    raw_total = sum((row[4] for row in iter_postings(conn, to_date=to_date)), _ZERO)
    if raw_total:
        raise cashview.CashViewError("The recorded trial balance does not balance. Nothing was exported; fix ledger integrity first.")
    schedules = _records(conn, "schedules")
    capitalized: dict[str, str] = {}
    for record in schedules:
        if record.get("kind") != "prepaid":
            continue
        treatment, source = effective_tax_treatment(record)
        if treatment == "capitalize":
            capitalized[record["id"]] = source
    # A deposit counts as refundable only when a person recorded it so. Unset means advance payment.
    refundable = {record["id"]: table for table, records in (("schedules", schedules), ("items", _records(conn, "items")))
                  for record in records if is_deposit(record, table) and effective_deposit_kind(record, table)[0] == "refundable"}
    kept = frozenset(capitalized) | {key for key, table in refundable.items() if table == "schedules"}
    ledger = cashview.derive(conn, to_date, kept, frozenset(key for key, table in refundable.items() if table == "items"))
    for start, end in ((_EARLIEST, to_date), (from_date, to_date)):
        tie = ledger.tie_out(start, end)
        if not tie["ties"]:
            raise cashview.CashViewError(
                f"The cash view for tax does not tie to the accrual books through {end}: accrual net income {tie['accrual_net_income']} "
                f"minus cash net income {tie['cash_net_income']} is {tie['difference']}, but the reconciling items add up to "
                f"{tie['reconciling_total']}. Nothing was exported. Run books accrual report --kind reconciliation to find the unexplained balance.")
    items, intents = _records(conn, "items"), _records(conn, "intents")
    control, _ = cashview._registered_accounts(items, schedules, intents, cashview._load_entries(conn, to_date))
    return TaxView("cash", "cash", "derived", LABELS["derived"], tracking_start=started, capitalized=capitalized,
                   refundable=refundable, ledger=ledger, control=set(control), fiscal_year_start=fiscal_year_start)


def derived_balances(conn: Any, view: TaxView, from_date: date, to_date: date) -> list[dict[str, Any]]:
    """Trial balance rows of the derived cash view, in the shape of ``taxmap.period_balances``."""
    from .reports.cache import iter_postings, list_accounts
    from .taxmap import PRIOR_EARNINGS_ACCOUNT
    start = from_date.isoformat()
    dropped = view.control
    opening: dict[str, Decimal] = {}
    ending: dict[str, Decimal] = {}
    activity: dict[str, Decimal] = {}
    prior = _ZERO
    accounts = {item["name"] for item in list_accounts(conn)
                if item["name"] not in dropped and item["name"].split(":")[0] not in {"Income", "Expenses"}}
    for row in iter_postings(conn, to_date=to_date):
        day, account, amount = str(row[0])[:10], row[3], row[4]
        if account.split(":")[0] in {"Income", "Expenses"} or account in dropped:
            continue
        accounts.add(account)
        ending[account] = ending.get(account, _ZERO) + amount
        if day < start:
            opening[account] = opening.get(account, _ZERO) + amount
    # Reconstruct only the capitalized schedules and the refundable deposits, including when their
    # account also holds other records. Never hide their assets or liabilities in equity.
    from datetime import timedelta
    from .accrual.reports import _managed_support
    for boundary, balances in ((from_date - timedelta(days=1), opening), (to_date, ending)):
        support_items, support_schedules = _managed_support(conn, boundary.isoformat())
        kept = [r for r in support_schedules if r["id"] in view.capitalized or view.refundable.get(r["id"]) == "schedules"]
        kept += [r for r in support_items if view.refundable.get(r["id"]) == "items"]
        for record in kept:
            account = record["account"]
            accounts.add(account)
            balances[account] = balances.get(account, _ZERO) + Decimal(record["signed_balance"])
    derived: set[str] = set()
    for posting in view.ledger.postings:
        if posting.date > to_date.isoformat():
            continue
        account = DERIVED_ACCOUNTS.get(posting.account, posting.account)
        if posting.account in DERIVED_ACCOUNTS:
            derived.add(account)
        if posting.date < start:
            prior += posting.amount
        else:
            activity[account] = activity.get(account, _ZERO) + posting.amount
    accounts |= {item["name"] for item in list_accounts(conn) if item["name"].split(":")[0] in {"Income", "Expenses"}}
    accounts |= set(activity)
    rows = []
    for account in sorted(accounts):
        root = account.split(":")[0]
        if root in {"Income", "Expenses"}:
            begin, balance = None, activity.get(account, _ZERO)
        else:
            begin, balance = opening.get(account, _ZERO), ending.get(account, _ZERO)
        row = {"account": account, "type": root, "beginning": begin, "balance": balance}
        if account in derived:
            row["computed"] = True
        rows.append(row)
    if prior != _ZERO:
        rows.append({"account": PRIOR_EARNINGS_ACCOUNT, "type": "Equity", "beginning": prior, "balance": prior, "computed": True})
    end_total = sum((r["balance"] for r in rows), _ZERO)
    begin_total = sum((r["beginning"] for r in rows if r["beginning"] is not None), _ZERO)
    if end_total or begin_total:
        rows.append({"account": ADJUSTMENT_ACCOUNT, "type": "Equity", "beginning": -begin_total, "balance": -end_total, "computed": True})
    return rows


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def flags(conn: Any, view: TaxView, from_date: date, to_date: date) -> list[dict[str, Any]]:
    """What the preparer must know about this view. Every flag has a code, a message and, where it applies, an amount."""
    start, end = from_date.isoformat(), to_date.isoformat()
    result: list[dict[str, Any]] = [{"code": "tax_method", "message": view.label + "."}]
    schedules = _records(conn, "schedules")
    depreciation = [s for s in schedules if s.get("kind") == "depreciation"]
    fixed = sorted({a for (a,) in conn.execute(
        "SELECT DISTINCT p.account FROM postings p JOIN entries e ON e.id=p.entry_id WHERE e.date BETWEEN ? AND ? AND p.account LIKE 'Assets:Fixed%'",
        (start, end))})
    if depreciation or fixed:
        result.append({"code": "depreciation_not_tax_adjusted", "accounts": fixed, "refs": sorted(s["id"] for s in depreciation),
                       "message": ("Fixed assets purchased stay on the balance sheet. Depreciation is book depreciation as recorded; "
                                   "depreciation not tax-adjusted (no Section 179, bonus or tax depreciation method). The preparer computes tax depreciation.")})
    if view.source != "derived":
        return result
    tie = view.ledger.tie_out(from_date, to_date)
    groups = {row["key"]: Decimal(row["amount"]) for row in tie["reconciling_items"]}
    differences = [{"key": row["key"], "label": row["label"], "cash_minus_accrual": _money(-Decimal(row["amount"]))}
                   for row in tie["reconciling_items"] if Decimal(row["amount"])]
    result.append({"code": "difference_from_accrual_view", "amount": _money(Decimal(tie["cash_net_income"]) - Decimal(tie["accrual_net_income"])),
                   "accrual_net_income": tie["accrual_net_income"], "cash_net_income": tie["cash_net_income"], "groups": differences,
                   "message": (f"Cash-method net income {tie['cash_net_income']} differs from the accrual view {tie['accrual_net_income']} by "
                               f"{_money(Decimal(tie['cash_net_income']) - Decimal(tie['accrual_net_income']))}. The groups list each difference.")})
    if groups.get("deferred_revenue") or groups.get("customer_deposits"):
        amount = -(groups.get("deferred_revenue", _ZERO) + groups.get("customer_deposits", _ZERO))
        result.append({"code": "deferred_revenue_on_receipt", "amount": _money(amount),
                       "message": f"Deferred revenue and customer deposits are income when received on the cash method: {_money(amount)} more income than the accrual view."
                                  + (" Deposits recorded as refundable are not included." if view.refundable else "")})
    if groups.get("writeoffs"):
        result.append({"code": "bad_debt_not_deductible", "amount": _money(groups["writeoffs"]),
                       "message": "Bad-debt write-offs, credits and true-ups are excluded: on the cash method, income that was never received is not deductible."})
    if groups.get("accrued"):
        result.append({"code": "accrued_liabilities_excluded", "amount": _money(groups["accrued"]),
                       "message": "Accrued payroll and other accrued liabilities are excluded until they are paid."})
    if groups.get("receivables") or groups.get("payables"):
        result.append({"code": "receivables_payables_excluded", "amount": _money(groups.get("receivables", _ZERO) + groups.get("payables", _ZERO)),
                       "message": "Unpaid invoices are not income and unpaid bills are not expenses until cash moves."})
    if any(Decimal(view.ledger.balances(from_date, to_date).get(k, _ZERO)) for k in ("~opening_receivables", "~opening_payables")):
        result.append({"code": "opening_items_on_receipt",
                       "message": ("Opening receivables collected and opening payables paid in the period are income and expense when cash moves. "
                                   "Confirm that no earlier return already reported them.")})
    for record in schedules:
        if record.get("id") in view.capitalized:
            source = view.capitalized[record["id"]]
            reason = "recorded tax treatment: capitalize" if source == "recorded" else "no tax treatment is recorded, so the books are followed"
            result.append({"code": "prepaid_capitalized", "refs": [record["id"]], "treatment_source": source,
                           "message": (f"Prepaid schedule {record.get('reference') or record['id']} stays on the balance sheet in the tax figures "
                                       f"and is deducted as it is used ({reason}).")})
    untreated = sorted(r["id"] for r in schedules if r.get("kind") == "prepaid" and "tax_treatment" not in r)
    if untreated:
        result.append({"code": "prepaid_tax_treatment_not_recorded", "refs": untreated,
                       "message": ("These prepaid schedules have no recorded tax treatment. The tax figures keep them on the balance sheet "
                                   "as the books record them. A person records the treatment with books accrual schedule tax-treatment.")})
    held = deposits(conn, view, to_date)
    for row in held:
        if row["deposit_kind"] == "refundable":
            result.append({"code": "deposit_refundable", "refs": [row["id"]],
                           "message": (f"Deposit {row['reference'] or row['id']} is recorded as refundable. The cash view treats it as "
                                       "a liability until it is applied to an invoice or forfeited, not as income on receipt.")})
    assumed = [row["id"] for row in held if row["deposit_kind_source"] == "assumed"]
    if assumed:
        result.append({"code": "deposit_kind_not_recorded", "refs": assumed,
                       "message": ("These deposits have no recorded deposit kind. The cash view treats them as advance payments, as income on receipt. "
                                   "Record the kind with books accrual schedule deposit-kind or books accrual item deposit-kind.")})
    issues = [i for i in view.ledger.issues if start <= i["date"] <= end]
    for issue in issues:
        result.append({"code": issue["code"], "amount": issue.get("amount"), "message": issue["message"]})
    result.append({"code": "adjustment_row", "accounts": [ADJUSTMENT_ACCOUNT],
                   "message": (f"{ADJUSTMENT_ACCOUNT} is computed. It removes the receivable, payable, deferred and accrued balances "
                               "that a cash-method return does not carry, so the trial balance stays balanced.")})
    return result


ASSUMED_ADVANCE_PAYMENT = "assumed advance payment"


def deposits(conn: Any, view: TaxView, to_date: date) -> list[dict[str, Any]]:
    """Every deferred schedule and customer deposit item that exists on ``to_date``, with its deposit kind.

    Data only. The kind is the recorded one, or advance payment ("assumed advance payment") when none is recorded.
    """
    if view.source != "derived":
        return []
    from .accrual.reports import _managed_support, opening_coverage, _origin_date
    from .accrual.schedules import effective_deposit_kind, is_deposit
    opening = opening_coverage(conn)
    support_items, support_schedules = _managed_support(conn, to_date.isoformat())
    # The kind comes from the current record, the one the cash-view math reads, not from a dated snapshot.
    current = {table: {r["id"]: r for r in _records(conn, table)} for table in ("schedules", "items")}
    rows = []
    for table, records in (("schedules", support_schedules), ("items", support_items)):
        for record in records:
            latest = current[table].get(record["id"], record)
            if not is_deposit(latest, table):
                continue
            kind, source = effective_deposit_kind(latest, table)
            rows.append({"id": record["id"], "record": table[:-1], "account": record["account"],
                         "date": _origin_date(record, opening), "amount": f"{Decimal(record.get('original_amount') or record['amount']):.2f}",
                         "balance_at_end": f"{abs(Decimal(record['signed_balance'])):.2f}",
                         "counterparty": record.get("counterparty") or "", "reference": record.get("reference") or "",
                         "deposit_kind": kind, "deposit_kind_source": source,
                         "note": ASSUMED_ADVANCE_PAYMENT if source == "assumed" else "recorded"})
    return sorted(rows, key=lambda r: (r["date"] or "", r["id"]))


def _entry_rows(conn: Any, to_date: date) -> list[tuple]:
    """(entry id, date, source_id, payee, metadata, account, amount) for postings through ``to_date``."""
    import sqlite3
    base = ("SELECT e.id, e.date, e.source_id, e.payee, {meta}, p.account, p.amount FROM entries e JOIN postings p ON p.entry_id = e.id "
            "WHERE e.date <= ? ORDER BY e.date, e.id, p.id")
    try:
        return conn.execute(base.format(meta="e.metadata_json"), (to_date.isoformat(),)).fetchall()
    except sqlite3.OperationalError:
        return conn.execute(base.format(meta="'[]'"), (to_date.isoformat(),)).fetchall()


_OPENING_META = ("accrual-opening", "accrual-tracking-opening")


def fixed_assets(conn: Any, view: TaxView, from_date: date, to_date: date) -> list[dict[str, Any]]:
    """The fixed-asset worksheet for the preparer: data only, no tax depreciation.

    One row per depreciation schedule that exists on ``to_date``, and one row per entry through ``to_date`` whose
    postings to ``Assets:Fixed`` accounts are a net debit and that no depreciation schedule records. Accounts whose
    name contains "Accumulated" and depreciation contra accounts are left out, so a move between two fixed-asset
    accounts is not a purchase. An opening-balance entry is marked ``source: opening``. Book depreciation is as recorded.
    """
    import json
    from .accrual.reports import _managed_support, opening_coverage, _origin_date
    opening = opening_coverage(conn)
    start, end = from_date.isoformat(), to_date.isoformat()
    schedules = [r for r in _managed_support(conn, end)[1] if r.get("kind") == "depreciation" and r.get("role") != "accumulated_depreciation"]
    all_depreciation = [r for r in _records(conn, "schedules") if r.get("kind") == "depreciation"]
    scheduled_ids = {r["id"] for r in all_depreciation}
    origin_sources = {(r.get("origin") or {}).get("source_id") for r in all_depreciation} - {None}
    contra = {r.get("contra_account") for r in all_depreciation} - {None}
    payees: dict[str, str] = {}
    entries: dict[int, dict[str, Any]] = {}
    for entry_id, day, source_id, payee, metadata, account, amount in _entry_rows(conn, to_date):
        if source_id and payee:
            payees.setdefault(source_id, payee)
        entry = entries.get(entry_id)
        if entry is None:
            try:
                meta = {str(k): str(v) for k, v in json.loads(metadata or "[]")}
            except (TypeError, ValueError):
                meta = {}
            entry = entries[entry_id] = {"date": str(day)[:10], "source_id": source_id or "", "payee": payee or "", "meta": meta, "fixed": {}}
        if account.startswith("Assets:Fixed") and account not in contra and "accumulated" not in account.lower():
            entry["fixed"][account] = entry["fixed"].get(account, _ZERO) + Decimal(amount)
    purchases = []
    for entry in entries.values():
        net = sum(entry["fixed"].values(), _ZERO)
        if net <= 0 or entry["source_id"] in origin_sources or entry["meta"].get("schedule-id") in scheduled_ids:
            continue
        account = max(entry["fixed"].items(), key=lambda pair: (pair[1], pair[0]))[0]
        is_opening = any(entry["meta"].get(key, "").lower() == "true" for key in _OPENING_META)
        purchases.append({"source": "opening" if is_opening else "purchase", "id": entry["source_id"], "account": account,
                          "placed_in_service": entry["date"], "cost": f"{net:.2f}", "vendor": entry["payee"], "reference": "",
                          "book_depreciation_to_date": "", "depreciation_schedule": "none"})
    rows = []
    for record in schedules:
        placed = _origin_date(record, opening)
        source_id = (record.get("origin") or {}).get("source_id")
        rows.append({"source": "depreciation_schedule", "id": record["id"], "account": record["account"], "placed_in_service": placed,
                     "cost": f"{Decimal(record['amount']):.2f}", "vendor": payees.get(source_id or "", ""),
                     "reference": record.get("reference") or "", "book_depreciation_to_date": f"{Decimal(record['recognized_amount']):.2f}",
                     "depreciation_schedule": record["id"]})
    rows += purchases
    for row in rows:
        row["in_period"] = start <= (row["placed_in_service"] or "") <= end
    return sorted(rows, key=lambda r: (r["placed_in_service"] or "", r["account"], r["id"]))


def readiness_notes(entity: Any) -> list[dict[str, Any]]:
    """A note (not a blocker) for each prepaid schedule without a recorded tax treatment, for a cash-method filer."""
    from .accrual.scope import capability
    if tax_method(entity.entity_config) != "cash" or capability(entity.entity_config) != 1:
        return []
    from .accrual.store import list_records
    from .ledger.store import LedgerStore, default_store_path
    path = default_store_path(entity.path)
    if not path.is_file():
        return []
    store = LedgerStore(path)
    missing = sorted(r["id"] for r in list_records(store, "schedules")
                     if r.get("kind") == "prepaid" and "tax_treatment" not in r)
    notes = []
    if missing:
        notes.append({"code": "schedule_tax_treatment_missing", "refs": missing,
                      "message": ("The company files on the cash method, and these prepaid schedules have no recorded tax treatment. "
                                  "The tax figures keep them on the balance sheet as the books record them. A person records the treatment "
                                  "with books accrual schedule tax-treatment.")})
    if active_method(entity.entity_config) == "cash":
        # Behind the tax-method view only, so a company without the view keeps its readiness output.
        from .accrual.schedules import is_deposit
        unset = sorted(r["id"] for table in ("schedules", "items") for r in list_records(store, table)
                       if is_deposit(r, table) and "deposit_kind" not in r)
        if unset:
            notes.append({"code": "deposit_kind_missing", "refs": unset,
                          "message": ("The tax view follows the cash method, and these deposits have no recorded deposit kind. "
                                      "The cash view treats them as advance payments, as income on receipt. Record the kind with "
                                      "books accrual schedule deposit-kind or books accrual item deposit-kind.")})
    return notes


# ---------------------------------------------------------------------------
# books entity set-tax-method (person-only; preview, then --apply)
# ---------------------------------------------------------------------------

SET_TAX_METHOD_KIND = "tax_method_set"
FLAG_EXPLANATION = (f"{TAX_VIEW_KEY} turns the tax-method view on. When it is on, books tax map and books tax export follow the tax method: "
                    "a cash-method filer with accrual records gets the cash view (unpaid invoices and bills are left out, prepaid expenses "
                    "follow their tax treatment), and an accrual-method filer gets the accrual view. When it is off, tax outputs read the "
                    "recorded books as before. Reports, entries and published statements never change.")


def _parse_switch(value: Any) -> bool | None:
    if value is None:
        return None
    if value in ("on", True):
        return True
    if value in ("off", False):
        return False
    raise ValueError("--tax-view must be on or off.")


def preview_set_tax_method(entity: Any, method: Any, tax_view: Any = None) -> dict[str, Any]:
    """Preview recording the tax method (and optionally the view flag). Writes nothing."""
    from .accrual.enable import _integrity
    from .accrual.intake import canonical_hash
    from .accrual.journals import JournalError
    from .accrual.scope import capability
    from .entity import load_entity
    from .ledger.store import LedgerStore, default_store_path
    entity = load_entity(entity.path)
    if method not in TAX_METHODS:
        raise JournalError("invalid_tax_method", "method must be cash or accrual.", "Use --method cash or --method accrual.")
    switch = _parse_switch(tax_view)
    config = entity.entity_config
    current_method, current_flag = tax_method(config), view_enabled(config)
    flag = current_flag if switch is None else switch
    _integrity(entity, "record the tax method")
    store = LedgerStore(default_store_path(entity.path))
    cap = capability(config)
    if method == "cash":
        source = "derived cash view of the accrual records" if cap == 1 else "recorded books (cash books)"
    else:
        source = "recorded books (accrual view)" if cap == 1 else "not available: the books keep no accrual records, so tax outputs refuse"
    warnings = []
    if method == "accrual" and cap != 1 and flag:
        warnings.append({"code": "accrual_view_unavailable",
                         "message": "With the view on, books tax map and books tax export refuse for these cash books until accrual is turned on."})
    body = {"schema_version": 1, "kind": SET_TAX_METHOD_KIND, "request": {"method": method, TAX_VIEW_KEY: flag},
            "changes": (current_method, current_flag) != (method, flag), "ledger_digest": store.content_digest(),
            "configuration_hash": canonical_hash(config),
            "effects": {"from": {"tax_method": current_method, TAX_VIEW_KEY: current_flag}, "to": {"tax_method": method, TAX_VIEW_KEY: flag},
                        "tax_outputs_read": source if flag else "recorded books, as before (the view is off)",
                        "entries_changed": 0, "period_locks_changed": 0, "published_statements_changed": False,
                        "flag": FLAG_EXPLANATION},
            "warnings": warnings}
    body["request_hash"] = canonical_hash({"kind": SET_TAX_METHOD_KIND, "request": body["request"]})
    body["plan_hash"] = canonical_hash(body)
    return body


def apply_set_tax_method(entity: Any, method: Any, tax_view: Any = None, plan_hash: str | None = None) -> dict[str, Any]:
    """Record the tax method. Repeating the same values returns already_applied."""
    import json
    import os
    from .accrual.enable import _seal
    from .accrual.journals import JournalError
    from .entity import load_entity
    from .ledger.importer import _entity_write_lock
    from .ledger.store import LedgerStore, default_store_path
    with _entity_write_lock(entity.path):
        fresh = preview_set_tax_method(entity, method, tax_view)
        if plan_hash is not None and plan_hash != fresh["plan_hash"]:
            raise JournalError("stale_preview", "The company changed after the preview.", "Run the preview again.")
        request = fresh["request"]
        if not fresh["changes"]:
            return {"status": "already_applied", "kind": SET_TAX_METHOD_KIND, **request, "request_hash": fresh["request_hash"]}
        entity = load_entity(entity.path)
        store = LedgerStore(default_store_path(entity.path))
        config = {**entity.entity_config, TAX_METHOD_KEY: request["method"], TAX_VIEW_KEY: request[TAX_VIEW_KEY]}
        temp = entity.path / "entity.json.tax-method.tmp"
        temp.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        try:
            with store.transaction(immediate=True) as conn:
                store.append_audit_event("tax-method-set", {"from": fresh["effects"]["from"], "to": fresh["effects"]["to"],
                                                             "plan_hash": fresh["plan_hash"]}, conn)
                _seal(store, conn, "entity-set-tax-method")
                os.replace(temp, entity.path / "entity.json")
        finally:
            temp.unlink(missing_ok=True)
        return {"status": "applied", "kind": SET_TAX_METHOD_KIND, **request, "from": fresh["effects"]["from"],
                "request_hash": fresh["request_hash"], "plan_hash": fresh["plan_hash"], "entries_changed": 0,
                "published_statements_changed": False}


# ---------------------------------------------------------------------------
# books entity set-tax-profile (person-only; preview, then --apply)
# ---------------------------------------------------------------------------

PRIOR_RETURN_METHOD_KEY = "prior_return_method"
ENTITY_TYPE_KEY = "entity_type"
PRIOR_RETURN_METHODS = ("cash", "accrual", "unknown", "none")
ENTITY_TYPES = ("sole_proprietor", "single_member_llc", "partnership", "s_corp", "c_corp", "nonprofit", "other", "unknown")
PROFILE_FIELDS = {PRIOR_RETURN_METHOD_KEY: PRIOR_RETURN_METHODS, ENTITY_TYPE_KEY: ENTITY_TYPES}
SET_TAX_PROFILE_KIND = "tax_profile_set"
PROFILE_NOTE = ("Records facts about the company for the tax preparer: the method of the last tax return filed "
                f"({PRIOR_RETURN_METHOD_KEY}; none for a first-year business) and the kind of business entity ({ENTITY_TYPE_KEY}). "
                "The tax export lists them. Slashbooks applies no tax rule to them: it does not decide, block or compute anything from them.")


def tax_profile(config: Mapping[str, Any]) -> dict[str, str]:
    """The recorded tax profile fields (only the ones that are set), validated."""
    result = {}
    for key, allowed in PROFILE_FIELDS.items():
        if key in config:
            if config[key] not in allowed:
                raise ValueError(f"entity.json {key} must be one of {', '.join(allowed)}. Run books entity set-tax-profile to fix it.")
            result[key] = config[key]
    return result


def preview_set_tax_profile(entity: Any, prior_return_method: Any = None, entity_type: Any = None) -> dict[str, Any]:
    """Preview recording the tax profile. Writes nothing."""
    from .accrual.enable import _integrity
    from .accrual.intake import canonical_hash
    from .accrual.journals import JournalError
    from .entity import load_entity
    from .ledger.store import LedgerStore, default_store_path
    entity = load_entity(entity.path)
    request = {}
    for key, value in ((PRIOR_RETURN_METHOD_KEY, prior_return_method), (ENTITY_TYPE_KEY, entity_type)):
        if value is None:
            continue
        if value not in PROFILE_FIELDS[key]:
            raise JournalError(f"invalid_{key}", f"{key} must be one of {', '.join(PROFILE_FIELDS[key])}.",
                               f"Use --{key.replace('_', '-')} with one of {', '.join(PROFILE_FIELDS[key])}.")
        request[key] = value
    if not request:
        raise JournalError("nothing_to_set", "Give --prior-return-method, --entity-type or both.",
                           "For example: books entity set-tax-profile PATH --prior-return-method cash --entity-type single_member_llc")
    config = entity.entity_config
    # Read the stored values without validating them, so this command can repair a bad value.
    current = {key: config[key] for key in PROFILE_FIELDS if key in config}
    invalid = sorted(key for key, value in current.items() if value not in PROFILE_FIELDS[key])
    _integrity(entity, "record the tax profile")
    store = LedgerStore(default_store_path(entity.path))
    target = {**current, **request}
    body = {"schema_version": 1, "kind": SET_TAX_PROFILE_KIND, "request": request,
            "changes": target != current, "ledger_digest": store.content_digest(),
            "configuration_hash": canonical_hash(config),
            "effects": {"from": current, "to": target, "entries_changed": 0, "period_locks_changed": 0,
                        "published_statements_changed": False, "note": PROFILE_NOTE}}
    if invalid:
        body["warnings"] = [{"code": "invalid_stored_value", "refs": invalid,
                             "message": "entity.json holds a value that is not allowed for " + ", ".join(invalid) + ". "
                                        + ("This change replaces it." if all(key in request for key in invalid)
                                           else "Give a valid value for it to repair it.")}]
    body["request_hash"] = canonical_hash({"kind": SET_TAX_PROFILE_KIND, "request": request})
    body["plan_hash"] = canonical_hash(body)
    return body


def apply_set_tax_profile(entity: Any, prior_return_method: Any = None, entity_type: Any = None,
                          plan_hash: str | None = None) -> dict[str, Any]:
    """Record the tax profile (person-only). Repeating the same values returns already_applied."""
    import json
    import os
    from .accrual.enable import _seal
    from .accrual.journals import JournalError
    from .entity import load_entity
    from .ledger.importer import _entity_write_lock
    from .ledger.store import LedgerStore, default_store_path
    from .preparation_runtime import is_preparation
    if is_preparation():
        raise JournalError("tax_profile_person_only", "Recording the tax profile requires the owner or tax preparer.")
    with _entity_write_lock(entity.path):
        fresh = preview_set_tax_profile(entity, prior_return_method, entity_type)
        if plan_hash is not None and plan_hash != fresh["plan_hash"]:
            raise JournalError("stale_preview", "The company changed after the preview.", "Run the preview again.")
        if not fresh["changes"]:
            return {"status": "already_applied", "kind": SET_TAX_PROFILE_KIND, **fresh["effects"]["to"], "request_hash": fresh["request_hash"]}
        entity = load_entity(entity.path)
        store = LedgerStore(default_store_path(entity.path))
        config = {**entity.entity_config, **fresh["request"]}
        temp = entity.path / "entity.json.tax-profile.tmp"
        temp.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        try:
            with store.transaction(immediate=True) as conn:
                store.append_audit_event("tax-profile-set", {"from": fresh["effects"]["from"], "to": fresh["effects"]["to"],
                                                              "plan_hash": fresh["plan_hash"]}, conn)
                _seal(store, conn, "entity-set-tax-profile")
                os.replace(temp, entity.path / "entity.json")
        finally:
            temp.unlink(missing_ok=True)
        return {"status": "applied", "kind": SET_TAX_PROFILE_KIND, **fresh["effects"]["to"], "from": fresh["effects"]["from"],
                "request_hash": fresh["request_hash"], "plan_hash": fresh["plan_hash"], "entries_changed": 0,
                "published_statements_changed": False}
