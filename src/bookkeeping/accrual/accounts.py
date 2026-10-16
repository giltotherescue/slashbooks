"""Read-only chart of accounts listing for agents."""
from __future__ import annotations

from typing import Any

from ..ledger.store import LedgerStore, default_store_path
from .store import list_records


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
        for table in ("items", "schedules", "coverage"):
            for record in list_records(store, table, conn):
                account = record.get("account")
                if account:
                    key = "external" if table == "coverage" else table
                    counts = support.setdefault(account, {})
                    counts[key] = counts.get(key, 0) + 1
    accounts = []
    for name, currency, open_date in rows:
        role = _role(name)
        accounts.append({"name": name, "type": name.split(":")[0], "role": role, "currency": currency,
                         "open_date": open_date, "has_postings": name in used,
                         "reconciles_to_support": role == "control" and entity.basis == "accrual",
                         "support_records": support.get(name, {})})
    return {"basis": entity.basis, "accounts": accounts}


def render_accounts(result: dict[str, Any]) -> str:
    lines = [f"Chart of accounts ({result['basis']} basis)", f"{'Account':<46} {'Type':<12} {'Role':<14} Open date"]
    for row in result["accounts"]:
        lines.append(f"{row['name']:<46} {row['type']:<12} {row['role']:<14} {row['open_date']}")
    return "\n".join(lines)
