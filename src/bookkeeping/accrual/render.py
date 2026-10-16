"""Readable views of engine-computed accrual reports; JSON remains canonical."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping


def _text(value: Any) -> str:
    return "Not provided" if value is None or value == "" else str(value).replace("\n", " ").replace("\r", " ")


def _label(value: Any) -> str:
    labels = {"receivable": "Receivable", "payable": "Payable", "accrual": "Accrued balance",
              "credit": "Unapplied credit", "prepaid": "Prepaid expense", "prepaid_expense": "Prepaid expense",
              "deferred": "Deferred revenue", "deferred_revenue": "Deferred revenue"}
    return labels.get(value, _text(value))


def _table(columns: list[tuple[str, str]], rows: list[Mapping[str, Any]]) -> list[str]:
    values = [[_text(row.get(key)) for key, _ in columns] for row in rows]
    widths = [max(len(label), *(len(row[index]) for row in values)) for index, (_, label) in enumerate(columns)]
    return [" | ".join(label.ljust(width) for (_, label), width in zip(columns, widths)),
            "-+-".join("-" * width for width in widths),
            *[" | ".join(value.ljust(width) for value, width in zip(row, widths)) for row in values]]


def render_report(document: Mapping[str, Any]) -> str:
    titles = {"aging": "Receivables and payables aging", "outstanding": "Outstanding balances",
              "schedules": "Prepaid expenses and deferred revenue", "adjustments": "Accrual activity register",
              "support": "External supporting balances", "reconciliation": "Account reconciliation"}
    kind = document["kind"]
    lines = [titles[kind], f"As of {document['as_of']} | Accrual basis | {document['currency']}"]
    if document.get("from_date"):
        lines.append(f"Period begins {document['from_date']}")
    coverage = document.get("coverage")
    if coverage:
        lines.append(f"Detailed activity begins {coverage['cutover_date']}; imported opening as of {coverage['source_as_of']}.")
        if coverage.get("summary_from"):
            lines.append(f"Imported fiscal summary: {coverage['summary_from']} through {coverage['summary_to']}; earlier monthly detail is unavailable.")
    rows = [{**row, "display_reference": row.get("reference") or row.get("counterparty") or row.get("description") or row.get("id"),
             "kind": _label(row.get("kind"))} for row in document["rows"]]
    columns = {
        "aging": [("display_reference", "Reference"), ("kind", "Type"), ("counterparty", "Customer / vendor"), ("due_date", "Due date"), ("bucket_label", "Aging"), ("aging_amount", "Balance")],
        "outstanding": [("display_reference", "Reference"), ("kind", "Type"), ("counterparty", "Customer / vendor"), ("due_date", "Due date"), ("remaining_amount", "Remaining"), ("status", "Status")],
        "schedules": [("display_reference", "Schedule"), ("kind", "Type"), ("account", "Account"), ("recognized_amount", "Recognized"), ("remaining_amount", "Remaining"), ("status", "Status")],
        "support": [("display_reference", "Support"), ("account", "Account"), ("as_of", "Evidence date"), ("balance", "Balance")],
        "reconciliation": [("account", "Account"), ("ledger_balance", "Ledger"), ("support_balance", "Supporting detail"), ("difference", "Difference"), ("status", "Status")],
    }
    if not rows:
        lines.append("No recorded items for this report.")
    elif kind == "adjustments":
        for row in rows:
            lines.extend(["", f"{row['date']} | {_text(row.get('reference') or row.get('description') or row['source_id'])}"])
            postings = [{**p, "debit": f"{max(Decimal(p['amount']), Decimal(0)):.2f}",
                         "credit": f"{max(-Decimal(p['amount']), Decimal(0)):.2f}"} for p in row["postings"]]
            lines.extend(_table([("account", "Account"), ("debit", "Debit"), ("credit", "Credit"), ("currency", "Currency")], postings))
    else:
        lines.extend(["", *_table(columns[kind], rows)])
    if kind == "aging":
        lines.append("\nSummary by type, account and customer/vendor; unapplied credits are separate.")
        for side, totals in (document.get("totals") or {}).items():
            line = f"Total {side} | {totals['total']} | Overdue {totals['overdue_total']} | Items {totals['count']} | Overdue items {totals['overdue_count']}"
            if totals["unapplied_credits"]["count"]:
                line += f" | Unapplied credits {totals['unapplied_credits']['total']} (separate)"
            lines.append(line)
        for group in document["summary"]:
            lines.append(f"{_label(group['kind'])} | {group['account']} | {_text(group['counterparty'])} | {group['currency']} | Total {group['total']}")
            lines.extend(f"  {document['bucket_labels'][key]}: {amount}" for key, amount in group["buckets"].items() if Decimal(amount) != 0)
    issues = document.get("issues", [])
    lines.append("\nReview issues:")
    lines.extend(f"- {row.get('message') or row['code']}" for row in issues)
    if not issues:
        lines.append("No outstanding review items.")
    return "\n".join(lines)
