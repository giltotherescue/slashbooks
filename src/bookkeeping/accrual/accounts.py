"""Read-only chart of accounts listing for agents."""
from __future__ import annotations

from typing import Any

from ..ledger.store import LedgerStore, default_store_path
from .store import list_records


SUBTYPES = ("receivable", "payable", "prepaid", "deferred_revenue", "customer_deposits", "accrued_liabilities",
            "fixed_asset", "accumulated_depreciation", "bank", "credit_card", "cash", "other_asset", "other_liability",
            "equity", "income", "expense")

SUBTYPE_HELP = ("subtype is one of: " + ", ".join(SUBTYPES) + ". Derivation order: (1) exact starter-chart account name; "
                "(2) registered use: an account on receivable or payable items, an accrual item (accrued_liabilities), "
                "a prepaid, deferred or depreciation schedule account (prepaid, deferred_revenue, fixed_asset) or a "
                "depreciation contra_account (accumulated_depreciation); (3) account name prefix.")

_STARTER_SUBTYPES = {
    "Assets:Bank:Checking": "bank",
    "Liabilities:CreditCard": "credit_card",
    "Assets:Receivable:Accounts-Receivable": "receivable",
    "Assets:Other:Prepaid-Expenses": "prepaid",
    "Assets:Fixed:Equipment": "fixed_asset",
    "Assets:Fixed:Accumulated-Depreciation": "accumulated_depreciation",
    "Liabilities:Payable:Accounts-Payable": "payable",
    "Liabilities:Other:Accrued-Liabilities": "accrued_liabilities",
    "Liabilities:Other:Deferred-Revenue": "deferred_revenue",
    "Liabilities:Other:Customer-Deposits": "customer_deposits",
}
# When one account has several registered uses, the earliest in this tuple wins.
_USAGE_PRIORITY = ("receivable", "payable", "prepaid", "deferred_revenue", "fixed_asset", "accumulated_depreciation", "accrued_liabilities")
_PREFIX_SUBTYPES = (
    ("Assets:Bank:", "bank"), ("Assets:Cash:", "cash"), ("Assets:Receivable:", "receivable"),
    ("Assets:Fixed:Accumulated", "accumulated_depreciation"), ("Assets:Fixed:", "fixed_asset"),
    ("Assets:Prepaid", "prepaid"), ("Assets:Other:Prepaid", "prepaid"), ("Liabilities:Payable:", "payable"),
    ("Liabilities:CreditCard", "credit_card"), ("Liabilities:DeferredRevenue", "deferred_revenue"),
    ("Liabilities:Other:Deferred", "deferred_revenue"), ("Liabilities:CustomerDeposits", "customer_deposits"),
    ("Liabilities:Other:Customer-Deposits", "customer_deposits"), ("Liabilities:Accrued", "accrued_liabilities"),
    ("Liabilities:Other:Accrued", "accrued_liabilities"),
)


def _usage_subtypes(records: dict[str, list[dict]]) -> dict[str, set[str]]:
    used: dict[str, set[str]] = {}
    for item in records.get("items", []):
        account, kind = item.get("account"), item.get("kind")
        if account and kind in ("receivable", "payable"):
            used.setdefault(account, set()).add(kind)
        elif account and kind == "accrual" and account.startswith("Liabilities:"):
            used.setdefault(account, set()).add("accrued_liabilities")
    for schedule in records.get("schedules", []):
        account, kind = schedule.get("account"), schedule.get("kind")
        mapped = {"prepaid": "prepaid", "deferred": "deferred_revenue", "depreciation": "fixed_asset"}.get(kind)
        if account and mapped:
            used.setdefault(account, set()).add(mapped)
        if kind == "depreciation" and schedule.get("contra_account"):
            used.setdefault(schedule["contra_account"], set()).add("accumulated_depreciation")
    return used


def _subtype(name: str, usage: dict[str, set[str]]) -> str:
    if name in _STARTER_SUBTYPES:
        return _STARTER_SUBTYPES[name]
    uses = usage.get(name)
    if uses:
        return next(s for s in _USAGE_PRIORITY if s in uses)
    for prefix, subtype in _PREFIX_SUBTYPES:
        if name.startswith(prefix) or name == prefix.rstrip(":"):
            return subtype
    root = name.split(":")[0]
    return {"Assets": "other_asset", "Liabilities": "other_liability", "Equity": "equity",
            "Income": "income", "Expenses": "expense"}.get(root, "other_asset")


def _role(name: str) -> str:
    if name.startswith(("Assets:Bank:", "Assets:Cash:")):
        return "cash_or_bank"
    if name == "Liabilities:CreditCard" or name.startswith("Liabilities:CreditCard:"):
        return "card"
    if name.startswith(("Assets:", "Liabilities:")):
        return "control"
    return {"Equity": "equity", "Income": "income", "Expenses": "expense"}.get(name.split(":")[0], name.split(":")[0].lower())


def list_accounts(entity) -> dict[str, Any]:
    store = LedgerStore(default_store_path(entity.path))
    with store.connection() as conn:
        rows = conn.execute("SELECT name, currency, open_date FROM accounts ORDER BY name").fetchall()
        used = {r[0] for r in conn.execute("SELECT DISTINCT account FROM postings")}
        support: dict[str, dict[str, int]] = {}
        records: dict[str, list[dict]] = {}
        for table in ("items", "schedules", "coverage"):
            records[table] = list_records(store, table, conn)
            for record in records[table]:
                account = record.get("account")
                if account:
                    key = "external" if table == "coverage" else table
                    counts = support.setdefault(account, {})
                    counts[key] = counts.get(key, 0) + 1
    usage = _usage_subtypes(records)
    accounts = []
    for name, currency, open_date in rows:
        role = _role(name)
        accounts.append({"name": name, "type": name.split(":")[0], "role": role, "subtype": _subtype(name, usage), "currency": currency,
                         "open_date": open_date, "has_postings": name in used,
                         "reconciles_to_support": role == "control" and entity.basis == "accrual",
                         "support_records": support.get(name, {})})
    return {"basis": entity.basis, "accounts": accounts}


def render_accounts(result: dict[str, Any]) -> str:
    lines = [f"Chart of accounts ({result['basis']} basis)", f"{'Account':<46} {'Type':<12} {'Role':<14} {'Subtype':<25} Open date"]
    for row in result["accounts"]:
        lines.append(f"{row['name']:<46} {row['type']:<12} {row['role']:<14} {row['subtype']:<25} {row['open_date']}")
    return "\n".join(lines)
