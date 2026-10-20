"""Comparative periods for the P&L, the balance sheet and the cash flow statement.

``--compare previous`` and ``--compare prior_year`` compute the same statement for a
second period and join the two by line.  The engine does all of the math with
``Decimal``.  A consumer only displays the fields.

Prior period
------------
Profit and loss and cash flow (a date range):

``previous``    The period just before the requested one.  If the range is whole
                calendar months (the first day of a month to the last day of a
                month), the prior period is the same number of whole months
                before it: a month gives the month before, a quarter gives the
                quarter before, a year gives the year before.  Any other range
                shifts back by its own length in days.
``prior_year``  The same dates one year earlier.  A month-end date stays a
                month-end date (2028-02-29 becomes 2027-02-28).

Balance sheet (a date):

``previous``    The same day one month earlier.  A month-end date stays a
                month-end date (2026-03-31 becomes 2026-02-28).
``prior_year``  The same date one year earlier, with the same month-end rule.

Per-line fields
---------------
``amount``          Current amount, exact to the cent.
``prior_amount``    Prior amount, or ``null`` when the prior period has no data.
``change``          ``amount - prior_amount``, exact, or ``null``.
``change_pct``      ``change / |prior_amount| * 100``, or ``null`` when the prior
                    amount is zero or missing.
``pct_of_income``   (P&L only) ``amount / total revenue * 100`` for the current
                    period, or ``null`` when total revenue is zero.
``prior_pct_of_income``  The same ratio for the prior period.

Rounding rule
-------------
Amounts are never rounded: they are exact two-place decimals.  Every percentage is
rounded once, to two decimal places, half away from zero (``ROUND_HALF_UP``).

No prior data
-------------
The prior period has no data when no ledger entry falls in a prior range (P&L and
cash flow) or on or before the prior date (balance sheet), or when the prior period
starts before the books' imported coverage.  Then ``comparison.prior.has_data`` is
``false``, ``comparison.prior.note`` says why, and every prior and change field is
``null``.  The current period is still reported in full.
"""
from __future__ import annotations

import calendar
import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Optional

from .cache import open_cache
from .structure import SCHEMA_VERSION, build_structure, pnl_structure, structured_payload

MODES = ("previous", "prior_year")
ROUNDING_RULE = ("Amounts are exact cents. Percentages are rounded once to 2 decimal places, "
                 "half away from zero (ROUND_HALF_UP).")
_PCT = Decimal("0.01")
ZERO = Decimal("0.00")


# ---- date rules ------------------------------------------------------------


def _is_month_end(d: date) -> bool:
    return d.day == calendar.monthrange(d.year, d.month)[1]


def add_months(d: date, months: int) -> date:
    """Shift by whole months.  A month-end stays a month-end; otherwise the day is clamped."""
    index = d.year * 12 + (d.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, last if _is_month_end(d) else min(d.day, last))


def prior_range(from_date: date, to_date: date, mode: str) -> tuple[date, date]:
    if mode == "prior_year":
        return add_months(from_date, -12), add_months(to_date, -12)
    if mode != "previous":
        raise ValueError(f"Unknown comparison {mode}. Use one of: {', '.join(MODES)}.")
    if from_date.day == 1 and _is_month_end(to_date):
        months = (to_date.year - from_date.year) * 12 + to_date.month - from_date.month + 1
        return add_months(from_date, -months), from_date - timedelta(days=1)
    days = (to_date - from_date).days + 1
    prior_to = from_date - timedelta(days=1)
    return prior_to - timedelta(days=days - 1), prior_to


def prior_date(as_of: date, mode: str) -> date:
    if mode == "prior_year":
        return add_months(as_of, -12)
    if mode != "previous":
        raise ValueError(f"Unknown comparison {mode}. Use one of: {', '.join(MODES)}.")
    return add_months(as_of, -1)


# ---- percentage math -------------------------------------------------------


def pct(numerator: Decimal, denominator: Decimal) -> Optional[Decimal]:
    if denominator == 0:
        return None
    return (numerator / denominator * 100).quantize(_PCT, rounding=ROUND_HALF_UP)


def _s(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(value)


# ---- merging two structures --------------------------------------------------


def merge_lines(current: list[dict], prior: Optional[list[dict]], income_key: Optional[str] = None) -> list[dict]:
    """Join the current and prior lines by key.  ``prior=None`` means no prior data."""
    def amount(line: Optional[dict]) -> Decimal:
        return Decimal(line["amount"]) if line and "amount" in line else ZERO

    prior_by_key = {l["key"]: l for l in (prior or []) if l["type"] != "section"}
    cur_by_key = {l["key"]: l for l in current if l["type"] != "section"}
    merged = [dict(l) for l in current]

    if prior is not None:
        # A prior-only row goes in its section, before the subtotal.  A prior-only
        # section goes before the first statement total.  A prior-only statement
        # total is not carried over.
        def section_end(section: str) -> int:
            rows = [i for i, l in enumerate(merged) if l.get("section") == section]
            if rows:
                return rows[-1] + 1
            return next(i for i, l in enumerate(merged) if l["type"] == "section" and l["key"] == section) + 1

        headers = {l["key"]: l for l in prior if l["type"] == "section"}
        for line in prior:
            if line["type"] in {"section", "total"} or line["key"] in cur_by_key:
                continue
            section = line.get("section")
            if not any(l["type"] == "section" and l["key"] == section for l in merged):
                at = next((i for i, l in enumerate(merged) if l["type"] == "total"), len(merged))
                merged.insert(at, dict(headers[section]))
            if line["type"] == "subtotal":
                merged.insert(section_end(section), {**line, "amount": str(ZERO)})
            else:
                # Keep account order: before the first later-named account of the section.
                later = next((i for i, l in enumerate(merged) if l.get("section") == section
                              and l["type"] == "account" and l["key"] > line["key"]), None)
                if later is None or line["type"] != "account":
                    later = next((i for i, l in enumerate(merged)
                                  if l["type"] == "subtotal" and l.get("section") == section), section_end(section))
                merged.insert(later, {**line, "amount": str(ZERO)})

    income_cur = amount(cur_by_key.get(income_key)) if income_key else None
    income_prior = amount(prior_by_key.get(income_key)) if income_key and prior is not None else None
    out: list[dict] = []
    for line in merged:
        if line["type"] == "section":
            out.append(line)
            continue
        cur = amount(line)
        row = dict(line)
        row["amount"] = _s(cur)
        if prior is None:
            row.update(prior_amount=None, change=None, change_pct=None)
        else:
            old = amount(prior_by_key.get(line["key"]))
            change = cur - old
            row.update(prior_amount=_s(old), change=_s(change), change_pct=_s(pct(change, abs(old))))
        if income_key:
            row["pct_of_income"] = _s(pct(cur, income_cur))
            row["prior_pct_of_income"] = None if prior is None else _s(pct(amount(prior_by_key.get(line["key"])), income_prior))
        out.append(row)
    return out


# ---- result ----------------------------------------------------------------


@dataclass
class ComparativeResult:
    kind: str
    mode: str
    base_payload: dict
    lines: list[dict]
    prior_info: dict
    prior_totals: Optional[dict]
    ties: bool = True
    tie_difference: str = "0.00"

    def payload(self) -> dict:
        payload = dict(self.base_payload)
        payload["schema_version"] = SCHEMA_VERSION
        structure = dict(payload.get("structure", {}))
        structure.update(version=SCHEMA_VERSION, lines=self.lines)
        payload["structure"] = structure
        payload["comparison"] = {"mode": self.mode, "prior": self.prior_info, "rounding": ROUNDING_RULE}
        payload["prior_totals"] = self.prior_totals
        return payload

    def to_json(self) -> str:
        return json.dumps(self.payload(), indent=2, sort_keys=False)

    def to_text(self) -> str:
        p = self.payload()
        title = {"pnl": "Profit and Loss", "balance_sheet": "Balance Sheet", "cashflow": "Statement of Cash Flows"}[self.kind]
        cur = p["to_date"] if self.kind == "balance_sheet" else f"{p['from_date']} through {p['to_date']}"
        prior = self.prior_info
        prior_label = (prior["to_date"] if self.kind == "balance_sheet" else f"{prior['from_date']} through {prior['to_date']}")
        head = f"{title}  {cur}  compared with  {prior_label}"
        out = [head, "=" * len(head)]
        if not prior["has_data"]:
            out.append(f"No prior period data: {prior['note']}")
        show_income = self.kind == "pnl"
        label_width = 48
        header = (f"{'':<{label_width}} {'Current':>13} {'Prior':>13} {'Change':>13} {'Change %':>9}"
                  + (f" {'% income':>9}" if show_income else ""))
        out += ["", header, "-" * len(header)]

        def cell(value, width):
            return f"{'-':>{width}}" if value is None else f"{Decimal(value):>{width},.2f}"

        for line in self.lines:
            if line["type"] == "section":
                out += ["", line["label"]]
                continue
            indent = "  " * line.get("indent", 0)
            row = (f"{indent}{line['label']:<{label_width - len(indent)}} "
                   f"{cell(line['amount'], 13)} {cell(line['prior_amount'], 13)} {cell(line['change'], 13)} "
                   f"{cell(line['change_pct'], 9)}")
            if show_income:
                row += f" {cell(line.get('pct_of_income'), 9)}"
            out.append(row)
        out += ["", ROUNDING_RULE]
        return "\n".join(out)


def _has_postings(conn: Any, start: Optional[date], end: date) -> bool:
    sql = "SELECT 1 FROM postings p JOIN entries e ON e.id = p.entry_id WHERE e.date <= ?"
    params: list = [end.isoformat()]
    if start is not None:
        sql += " AND e.date >= ?"
        params.append(start.isoformat())
    return conn.execute(sql + " LIMIT 1", params).fetchone() is not None


def _prior_data(entity: Path, start: Optional[date], end: date) -> bool:
    conn = open_cache(entity)
    try:
        return _has_postings(conn, start, end)
    finally:
        conn.close()


def _no_data_note(start: Optional[date], end: date) -> str:
    span = f"on or before {end}" if start is None else f"from {start} through {end}"
    return f"the books have no entries {span}."


def _prior_info(start: Optional[date], end: date, has_data: bool, note: Optional[str]) -> dict:
    info: dict[str, Any] = {"has_data": has_data, "note": note}
    if start is not None:
        info["from_date"] = start.isoformat()
    info["to_date"] = end.isoformat()
    return info


def _attempt_prior(compute, entity: Path, start: Optional[date], end: date):
    """Return ``(prior_result_or_None, info)``.  Never raises for missing prior data."""
    from ..accrual.reports import CoverageError

    if not _prior_data(entity, start, end):
        return None, _prior_info(start, end, False, _no_data_note(start, end))
    try:
        return compute(), _prior_info(start, end, True, None)
    except CoverageError as exc:
        return None, _prior_info(start, end, False, f"the prior period is outside the books' imported coverage ({exc}).")


def compare_pnl(entity: Path | str, from_date: date, to_date: date, mode: str, basis: Optional[str] = None) -> ComparativeResult:
    from .statements import profit_and_loss

    entity = Path(entity)
    pf, pt = prior_range(from_date, to_date, mode)
    current = profit_and_loss(entity, from_date, to_date, basis=basis)
    prior, info = _attempt_prior(lambda: profit_and_loss(entity, pf, pt, basis=basis), entity, pf, pt)
    force = any(l["key"] == "gross_profit" for r in (current, prior) if r is not None
                for l in pnl_structure(r)["lines"])
    cur_lines = pnl_structure(current, force)["lines"]
    prior_lines = pnl_structure(prior, force)["lines"] if prior else None
    merged = merge_lines(cur_lines, prior_lines, "total_revenue")
    prior_totals = {k: str(v) for k, v in prior.totals.items()} if prior else None
    return ComparativeResult("pnl", mode, structured_payload(current), merged, info, prior_totals)


def compare_balance_sheet(entity: Path | str, as_of: date, mode: str) -> ComparativeResult:
    from .statements import balance_sheet

    entity = Path(entity)
    prior_as_of = prior_date(as_of, mode)
    current = balance_sheet(entity, as_of)
    prior, info = _attempt_prior(lambda: balance_sheet(entity, prior_as_of), entity, None, prior_as_of)
    merged = merge_lines(build_structure(current)["lines"], build_structure(prior)["lines"] if prior else None)
    prior_totals = {k: str(v) for k, v in prior.totals.items()} if prior else None
    return ComparativeResult("balance_sheet", mode, structured_payload(current), merged, info, prior_totals)


def compare_cashflow(entity: Path | str, from_date: date, to_date: date, mode: str) -> ComparativeResult:
    from .cashflow_statement import cash_flow

    entity = Path(entity)
    pf, pt = prior_range(from_date, to_date, mode)
    current = cash_flow(entity, from_date, to_date)
    prior, info = _attempt_prior(lambda: cash_flow(entity, pf, pt), entity, pf, pt)
    merged = merge_lines(current.lines, prior.lines if prior else None)
    result = ComparativeResult("cashflow", mode, current.payload(), merged, info,
                               {k: str(v) for k, v in prior.totals.items()} if prior else None)
    if prior is not None:
        info["reconciliation"] = prior.reconciliation
    ties = current.reconciliation["ties"] and (prior is None or prior.reconciliation["ties"])
    result.ties = ties
    result.tie_difference = current.reconciliation["difference"] if not current.reconciliation["ties"] \
        else (prior.reconciliation["difference"] if prior else "0.00")
    return result
