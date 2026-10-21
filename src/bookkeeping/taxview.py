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

from collections import defaultdict
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
    return method if method and view_enabled(config) else None


@dataclass
class TaxView:
    tax_method: str
    view: str            # "cash" or "accrual"
    source: str          # "derived" (cash view of accrual records) or "recorded"
    label: str
    tracking_start: str | None = None
    capitalized: dict[str, str] = field(default_factory=dict)   # schedule id -> "recorded" or "default"
    ledger: Any = None
    control: set[str] = field(default_factory=set)
    kept_control: set[str] = field(default_factory=set)
    mixed_prepaid_accounts: list[str] = field(default_factory=list)

    def stamp(self) -> dict[str, Any]:
        return {"tax_method": self.tax_method, "view": self.view, "source": self.source, "label": self.label}


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
        return TaxView("accrual", "accrual", "recorded", label, tracking_start=started)
    if kind == DISCIPLINE_OFF:
        coverage_metadata(conn, "tax_handoff", from_date, to_date, view="cash")
        return TaxView("cash", "cash", "recorded", LABELS["recorded_cash"])
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
    return _derived(conn, from_date, to_date, started if kind == DISCIPLINE_PER_ACCOUNT else None)


def _derived(conn: Any, from_date: date, to_date: date, started: str | None) -> TaxView:
    from .accrual.schedules import effective_tax_treatment
    from .reports import cashview
    schedules = _records(conn, "schedules")
    capitalized: dict[str, str] = {}
    for record in schedules:
        if record.get("kind") != "prepaid":
            continue
        treatment, source = effective_tax_treatment(record)
        if treatment == "capitalize":
            capitalized[record["id"]] = source
    ledger = cashview.derive(conn, to_date, frozenset(capitalized))
    for start, end in ((_EARLIEST, to_date), (from_date, to_date)):
        tie = ledger.tie_out(start, end)
        if not tie["ties"]:
            raise cashview.CashViewError(
                f"The cash view for tax does not tie to the accrual books through {end}: accrual net income {tie['accrual_net_income']} "
                f"minus cash net income {tie['cash_net_income']} is {tie['difference']}, but the reconciling items add up to "
                f"{tie['reconciling_total']}. Nothing was exported. Run books accrual report --kind reconciliation to find the unexplained balance.")
    items, intents = _records(conn, "items"), _records(conn, "intents")
    control, _ = cashview._registered_accounts(items, schedules, intents, cashview._load_entries(conn, to_date))
    by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in schedules:
        if record.get("kind") in {"prepaid", "deferred"} and record.get("account"):
            by_account[record["account"]].append(record)
    item_accounts = {i["account"] for i in items if i.get("account")}
    kept, mixed = set(), []
    for account, records in by_account.items():
        if account in item_accounts:
            continue
        flags = {r["id"] in capitalized for r in records}
        if flags == {True}:
            kept.add(account)
        elif flags == {True, False}:
            mixed.append(account)
    return TaxView("cash", "cash", "derived", LABELS["derived"], tracking_start=started, capitalized=capitalized,
                   ledger=ledger, control=set(control), kept_control=kept, mixed_prepaid_accounts=sorted(mixed))


def derived_balances(conn: Any, view: TaxView, from_date: date, to_date: date) -> list[dict[str, Any]]:
    """Trial balance rows of the derived cash view, in the shape of ``taxmap.period_balances``."""
    from .reports.cache import iter_postings, list_accounts
    from .taxmap import PRIOR_EARNINGS_ACCOUNT
    start = from_date.isoformat()
    dropped = view.control - view.kept_control
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
                       "message": f"Deferred revenue and customer deposits are income when received on the cash method: {_money(amount)} more income than the accrual view."})
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
            result.append({"code": "prepaid_capitalized", "refs": [record["id"]], "treatment_source": view.capitalized[record["id"]],
                           "message": (f"Prepaid schedule {record.get('reference') or record['id']} is capitalized for tax "
                                       f"({'recorded' if view.capitalized[record['id']] == 'recorded' else 'default: the benefit lasts more than 12 months or past the next tax year'}). "
                                       "It is deducted as it is used, not when paid.")})
        elif record.get("kind") == "prepaid" and "tax_treatment" not in record:
            result.append({"code": "prepaid_expensed_by_default", "refs": [record["id"]], "treatment_source": "default",
                           "message": (f"Prepaid schedule {record.get('reference') or record['id']} is deducted when paid by default (12-month rule). "
                                       "Record its tax treatment with books accrual schedule tax-treatment to confirm.")})
    for account in view.mixed_prepaid_accounts:
        result.append({"code": "prepaid_account_mixed", "accounts": [account],
                       "message": f"{account} holds both capitalized and expensed prepaid schedules, so its balance is not shown. The computed adjustment row carries it."})
    issues = [i for i in view.ledger.issues if start <= i["date"] <= end]
    for issue in issues:
        result.append({"code": issue["code"], "amount": issue.get("amount"), "message": issue["message"]})
    result.append({"code": "adjustment_row", "accounts": [ADJUSTMENT_ACCOUNT],
                   "message": (f"{ADJUSTMENT_ACCOUNT} is computed. It removes the receivable, payable, deferred and accrued balances "
                               "that a cash-method return does not carry, so the trial balance stays balanced.")})
    return result


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
    missing = sorted(r["id"] for r in list_records(LedgerStore(path), "schedules")
                     if r.get("kind") == "prepaid" and "tax_treatment" not in r)
    if not missing:
        return []
    return [{"code": "schedule_tax_treatment_missing", "refs": missing,
             "message": ("The company files on the cash method, and these prepaid schedules have no recorded tax treatment. "
                         "A default applies (deduct when paid only under the 12-month rule). Record it with books accrual schedule tax-treatment.")}]


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
        with store.transaction(immediate=True) as conn:
            store.append_audit_event("tax-method-set", {"from": fresh["effects"]["from"], "to": fresh["effects"]["to"],
                                                         "plan_hash": fresh["plan_hash"]}, conn)
            _seal(store, conn, "entity-set-tax-method")
        config = {**entity.entity_config, TAX_METHOD_KEY: request["method"], TAX_VIEW_KEY: request[TAX_VIEW_KEY]}
        temp = entity.path / "entity.json.tax-method.tmp"
        temp.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, entity.path / "entity.json")
        return {"status": "applied", "kind": SET_TAX_METHOD_KIND, **request, "from": fresh["effects"]["from"],
                "request_hash": fresh["request_hash"], "plan_hash": fresh["plan_hash"], "entries_changed": 0,
                "published_statements_changed": False}
