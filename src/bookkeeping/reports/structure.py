"""Statement structure for machine output (schema version 2).

``books report pnl`` and ``books report balance-sheet`` print ``sections`` with a
section total.  Some consumers also need the ordered statement as a buyer reads
it in QuickBooks or Xero: account rows, subtotal rows and total rows, such as
Gross profit and Total liabilities and equity.  This module builds that view from
a computed :class:`StatementResult`.  It is opt-in (``--structure``), so the
default output of every report stays unchanged.

Every amount is a ``Decimal`` taken from the engine's own statement result.  This
module adds no accounting math beyond adding and subtracting those amounts.

Line model
----------
Each entry of ``structure.lines`` is an object with these fields:

``type``     ``section`` (header, no amount), ``account`` (one ledger account),
             ``computed`` (a line the engine derives, such as current-period net
             income), ``subtotal`` (a section total or Gross profit) or ``total``
             (a statement total such as Net income).
``key``      A stable machine name, for example ``total_revenue``.
``label``    The human label.
``amount``   A decimal string with two places.  Absent on ``section`` lines.
``indent``   Nesting level for display.
``account``  The ledger account name.  Present on ``account`` lines only.
``section``  The ``key`` of the section that the line belongs to.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

from .statements import StatementResult, _SEP

SCHEMA_VERSION = 2

_COST_OF_SALES_NAMES = {"costofsales", "costofgoodssold", "cogs", "costofrevenue", "costofgoods"}


def _account_from_label(label: str) -> str:
    return label.replace(_SEP, ":")


def is_cost_of_sales(account: str) -> bool:
    """True when the account sits under a cost-of-sales group, for example
    ``Expenses:Cost-of-Sales:Hosting`` or ``Expenses:COGS``."""
    parts = account.split(":")
    if len(parts) < 2 or parts[0] != "Expenses":
        return False
    return re.sub(r"[^a-z]", "", parts[1].lower()) in _COST_OF_SALES_NAMES


def _money(value: Decimal) -> str:
    return str(Decimal(value).quantize(Decimal("0.01")))


def _section(key: str, label: str) -> dict:
    return {"type": "section", "key": key, "label": label, "indent": 0}


def _account(section: str, row: dict) -> dict:
    return {
        "type": "account", "key": _account_from_label(row["label"]), "label": row["label"],
        "account": _account_from_label(row["label"]), "amount": _money(row["amount"]),
        "indent": row.get("indent", 1), "section": section,
    }


def _total(kind: str, key: str, label: str, amount: Decimal, section: str | None = None) -> dict:
    line = {"type": kind, "key": key, "label": label, "amount": _money(amount), "indent": 0}
    if section:
        line["section"] = section
    return line


def pnl_structure(result: StatementResult, force_cost_of_sales: bool = False) -> dict:
    revenue, expenses = result.sections[0], result.sections[1]
    lines: list[dict] = [_section("revenue", revenue["label"])]
    lines += [_account("revenue", r) for r in revenue["rows"]]
    lines.append(_total("subtotal", "total_revenue", revenue["total_label"], revenue["total"], "revenue"))

    cost_rows = [r for r in expenses["rows"] if is_cost_of_sales(_account_from_label(r["label"]))]
    other_rows = [r for r in expenses["rows"] if r not in cost_rows]
    gross_profit = None
    if cost_rows or force_cost_of_sales:
        cost_total = sum((r["amount"] for r in cost_rows), Decimal("0.00"))
        lines.append(_section("cost_of_sales", "Cost of Sales"))
        lines += [_account("cost_of_sales", r) for r in cost_rows]
        lines.append(_total("subtotal", "total_cost_of_sales", "Total Cost of Sales", cost_total, "cost_of_sales"))
        gross_profit = revenue["total"] - cost_total
        lines.append(_total("subtotal", "gross_profit", "Gross Profit", gross_profit))
    other_total = sum((r["amount"] for r in other_rows), Decimal("0.00"))
    lines.append(_section("expenses", expenses["label"]))
    lines += [_account("expenses", r) for r in other_rows]
    lines.append(_total("subtotal", "total_expenses", expenses["total_label"], other_total, "expenses"))
    lines.append(_total("total", "net_income", "Net Income", result.totals["net_income"]))
    return {"lines": lines}


_COMPUTED_EQUITY = {
    "Net Income (current period)": "net_income_current_period",
    "Retained earnings from prior fiscal activity": "retained_earnings_prior",
}


def balance_sheet_structure(result: StatementResult) -> dict:
    keys = ("assets", "liabilities", "equity")
    lines: list[dict] = []
    for key, section in zip(keys, result.sections):
        lines.append(_section(key, section["label"]))
        for row in section["rows"]:
            if row["label"] in _COMPUTED_EQUITY:
                lines.append({"type": "computed", "key": _COMPUTED_EQUITY[row["label"]], "label": row["label"],
                              "amount": _money(row["amount"]), "indent": row.get("indent", 1), "section": key})
            else:
                lines.append(_account(key, row))
        lines.append(_total("subtotal", "total_" + key, section["total_label"], section["total"], key))
    total_assets = result.sections[0]["total"]
    liabilities_and_equity = result.sections[1]["total"] + result.sections[2]["total"]
    lines.append(_total("total", "total_liabilities_and_equity", "Total Liabilities and Equity", liabilities_and_equity))
    difference = total_assets - liabilities_and_equity
    return {"lines": lines, "balanced": difference == 0, "difference": _money(difference)}


def build_structure(result: StatementResult) -> dict:
    """Return the structure object for a P&L or balance sheet result."""
    if result.kind == "pnl":
        body = pnl_structure(result)
    elif result.kind == "balance_sheet":
        body = balance_sheet_structure(result)
    else:
        raise ValueError(f"No structured view for {result.kind}.")
    return {"version": SCHEMA_VERSION, **body}


def structured_payload(result: StatementResult) -> dict:
    """The normal JSON payload plus ``schema_version`` and ``structure``."""
    payload = json.loads(result.to_json())
    payload["schema_version"] = SCHEMA_VERSION
    payload["structure"] = build_structure(result)
    return payload


def structured_json(result: StatementResult) -> str:
    return json.dumps(structured_payload(result), indent=2, sort_keys=False)


def structure_to_text(title: str, lines: list[dict]) -> str:
    out = [title, "=" * len(title)]
    for line in lines:
        if line["type"] == "section":
            out += ["", line["label"], "-" * 40]
        else:
            out.append(f"{'  ' * line.get('indent', 0)}{line['label']:<40} {Decimal(line['amount']):>14,.2f}")
    return "\n".join(out)
