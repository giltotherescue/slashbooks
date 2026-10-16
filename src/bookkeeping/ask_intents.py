"""Balance-sheet style intents for ``books ask``: an explicit table, no clever parsing.

Each row maps one plain-English pattern to one existing report. The answers read
reports; they never compute accounting results here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional

# (intent, pattern). A question may match several rows: payables and receivables combine.
INTENT_TABLE = (
    ("balance_sheet", r"balance sheet|what do we (?:have|own)\b|\bour assets\b|financial position|\bnet worth\b"),
    ("schedules", r"\bprepaid|\bdeferred|\bunearned"),
    ("payables", r"\bpayables?\b|\bwhat do we owe\b|\bwe owe\b|\bunpaid bills?\b|\boutstanding bills?\b|\bbills? (?:are |is )?(?:due|unpaid|outstanding|open)\b|\bowe (?:our )?(?:vendors?|suppliers?)\b"),
    ("receivables", r"\breceivables?\b|\bwho owes\b|\bowes? us\b|\bowed to us\b|\bunpaid invoices?\b|\boutstanding invoices?\b|\bcustomers? owe\b"),
    ("aging", r"\bag(?:e)?ing\b"),
)

SUPPORTED_TEXT = (
    "I didn't recognise the type of question. Here's what you can ask:\n\n"
    "  Revenue/income and earnings:\n"
    "    'What was Q1 revenue?'\n"
    "    'How much did we earn in January?'\n\n"
    "  Spending:\n"
    "    'How much did we spend on software last quarter?'\n"
    "    'What were our total expenses this year?'\n\n"
    "  Overall performance:\n"
    "    'How are we doing this year?'\n"
    "    'What is our net income for Q2?'\n\n"
    "  What we owe, and what is owed to us (accrual books):\n"
    "    'What do we owe?'  /  'What are our unpaid bills?'\n"
    "    'Who owes us money?'  /  'Show receivables aging'\n\n"
    "  Prepaid expenses and deferred revenue (accrual books):\n"
    "    'How much is prepaid?'  /  'What is our deferred revenue?'\n\n"
    "  Balance sheet:\n"
    "    'What is the balance sheet?'\n\n"
    "  Owner draws and vendor payments:\n"
    "    'What were owner draws this month?'\n"
    "    'How much did we pay Acme last quarter?'"
)

# "cash" used as a basis in a flow question: "how much cash did we spend ...".
CASH_FLOW = re.compile(r"\bcash\b(?:\s+\w+){0,3}?\s+(?:spen[dt]|paid|pay|received|receive|collected|collect|earned?|brought in)\b"
                       r"|\b(?:spen[dt]|paid|received|collected)\b[^?.]*\bin cash\b")


_BUCKET_LABELS = {"current": "Current", "1_30": "1-30 days overdue", "31_60": "31-60 days overdue",
                  "61_90": "61-90 days overdue", "over_90": "Over 90 days overdue", "unknown_due_date": "No due date"}


def match(question: str) -> list[str]:
    lowered = question.lower()
    return [name for name, pattern in INTENT_TABLE if re.search(pattern, lowered)]


@dataclass
class Routed:
    text: str
    intent: str
    data: dict[str, Any]


def _money(value: Any) -> str:
    return f"{float(value):,.2f}"


def _side_text(label: str, side: dict[str, Any]) -> str:
    lines = [f"{label}: {_money(side['total'])} across {side['count']} open item(s); overdue {_money(side['overdue_total'])}."]
    for bucket, amount in side["buckets"].items():
        if float(amount):
            lines.append(f"  {_BUCKET_LABELS.get(bucket, bucket)}: {_money(amount)}")
    credits = side["unapplied_credits"]
    if credits["count"]:
        lines.append(f"  Unapplied credits (not netted): {_money(credits['total'])}")
    return "\n".join(lines)


def answer(entity_path: Path, question: str, names: list[str], kept: Optional[str], as_of: date) -> Routed:
    """Answer one matched point-in-time question as of ``as_of``."""
    base = {"from_date": str(as_of), "to_date": str(as_of), "as_of": str(as_of)}
    if "balance_sheet" in names:
        from .reports.statements import balance_sheet
        sheet = balance_sheet(entity_path, as_of)
        return Routed(f"Balance sheet as of {as_of}.\n\n{sheet.to_text()}", "balance_sheet", base)
    if kept != "accrual":
        return Routed("Not available: these books are kept on the cash basis, which does not track bills, invoices, prepaid "
                      "expenses or deferred revenue. Ask for the balance sheet, or switch to accrual books to track them.",
                      "unavailable_cash_basis", {**base, "unavailable": "accrual_tracking_required", "books_basis": kept})
    from .accrual import reports
    if "schedules" in names:
        rows = reports.report(entity_path, "schedules", as_of=as_of)["rows"]
        lowered = question.lower()
        wanted = {"prepaid"} if "prepaid" in lowered and not re.search(r"deferred|unearned", lowered) else \
                 {"deferred"} if re.search(r"deferred|unearned", lowered) and "prepaid" not in lowered else {"prepaid", "deferred"}
        rows = [r for r in rows if r.get("kind") in wanted and float(r.get("remaining_amount", 0)) > 0]
        total = sum(float(r["remaining_amount"]) for r in rows)
        title = "Prepaid expenses and deferred revenue" if len(wanted) == 2 else "Prepaid expenses" if "prepaid" in wanted else "Deferred revenue"
        lines = [f"{title} still to recognize as of {as_of}: {_money(total)} across {len(rows)} schedule(s)."]
        lines += [f"  {r.get('kind')}: {r['id']} ({r.get('account')}): {_money(r['remaining_amount'])}" for r in rows]
        return Routed("\n".join(lines), "schedules", {**base, "total": f"{total:.2f}", "schedules": [r["id"] for r in rows]})
    result = reports.report(entity_path, "aging", as_of=as_of)
    totals = result["totals"]
    wants_pay = "payables" in names or names == ["aging"]
    wants_recv = "receivables" in names or names == ["aging"]
    parts = []
    if wants_pay:
        parts.append(_side_text("What we owe (payables)", totals["payable"]))
    if wants_recv:
        parts.append(_side_text("What is owed to us (receivables)", totals["receivable"]))
    shown = {k: totals[k] for k, on in (("payable", wants_pay), ("receivable", wants_recv)) if on}
    return Routed(f"Open balances by due date as of {as_of}.\n" + "\n".join(parts), "aging", {**base, "totals": shown})
