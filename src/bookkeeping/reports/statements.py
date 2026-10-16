"""Deterministic financial statements and the ``ask`` question dispatcher.

All computations use Python ``Decimal`` arithmetic.  No floats.  No SQL emitted
to the owner.  No beancount syntax in rendered output.

Statements
----------
profit_and_loss(entity_path, from_date, to_date)  -> StatementResult
balance_sheet(entity_path, as_of)                 -> StatementResult
trial_balance(entity_path, as_of)                 -> StatementResult
general_ledger(entity_path, from_date, to_date)   -> StatementResult

Ask dispatcher
--------------
ask(entity_path, question, *, today=None)         -> AskResult

CLI surface
-----------
add_parser(subparsers)  — registers ``report`` command group + ``ask``
run(args)               — dispatch

Account-name rendering
----------------------
Internal beancount account names like ``Income:Revenue:Consulting`` are
rendered as ``Income › Revenue › Consulting`` throughout all human-facing
text.  The ``›`` separator is the canonical readable separator for this module.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from .cache import (
    get_account_balance,
    iter_postings,
    list_accounts,
    open_cache,
    regenerate,
)

# ---------------------------------------------------------------------------
# Account-name rendering helpers
# ---------------------------------------------------------------------------

_SEP = " › "  # human-readable account-name separator


def _render_account(name: str) -> str:
    """Convert ``Assets:Bank:Checking`` to ``Assets › Bank › Checking``."""
    return name.replace(":", _SEP)


# Root categories used for statement classification
_INCOME_ROOTS = ("Income",)
_EXPENSE_ROOTS = ("Expenses",)
_ASSET_ROOTS = ("Assets",)
_LIABILITY_ROOTS = ("Liabilities",)
_EQUITY_ROOTS = ("Equity",)


def _root(account: str) -> str:
    return account.split(":")[0]


def _is_income(account: str) -> bool:
    return _root(account) in _INCOME_ROOTS


def _is_expense(account: str) -> bool:
    return _root(account) in _EXPENSE_ROOTS


def _is_asset(account: str) -> bool:
    return _root(account) in _ASSET_ROOTS


def _is_liability(account: str) -> bool:
    return _root(account) in _LIABILITY_ROOTS


def _is_equity(account: str) -> bool:
    return _root(account) in _EQUITY_ROOTS


# ---------------------------------------------------------------------------
# StatementResult
# ---------------------------------------------------------------------------


@dataclass
class StatementResult:
    """Container for a computed statement."""

    kind: str  # "pnl" | "balance_sheet" | "trial_balance" | "general_ledger"
    from_date: Optional[date]
    to_date: Optional[date]  # also used as as_of for point-in-time statements
    sections: list[dict] = field(default_factory=list)
    totals: dict[str, Decimal] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    # ---- Rendering ----------------------------------------------------------

    def to_json(self) -> str:
        """Serialise to deterministic JSON (Decimal → string)."""

        def _default(obj: Any) -> Any:
            if isinstance(obj, Decimal):
                return str(obj)
            if isinstance(obj, date):
                return obj.isoformat()
            raise TypeError(f"Not serialisable: {type(obj)}")

        payload = {
            "kind": self.kind,
            "from_date": self.from_date.isoformat() if self.from_date else None,
            "to_date": self.to_date.isoformat() if self.to_date else None,
            "sections": self.sections,
            "totals": {k: str(v) for k, v in self.totals.items()},
            "meta": self.meta,
        }
        return json.dumps(payload, indent=2, default=_default, sort_keys=False)

    def to_text(self) -> str:
        """Plain-English rendering.  No beancount syntax, no SQL, no jargon."""
        lines: list[str] = []
        _head = self._header()
        lines.append(_head)
        lines.append("=" * len(_head))
        if self.meta.get("basis"):
            lines.append(f"Accounting basis: {self.meta['basis']}")
        coverage = self.meta.get("coverage")
        if coverage:
            lines.append(f"Activity cutover: {coverage['cutover_date']}, opening balances as of {coverage['source_as_of']}")
            if coverage.get("summary_from") and coverage.get("summary_to"):
                period = f"{coverage['summary_from']} through {coverage['summary_to']}"
                if self.kind in {"pnl", "general_ledger"}:
                    included = "included" if self.meta.get("summary_included") else "not included in this report"
                    lines.append(f"Imported activity summary: {period} ({included})")
                else:
                    lines.append(f"Retained fiscal summary coverage: {period}")
        issues = self.meta.get("unresolved_issues", [])
        if issues:
            lines.append("Unresolved accounting issues:")
            for issue in issues:
                message = issue.get("message") or issue.get("code", "Unresolved issue").replace("_", " ")
                refs = ", ".join(str(ref) for ref in issue.get("refs", []))
                lines.append(f"  - {message}" + (f" [{refs}]" if refs else ""))
        for section in self.sections:
            lines.append("")
            lines.append(section.get("label", ""))
            lines.append("-" * 40)
            for row in section.get("rows", []):
                indent = "  " * row.get("indent", 0)
                label = row.get("label", "")
                amount = row.get("amount")
                if amount is not None:
                    lines.append(f"{indent}{label:<40} {_fmt(amount):>14}")
                else:
                    lines.append(f"{indent}{label}")
            total = section.get("total")
            if total is not None:
                label = section.get("total_label", "Total")
                lines.append(f"{'':>2}{label:<40} {_fmt(total):>14}")
        lines.append("")
        # Overall totals
        if self.totals:
            lines.append("-" * 56)
            for k, v in self.totals.items():
                k_readable = k.replace("_", " ").title()
                lines.append(f"{k_readable:<42} {_fmt(v):>14}")
        return "\n".join(lines)

    def _header(self) -> str:
        kind_labels = {
            "pnl": "Profit and Loss",
            "balance_sheet": "Balance Sheet",
            "trial_balance": "Trial Balance",
            "general_ledger": "General Ledger",
        }
        label = kind_labels.get(self.kind, self.kind.replace("_", " ").title())
        if self.from_date and self.to_date and self.kind in ("pnl", "general_ledger"):
            return f"{label}  {self.from_date}  through  {self.to_date}"
        if self.to_date and self.kind in ("balance_sheet", "trial_balance"):
            return f"{label}  as of  {self.to_date}"
        return label


def _fmt(amount: Decimal) -> str:
    """Format a Decimal as a two-decimal currency string."""
    return f"{amount:,.2f}"


# ---------------------------------------------------------------------------
# Ledger balance helpers
# ---------------------------------------------------------------------------


def _account_balances_in_range(
    conn: Any,
    from_date: Optional[date],
    to_date: Optional[date],
) -> dict[str, Decimal]:
    """Return net posting amounts per account within [from_date, to_date] inclusive."""
    totals: dict[str, Decimal] = {}
    for row in iter_postings(conn, from_date=from_date, to_date=to_date):
        _, _, _, account, amount, _ = row
        totals[account] = totals.get(account, Decimal("0.00")) + amount
    return totals


def _account_balances_as_of(conn: Any, as_of: date) -> dict[str, Decimal]:
    """Return cumulative balance per account up to and including *as_of*."""
    return _account_balances_in_range(conn, None, as_of)


# ---------------------------------------------------------------------------
# P&L
# ---------------------------------------------------------------------------


def profit_and_loss(
    entity_path: Path | str,
    from_date: date,
    to_date: date,
) -> StatementResult:
    """Compute profit and loss for the period [from_date, to_date] inclusive."""
    entity_path = Path(entity_path)
    conn = open_cache(entity_path)
    try:
        return _compute_pnl(conn, from_date, to_date)
    finally:
        conn.close()


def _compute_pnl(conn: Any, from_date: date, to_date: date) -> StatementResult:
    from ..accrual.reports import financial_metadata
    if from_date > to_date:
        raise ValueError("Report start date must be on or before end date.")
    metadata = financial_metadata(conn, "pnl", from_date, to_date)
    balances = _account_balances_in_range(conn, from_date, to_date)

    # Income: credit-normal → positive balance means credit → income positive
    income_rows = []
    total_income = Decimal("0.00")
    for acc, bal in sorted(balances.items()):
        if _is_income(acc):
            # Income accounts: credit-normal, flip sign for human display
            display = -bal
            income_rows.append({"label": _render_account(acc), "amount": display, "indent": 1})
            total_income += display

    # Expenses: debit-normal → positive balance = expense
    expense_rows = []
    total_expenses = Decimal("0.00")
    for acc, bal in sorted(balances.items()):
        if _is_expense(acc):
            display = bal  # debit-normal, positive = expense
            expense_rows.append({"label": _render_account(acc), "amount": display, "indent": 1})
            total_expenses += display

    net_income = total_income - total_expenses

    sections = [
        {
            "label": "Revenue" if metadata["basis"] == "accrual" else "Money In",
            "rows": income_rows,
            "total": total_income,
            "total_label": "Total Revenue",
        },
        {
            "label": "Expenses" if metadata["basis"] == "accrual" else "Money Out",
            "rows": expense_rows,
            "total": total_expenses,
            "total_label": "Total Expenses",
        },
    ]

    return StatementResult(
        kind="pnl",
        from_date=from_date,
        to_date=to_date,
        sections=sections,
        totals={"net_income": net_income},
        meta=metadata,
    )


# ---------------------------------------------------------------------------
# Balance Sheet
# ---------------------------------------------------------------------------


def balance_sheet(
    entity_path: Path | str,
    as_of: date,
) -> StatementResult:
    """Compute balance sheet as of *as_of* (end-of-day)."""
    entity_path = Path(entity_path)
    conn = open_cache(entity_path)
    try:
        return _compute_balance_sheet(conn, as_of)
    finally:
        conn.close()


def _compute_balance_sheet(conn: Any, as_of: date) -> StatementResult:
    from ..accrual.reports import financial_metadata
    metadata = financial_metadata(conn, "balance_sheet", None, as_of)
    balances = _account_balances_as_of(conn, as_of)

    # Also compute net income from inception through as_of for equity section
    all_balances = _account_balances_as_of(conn, as_of)
    net_income_raw = Decimal("0.00")
    for acc, bal in all_balances.items():
        if _is_income(acc):
            net_income_raw += -bal  # flip income
        elif _is_expense(acc):
            net_income_raw -= bal  # subtract expenses

    # Assets
    asset_rows = []
    total_assets = Decimal("0.00")
    for acc, bal in sorted(balances.items()):
        if _is_asset(acc):
            display = bal  # debit-normal
            asset_rows.append({"label": _render_account(acc), "amount": display, "indent": 1})
            total_assets += display

    # Liabilities
    liability_rows = []
    total_liabilities = Decimal("0.00")
    for acc, bal in sorted(balances.items()):
        if _is_liability(acc):
            display = -bal  # credit-normal, flip for display
            liability_rows.append({"label": _render_account(acc), "amount": display, "indent": 1})
            total_liabilities += display

    # Equity
    equity_rows = []
    total_equity_base = Decimal("0.00")
    for acc, bal in sorted(balances.items()):
        if _is_equity(acc):
            display = -bal  # credit-normal, flip for display
            equity_rows.append({"label": _render_account(acc), "amount": display, "indent": 1})
            total_equity_base += display

    if metadata["basis"] == "accrual":
        opening = metadata.get("coverage")
        anchor = date.fromisoformat(opening["fiscal_start"]) if opening else date(as_of.year, 1, 1)
        import calendar
        fiscal_start = date(as_of.year, anchor.month, min(anchor.day, calendar.monthrange(as_of.year, anchor.month)[1]))
        if fiscal_start > as_of:
            fiscal_start = fiscal_start.replace(year=fiscal_start.year - 1)
        current = _account_balances_in_range(conn, fiscal_start, as_of)
        current_income = -sum((n for a, n in current.items() if _is_income(a) or _is_expense(a)), Decimal(0))
        prior_income = net_income_raw - current_income
        if prior_income:
            equity_rows.append({"label": "Retained earnings from prior fiscal activity", "amount": prior_income, "indent": 1})
            total_equity_base += prior_income
        net_income_raw = current_income
        metadata["fiscal_start"] = fiscal_start.isoformat()
        metadata["cumulative_net_income"] = str(prior_income + current_income)
        metadata["current_fiscal_net_income"] = str(current_income)
    # Include current-period net income as a computed line in equity
    equity_rows.append({
        "label": "Net Income (current period)",
        "amount": net_income_raw,
        "indent": 1,
    })
    total_equity = total_equity_base + net_income_raw

    sections = [
        {
            "label": "Assets",
            "rows": asset_rows,
            "total": total_assets,
            "total_label": "Total Assets",
        },
        {
            "label": "Liabilities",
            "rows": liability_rows,
            "total": total_liabilities,
            "total_label": "Total Liabilities",
        },
        {
            "label": "Equity",
            "rows": equity_rows,
            "total": total_equity,
            "total_label": "Total Equity",
        },
    ]

    return StatementResult(
        kind="balance_sheet",
        from_date=None,
        to_date=as_of,
        sections=sections,
        totals={
            "total_assets": total_assets,
            "total_liabilities_and_equity": total_liabilities + total_equity,
        },
        meta=metadata,
    )


# ---------------------------------------------------------------------------
# Trial Balance
# ---------------------------------------------------------------------------


def trial_balance(
    entity_path: Path | str,
    as_of: date,
) -> StatementResult:
    """Compute trial balance as of *as_of*."""
    entity_path = Path(entity_path)
    conn = open_cache(entity_path)
    try:
        return _compute_trial_balance(conn, as_of)
    finally:
        conn.close()


def _compute_trial_balance(conn: Any, as_of: date) -> StatementResult:
    from ..accrual.reports import financial_metadata
    metadata = financial_metadata(conn, "trial_balance", None, as_of)
    balances = _account_balances_as_of(conn, as_of)

    rows = []
    total_debits = Decimal("0.00")
    total_credits = Decimal("0.00")

    for acc in sorted(balances):
        bal = balances[acc]
        if bal == Decimal("0.00"):
            continue  # skip zero-balance accounts
        if bal > 0:
            rows.append({"label": _render_account(acc), "debit": bal, "credit": Decimal("0.00"), "indent": 0})
            total_debits += bal
        else:
            rows.append({"label": _render_account(acc), "debit": Decimal("0.00"), "credit": -bal, "indent": 0})
            total_credits += -bal

    # Reformat rows to have a single "amount" column for to_text rendering
    display_rows = []
    for r in rows:
        if r["debit"] > 0:
            display_rows.append({"label": r["label"], "amount": r["debit"], "indent": 0})
        else:
            display_rows.append({"label": r["label"], "amount": -r["credit"], "indent": 0})

    sections = [
        {
            "label": "All Accounts",
            "rows": display_rows,
            "total": None,
        }
    ]

    return StatementResult(
        kind="trial_balance",
        from_date=None,
        to_date=as_of,
        sections=sections,
        totals={
            "total_debits": total_debits,
            "total_credits": total_credits,
            "net": total_debits - total_credits,
        },
        meta={**metadata, "raw_rows": rows},
    )


# ---------------------------------------------------------------------------
# General Ledger
# ---------------------------------------------------------------------------


def general_ledger(
    entity_path: Path | str,
    from_date: date,
    to_date: date,
) -> StatementResult:
    """Compute general ledger for [from_date, to_date] inclusive."""
    entity_path = Path(entity_path)
    conn = open_cache(entity_path)
    try:
        return _compute_general_ledger(conn, from_date, to_date)
    finally:
        conn.close()


def _compute_general_ledger(conn: Any, from_date: date, to_date: date) -> StatementResult:
    from ..accrual.reports import financial_metadata
    metadata = financial_metadata(conn, "general_ledger", from_date, to_date)
    rows_by_account: dict[str, list[dict]] = {}

    from ..accrual.reports import settlement_details
    details = settlement_details(conn)
    for entry_date, narration, payee, account, amount, currency, source_id in iter_postings(
        conn, from_date=from_date, to_date=to_date, include_source=True
    ):
        if source_id in details and details[source_id] not in narration:
            narration = f"{narration}: {details[source_id]}"
        d = entry_date if isinstance(entry_date, date) else date.fromisoformat(entry_date)
        label_parts = [str(d)]
        if payee:
            label_parts.append(payee)
        label_parts.append(narration)
        label = "  |  ".join(label_parts)
        row = {"label": label, "amount": amount, "indent": 1, "date": str(d)}
        rows_by_account.setdefault(account, []).append(row)

    sections = []
    for account in sorted(rows_by_account):
        acct_rows = rows_by_account[account]
        total = sum(r["amount"] for r in acct_rows)
        sections.append({
            "label": _render_account(account),
            "rows": acct_rows,
            "total": total,
            "total_label": "Account Total",
        })

    return StatementResult(
        kind="general_ledger",
        from_date=from_date,
        to_date=to_date,
        sections=sections,
        totals={},
        meta=metadata,
    )


# ---------------------------------------------------------------------------
# Ask dispatcher
# ---------------------------------------------------------------------------


@dataclass
class AskResult:
    """Result of an ``ask()`` call."""

    question: str
    intent: str  # "revenue" | "spend" | "draws" | "vendor" | "net_income" | "unknown"
    answer_text: str  # plain-English answer
    data: dict[str, Any] = field(default_factory=dict)


# Period parsing patterns
_MONTH_NAMES = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_QUARTER_MONTHS = {
    1: (1, 3),
    2: (4, 6),
    3: (7, 9),
    4: (10, 12),
}


def _days_in_month(year: int, month: int) -> int:
    """Return the number of days in a given month."""
    import calendar
    return calendar.monthrange(year, month)[1]


def _quarter_dates(q: int, year: int) -> tuple[date, date]:
    start_m, end_m = _QUARTER_MONTHS[q]
    start = date(year, start_m, 1)
    end = date(year, end_m, _days_in_month(year, end_m))
    return start, end


def _current_quarter(today: date) -> int:
    m = today.month
    return (m - 1) // 3 + 1


def _last_quarter_dates(today: date) -> tuple[date, date]:
    q = _current_quarter(today)
    prev_q = q - 1 if q > 1 else 4
    year = today.year if q > 1 else today.year - 1
    return _quarter_dates(prev_q, year)


def _parse_period(question: str, today: date) -> tuple[Optional[date], Optional[date]]:
    """Extract (from_date, to_date) from a question string.

    Returns (None, None) when no period is recognised.
    """
    q = question.lower()

    # Explicit year "2026"
    year_m = re.search(r"\b(20\d{2})\b", q)
    explicit_year = int(year_m.group(1)) if year_m else today.year

    # Explicit ISO range: "2026-01-01 to 2026-05-31"
    iso = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", q)
    if len(iso) == 2:
        try:
            first, last = date.fromisoformat(iso[0]), date.fromisoformat(iso[1])
            if first <= last:
                return first, last
        except ValueError:
            pass

    # Month range: "January through May", "jan to may 2026", "from March - June"
    names = "|".join(sorted(_MONTH_NAMES, key=len, reverse=True))
    span = re.search(rf"\b({names})(?:\s+(20\d{{2}}))?\s*(?:through|thru|to|until|-|–)\s*({names})(?:\s+(20\d{{2}}))?\b", q)
    if span:
        first_month, last_month = _MONTH_NAMES[span.group(1)], _MONTH_NAMES[span.group(3)]
        first_year = int(span.group(2) or span.group(4) or explicit_year)
        last_year = int(span.group(4) or span.group(2) or explicit_year)
        if last_year == first_year and last_month < first_month:
            last_year += 1
        return date(first_year, first_month, 1), date(last_year, last_month, _days_in_month(last_year, last_month))

    # "last year"
    if "last year" in q:
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)

    # "last quarter"
    if "last quarter" in q:
        return _last_quarter_dates(today)

    # "this quarter" / "current quarter"
    if "this quarter" in q or "current quarter" in q:
        return _quarter_dates(_current_quarter(today), today.year)

    # "Q1" .. "Q4" (optionally followed by year)
    qm = re.search(r"\bq([1-4])\b", q)
    if qm:
        return _quarter_dates(int(qm.group(1)), explicit_year)

    # "last month"
    if "last month" in q:
        m = today.month - 1 if today.month > 1 else 12
        y = today.year if today.month > 1 else today.year - 1
        start = date(y, m, 1)
        end = date(y, m, _days_in_month(y, m))
        return start, end

    # "this month" / "current month"
    if "this month" in q or "current month" in q:
        start = date(today.year, today.month, 1)
        end = date(today.year, today.month, _days_in_month(today.year, today.month))
        return start, end

    # "this year"
    if "this year" in q or "ytd" in q or "year to date" in q:
        return date(today.year, 1, 1), today

    # Month name
    for name, month_num in _MONTH_NAMES.items():
        if re.search(rf"\b{name}\b", q):
            y = explicit_year
            start = date(y, month_num, 1)
            end = date(y, month_num, _days_in_month(y, month_num))
            return start, end

    # Explicit year only → full calendar year
    if year_m:
        return date(explicit_year, 1, 1), date(explicit_year, 12, 31)

    return None, None


def _extract_category(question: str) -> Optional[str]:
    """Extract a category/account hint from 'spend on <category>'."""
    m = re.search(r"\bon\s+([a-zA-Z][a-zA-Z0-9\s\-]+?)(?:\s+(?:last|this|in|for|\d|q[1-4])|$)", question.lower())
    if m:
        return m.group(1).strip()
    return None


def _extract_vendor(question: str) -> Optional[str]:
    """Extract a vendor/payee name from 'pay <name>' / 'paid <name>'."""
    m = re.search(r"(?:pay|paid|to)\s+([A-Za-z][A-Za-z0-9\s\-&'.]+?)(?:\s+(?:last|this|in|for|\d|q[1-4])|[\?.]|$)", question)
    if m:
        return m.group(1).strip()
    return None


_CASH_VIEW = re.compile(r"\bcash[- ]?(?:basis|view|method|based|accounting)\b|\bon a cash\b|\bcash[- ]profit\b|\bcash[- ]income\b|\bcash[- ]p&l\b")
_ACCRUAL_VIEW = re.compile(r"\baccrual[- ]?(?:basis|view|method|based|accounting)\b|\bon an accrual\b|\baccrual[- ](?:profit|income|p&l)\b")


def _without_cash_wording(question: str) -> str:
    """Remove the cash-basis wording so the question is grammatical on the kept basis."""
    text = re.sub(r"(?i)\b(?:on|using|in|under|with|by)\s+(?:a|the)\s+cash[- ]?(?:basis|view|method|based|accounting)\b", "", question)
    text = re.sub(r"(?i)\b(?:a\s+|the\s+)?cash[- ]?(?:basis|view|method|based|accounting)\b", "", text)
    text = re.sub(r"(?i)\bcash[- ](profit|income|p&l)\b", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([?.!,;])", r"\1", text)
    return text.strip(" ,;") or "What is my profit?"


def ask(
    entity_path: Path | str,
    question: str,
    *,
    today: Optional[date] = None,
) -> AskResult:
    """Answer a question and always say which basis and period the answer covers.

    A question that asks for a basis the books do not keep is refused with the
    nearest valid report, never answered on the other basis.
    """
    from ..entity import load_entity, resolve_basis
    if today is None:
        today = date.today()
    entity_path = Path(entity_path)
    try:
        kept = resolve_basis(load_entity(entity_path).entity_config)
    except Exception:
        kept = None
    q_lower = question.lower()
    from .. import ask_intents
    cash_flow = bool(ask_intents.CASH_FLOW.search(q_lower))
    wanted = "cash" if _CASH_VIEW.search(q_lower) or cash_flow else "accrual" if _ACCRUAL_VIEW.search(q_lower) else None
    from_date, to_date = _parse_period(question, today)
    named = from_date is not None
    if from_date is None:
        from_date, to_date = date(today.year, 1, 1), today
    span = f"from {from_date} to {to_date}"
    if kept and wanted and wanted != kept:
        options = [f"- Bank movements for the period: books report general-ledger --entity {entity_path} --from {from_date} --to {to_date} (read the Assets:Bank accounts)."]
        if kept == "accrual":
            options.append(f"- The same period on the accrual basis: books ask --entity {entity_path} \""
                           + (f"What was our net income from {from_date} to {to_date}?" if cash_flow else _without_cash_wording(question)) + "\"")
            options.append(f"- Future cash only: books forecast {entity_path}")
        text = (f"Not available: you asked for the {wanted} basis, but these books are kept on the {kept} basis, "
                f"so no {wanted}-basis figure is computed. Period asked: {span}.\n" + "\n".join(options))
        return AskResult(question=question, intent="unavailable_basis", answer_text=text,
                         data={"unavailable": "basis_not_kept", "requested_basis": wanted, "books_basis": kept,
                               "from_date": str(from_date), "to_date": str(to_date)})
    names = ask_intents.match(question)
    routed = None
    if names:
        as_of = to_date if named else today
        routed = ask_intents.answer(entity_path, question, names, kept, as_of)
    if routed is not None:
        result = AskResult(question=question, intent=routed.intent, answer_text=routed.text, data=routed.data)
        note = "" if named else " No period was named in the question, so this uses today's date."
        result.answer_text = f"Answer covers: {kept or 'unknown'} basis, from {routed.data['from_date']} to {routed.data['to_date']}.{note}\n\n" + result.answer_text
        result.data.setdefault("basis", kept)
        return result
    result = _ask_core(entity_path, question, today=today)
    if result.intent == "unknown":
        return result
    answered_from, answered_to = result.data.get("from_date", str(from_date)), result.data.get("to_date", str(to_date))
    note = "" if named else " No period was named in the question, so this covers the year to date."
    result.answer_text = (f"Answer covers: {kept or 'unknown'} basis, from {answered_from} to {answered_to}.{note}\n\n"
                          + result.answer_text)
    result.data.setdefault("basis", kept)
    result.data.setdefault("from_date", answered_from)
    result.data.setdefault("to_date", answered_to)
    return result


def _ask_core(
    entity_path: Path | str,
    question: str,
    *,
    today: Optional[date] = None,
) -> AskResult:
    """Dispatch a plain-English question to deterministic statement/cache queries.

    Supported question shapes (origin F4):
    - Revenue/income for a period: "what was Q1 revenue?"
    - Spend by category: "how much did we spend on software last quarter?"
    - Owner draws: "what were owner draws this month?"
    - Vendor/payee totals: "how much did we pay Acme last year?"
    - Net income: "how are we doing?" / "what is our net income?"

    Unknown questions get a helpful guidance response listing supported shapes.
    Output contains NO beancount syntax, SQL, or internal jargon.
    """
    entity_path = Path(entity_path)
    if today is None:
        from datetime import date as _date
        today = _date.today()

    q_lower = question.lower()

    # --- Determine intent ---------------------------------------------------
    is_revenue = bool(re.search(r"\b(revenue|income|sales|earn|earned|earnings|receipts)\b", q_lower))
    is_spend = bool(re.search(r"\b(spend|spent|expenses?|cost|costs|paid|pay)\b", q_lower))
    is_draws = bool(re.search(r"\b(draw|draws|owner pay|owner draws|distribution|distributions)\b", q_lower))
    is_vendor = bool(re.search(r"\b(?:how much did we pay|how much have we paid|paid to|payments? to)\b", q_lower))
    is_net = bool(re.search(r"\b(net income|profit|how are we doing|doing|performance|p&l)\b", q_lower))

    # draws check before spend (more specific)
    if is_draws:
        intent = "draws"
    elif is_vendor and not is_revenue:
        intent = "vendor"
    elif is_revenue and not is_spend:
        intent = "revenue"
    elif is_spend and not is_revenue:
        intent = "spend"
    elif is_net:
        intent = "net_income"
    else:
        intent = "unknown"

    from_date, to_date = _parse_period(question, today)

    # Fallback period: current year
    if from_date is None and intent != "unknown":
        from_date = date(today.year, 1, 1)
        to_date = today

    conn = open_cache(entity_path)
    try:
        from ..accrual.reports import CoverageError, basis, financial_metadata
        if basis(conn) == "accrual" and intent != "unknown":
            if is_net and not is_draws and not is_vendor:
                intent = "net_income"
            # Use the same coverage checks and recognition totals as statements.
            # A summarized opening cannot answer an arbitrary historical period.
            metadata = financial_metadata(conn, "pnl", from_date, to_date)
            context = StatementResult("pnl", from_date, to_date, meta=metadata).to_text()
            dates = {"from_date": str(from_date), "to_date": str(to_date), "meta": metadata}
            if intent == "vendor" or (intent in {"revenue", "spend"} and re.search(r"\b(paid|pay|payments?|receipts)\b", q_lower)):
                return AskResult(question=question, intent=intent,
                    answer_text="Cash payments and receipts require linked bank movements and settlement evidence. Use the general ledger and settlement reports to inspect those movements.\n\n" + context,
                    data={**dates, "unavailable": "cash_payment_linkage_required"})
            if intent == "draws":
                if metadata.get("summary_included"):
                    raise CoverageError("Owner draw activity before cutover is unavailable from the imported fiscal summary; request a post-cutover period.")
                balances = _account_balances_in_range(conn, from_date, to_date)
                total = sum((n for a, n in balances.items() if _is_equity(a) and re.search(r"draw|distribution|owner", a.lower())), Decimal("0.00"))
                return AskResult(question=question, intent=intent,
                    answer_text=f"Recorded owner draws {_period_label(from_date, to_date)}: {_fmt(total)}\n\n" + context,
                    data={**dates, "total_draws": str(total)})
            pnl = _compute_pnl(conn, from_date, to_date)
            if intent == "revenue":
                total, key, label = pnl.sections[0]["total"], "total_revenue", "Recognized revenue"
            elif intent == "spend":
                category = _extract_category(question)
                total = (sum((r["amount"] for r in pnl.sections[1]["rows"] if category.lower() in r["label"].lower()), Decimal("0.00"))
                         if category else pnl.sections[1]["total"])
                key, label = "total_expenses", "Recognized expenses" + (f" matching '{category}'" if category else "")
            else:
                total, key, label = pnl.totals["net_income"], "net_income", "Net income"
            return AskResult(question=question, intent=intent,
                answer_text=f"{label} {_period_label(from_date, to_date)}: {_fmt(total)}\n\n" + pnl.to_text(),
                data={**dates, key: str(total)})
        if intent == "revenue":
            balances = _account_balances_in_range(conn, from_date, to_date)
            total = sum(
                -bal for acc, bal in balances.items() if _is_income(acc)
            )
            period_str = _period_label(from_date, to_date)
            answer = (
                f"Total revenue {period_str}: {_fmt(total)}\n\n"
                + _income_breakdown(balances)
            )
            return AskResult(
                question=question,
                intent="revenue",
                answer_text=answer,
                data={"total_revenue": str(total), "from_date": str(from_date), "to_date": str(to_date)},
            )

        elif intent == "spend":
            category = _extract_category(question)
            balances = _account_balances_in_range(conn, from_date, to_date)
            if category:
                matching = {
                    acc: bal
                    for acc, bal in balances.items()
                    if _is_expense(acc) and category.lower() in acc.lower().replace(":", " ").lower()
                }
                if matching:
                    total = sum(bal for bal in matching.values())
                    lines = [f"  {_render_account(a)}: {_fmt(v)}" for a, v in sorted(matching.items())]
                    period_str = _period_label(from_date, to_date)
                    answer = (
                        f"Spending on '{category}' {period_str}: {_fmt(total)}\n"
                        + "\n".join(lines)
                    )
                    return AskResult(
                        question=question,
                        intent="spend",
                        answer_text=answer,
                        data={"total": str(total), "category": category, "from_date": str(from_date), "to_date": str(to_date)},
                    )
                else:
                    answer = f"No spending found matching '{category}' {_period_label(from_date, to_date)}."
                    return AskResult(
                        question=question, intent="spend", answer_text=answer,
                        data={"total": "0.00", "category": category},
                    )
            else:
                total = sum(bal for acc, bal in balances.items() if _is_expense(acc))
                period_str = _period_label(from_date, to_date)
                answer = (
                    f"Total spending {period_str}: {_fmt(total)}\n\n"
                    + _expense_breakdown(balances)
                )
                return AskResult(
                    question=question,
                    intent="spend",
                    answer_text=answer,
                    data={"total_expenses": str(total), "from_date": str(from_date), "to_date": str(to_date)},
                )

        elif intent == "draws":
            balances = _account_balances_in_range(conn, from_date, to_date)
            draw_accounts = {
                acc: bal
                for acc, bal in balances.items()
                if re.search(r"draw|distribution|owner", acc.lower())
            }
            total = sum(bal for bal in draw_accounts.values())
            period_str = _period_label(from_date, to_date)
            if draw_accounts:
                lines = [f"  {_render_account(a)}: {_fmt(v)}" for a, v in sorted(draw_accounts.items())]
                answer = (
                    f"Owner draws {period_str}: {_fmt(total)}\n"
                    + "\n".join(lines)
                )
            else:
                answer = f"No owner draw accounts found {period_str}."
                total = Decimal("0.00")
            return AskResult(
                question=question,
                intent="draws",
                answer_text=answer,
                data={"total_draws": str(total), "from_date": str(from_date), "to_date": str(to_date)},
            )

        elif intent == "vendor":
            vendor = _extract_vendor(question)
            if not vendor:
                answer = (
                    "I couldn't determine the vendor name from your question.\n"
                    "Try: 'how much did we pay Acme last quarter?'"
                )
                return AskResult(question=question, intent="vendor", answer_text=answer)

            from_date_v, to_date_v = _parse_period(question, today)
            if from_date_v is None:
                from_date_v = date(today.year, 1, 1)
                to_date_v = today

            # Sum postings where payee matches vendor name (case-insensitive)
            total = Decimal("0.00")
            matched_rows = []
            for entry_date, narration, payee, account, amount, currency in iter_postings(
                conn, from_date=from_date_v, to_date=to_date_v
            ):
                p = payee or narration
                if vendor.lower() in p.lower():
                    if _is_expense(account) or _is_asset(account):
                        total += amount
                        matched_rows.append((entry_date, p, _render_account(account), amount))

            period_str = _period_label(from_date_v, to_date_v)
            if matched_rows:
                lines = [f"  {r[0]}  {r[1]}  {r[2]}  {_fmt(r[3])}" for r in matched_rows[:20]]
                answer = (
                    f"Payments to '{vendor}' {period_str}: {_fmt(total)}\n"
                    + "\n".join(lines)
                )
            else:
                answer = f"No payments found to '{vendor}' {period_str}."
            return AskResult(
                question=question,
                intent="vendor",
                answer_text=answer,
                data={"vendor": vendor, "total": str(total), "from_date": str(from_date_v), "to_date": str(to_date_v)},
            )

        elif intent == "net_income":
            pnl = _compute_pnl(conn, from_date, to_date)
            net = pnl.totals["net_income"]
            period_str = _period_label(from_date, to_date)
            total_income = pnl.sections[0]["total"]
            total_expenses = pnl.sections[1]["total"]
            sign = "profit" if net >= 0 else "loss"
            answer = (
                f"Net {sign} {period_str}: {_fmt(abs(net))}\n"
                f"  Revenue:  {_fmt(total_income)}\n"
                f"  Expenses: {_fmt(total_expenses)}\n"
                f"  Net:      {_fmt(net)}"
            )
            return AskResult(
                question=question,
                intent="net_income",
                answer_text=answer,
                data={"net_income": str(net), "from_date": str(from_date), "to_date": str(to_date)},
            )

        else:
            # Unknown intent — helpful guidance
            from .. import ask_intents
            answer = ask_intents.SUPPORTED_TEXT
            return AskResult(question=question, intent="unknown", answer_text=answer)
    finally:
        conn.close()


def _period_label(from_date: Optional[date], to_date: Optional[date]) -> str:
    if from_date and to_date:
        if from_date == to_date:
            return f"on {from_date}"
        return f"from {from_date} to {to_date}"
    return ""


def _income_breakdown(balances: dict[str, Decimal]) -> str:
    lines = []
    for acc, bal in sorted(balances.items()):
        if _is_income(acc):
            lines.append(f"  {_render_account(acc)}: {_fmt(-bal)}")
    return "\n".join(lines)


def _expense_breakdown(balances: dict[str, Decimal]) -> str:
    lines = []
    for acc, bal in sorted(balances.items()):
        if _is_expense(acc):
            lines.append(f"  {_render_account(acc)}: {_fmt(bal)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------


def _write_output(result: StatementResult, output_path: Optional[Path], fmt: str) -> None:
    if fmt == "json":
        content = result.to_json()
    else:
        content = result.to_text()

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
        print(f"Written to {output_path}")
    else:
        print(content)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def add_parser(subparsers: Any) -> None:
    """Register the ``report`` and ``ask`` subcommands."""
    # ---- report group -------------------------------------------------------
    report_parser = subparsers.add_parser("report", help="Generate financial reports")
    report_sub = report_parser.add_subparsers(dest="report_command", required=True)

    _common_args = lambda p: (
        p.add_argument("--entity", required=True, help="Path to entity directory"),
        p.add_argument("--output", default=None, help="Output file path"),
        p.add_argument("--format", choices=["text", "json"], default="text", dest="fmt"),
    )

    # pnl
    pnl_p = report_sub.add_parser("pnl", help="Profit and loss statement")
    pnl_p.add_argument("--from", dest="from_date", required=True, help="Start date (YYYY-MM-DD)")
    pnl_p.add_argument("--to", dest="to_date", required=True, help="End date (YYYY-MM-DD)")
    _common_args(pnl_p)

    # balance-sheet
    bs_p = report_sub.add_parser("balance-sheet", help="Balance sheet")
    bs_p.add_argument("--as-of", dest="as_of", required=True, help="As-of date (YYYY-MM-DD)")
    _common_args(bs_p)

    # trial-balance
    tb_p = report_sub.add_parser("trial-balance", help="Trial balance")
    tb_p.add_argument("--as-of", dest="as_of", required=True, help="As-of date (YYYY-MM-DD)")
    _common_args(tb_p)

    # general-ledger
    gl_p = report_sub.add_parser("general-ledger", help="General ledger")
    gl_p.add_argument("--from", dest="from_date", required=True, help="Start date (YYYY-MM-DD)")
    gl_p.add_argument("--to", dest="to_date", required=True, help="End date (YYYY-MM-DD)")
    _common_args(gl_p)

    # ---- ask ----------------------------------------------------------------
    ask_p = subparsers.add_parser("ask", help="Ask a plain-English question about the books")
    ask_p.add_argument("--entity", required=True, help="Path to entity directory")
    ask_p.add_argument("question", help="Your question")
    ask_p.add_argument("--today", default=None, help="Override today's date (YYYY-MM-DD) for deterministic tests")


def _coverage_failure(args: Any, exc: Exception) -> int:
    """Print a clean coverage error with the first available date and the corrected command."""
    first, field = getattr(exc, "first_date", None), getattr(exc, "field", None)
    command = getattr(args, "report_command", None)
    lines = [f"Error: {exc}"]
    if first and command:
        parts = [f"books report {command} --entity {args.entity}"]
        if command in {"pnl", "general-ledger"}:
            from_date, to_date = args.from_date, args.to_date
            if field == "from":
                from_date = first
                to_date = max(to_date, first)
            else:
                to_date = first
                from_date = min(from_date, first)
            parts.append(f"--from {from_date} --to {to_date}")
        else:
            parts.append(f"--as-of {first}")
        if getattr(args, "output", None):
            parts.append(f"--output {args.output}")
        if getattr(args, "fmt", "text") != "text":
            parts.append(f"--format {args.fmt}")
        lines.append(f"The first available date is {first}. Run: {' '.join(parts)}")
    print("\n".join(lines), file=sys.stderr)
    return 1


def run(args: Any) -> int:
    """Dispatch report or ask command."""
    from ..accrual.reports import CoverageError
    if getattr(args, "command", None) == "report":
        try:
            return _run(args)
        except CoverageError as exc:
            return _coverage_failure(args, exc)
    return _run(args)


def _run(args: Any) -> int:
    """Dispatch report or ask command."""
    cmd = getattr(args, "command", None)

    if cmd == "ask":
        entity = Path(args.entity)
        today_override = date.fromisoformat(args.today) if args.today else None
        result = ask(entity, args.question, today=today_override)
        print(result.answer_text)
        return 0

    if cmd == "report":
        entity = Path(args.entity)
        output = Path(args.output) if args.output else None
        fmt = getattr(args, "fmt", "text")
        rc = getattr(args, "report_command", None)

        if rc == "pnl":
            from_date = date.fromisoformat(args.from_date)
            to_date = date.fromisoformat(args.to_date)
            result = profit_and_loss(entity, from_date, to_date)
            _write_output(result, output, fmt)
            return 0

        if rc == "balance-sheet":
            as_of = date.fromisoformat(args.as_of)
            result = balance_sheet(entity, as_of)
            _write_output(result, output, fmt)
            return 0

        if rc == "trial-balance":
            as_of = date.fromisoformat(args.as_of)
            result = trial_balance(entity, as_of)
            _write_output(result, output, fmt)
            return 0

        if rc == "general-ledger":
            from_date = date.fromisoformat(args.from_date)
            to_date = date.fromisoformat(args.to_date)
            result = general_ledger(entity, from_date, to_date)
            _write_output(result, output, fmt)
            return 0

        print(f"Unknown report command: {rc}", file=sys.stderr)
        return 2

    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 2
