"""Accountant workbook: year-end/period export as XLSX + per-sheet CSV files.

Produces a set of CSV files (always) and optionally an XLSX workbook when
the ``xlsxwriter`` package is available.

Public API
----------
generate_accountant_package(entity_path, from_date, to_date,
                     output_dir=None, override=False) -> PackageResult
    Build the full accountant export.  Refuses when sanity checks have failures unless
    ``override=True``.

run_sanity_checks(entity_path, from_date, to_date) -> SanityResult
    Run the sanity-check suite and return a machine-readable result.

add_parser(subparsers) / run(args) — CLI surface.
    Commands: ``sanity-check`` and ``export``.

XlsxWriter guard
----------------
Guarded by ``importlib.util.find_spec("xlsxwriter")``.  When absent, CSV-only
output is produced and an enablement message is printed.  When present, the XLSX
workbook is also written using Decimal-direct write_number, accounting number
formats, frozen header rows, and constant_memory mode for the GL sheet.

Sheets (in order)
-----------------
1. Cover          — entity name, period, generated-from note, sheet index,
                    sanity-check results summary
2. P&L            — profit_and_loss for the period
3. Balance Sheet  — balance_sheet as of to_date
4. Trial Balance  — trial_balance as of to_date
5. General Ledger — period transactions with review-friendly filter columns
6. Reconciliations — list_discrepancies (all records with raw and review status)
7. Vendor-1099    — per-payee totals from GL expense postings; flag >= threshold
8. Adjustment Log  — original/reversal/correction trace rows
9. Open Questions  — structured rows from reports/open-questions.json when present

Queue-empty sanity check coupling note
---------------------------------------
queue.py does not exist yet (being built in a parallel unit).  The queue_empty
check counts open items by listing review-queue/*.json files with
status == "open" directly.  This lightweight file-system coupling is documented
here so it can be replaced by a queue.py import when that module lands.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from .statements import (
    StatementResult,
    balance_sheet,
    general_ledger,
    profit_and_loss,
    trial_balance,
)
from ..reconcile import list_discrepancies
from ..ledger.validator import parse_ledger
from ..connectors.payroll import provider_spec

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

XLSX_EXTRA_MESSAGE = (
    "Install the xlsx extra to also get the Excel workbook: "
    "pip install 'agent-books[xlsx]'"
)

_ACCT_NUM_FMT = "#,##0.00;(#,##0.00)"
_HEADER_BG_COLOR = "#D9E1F2"

# Default 1099 threshold; overridable via entity.json key vendor_1099_threshold
_DEFAULT_1099_THRESHOLD = Decimal("600.00")


def _render_account_name(account: str) -> str:
    """Render internal account names for accountant-facing workbook output."""
    return account.replace(":", " › ")


def _amount_text(amount: Any) -> str:
    if isinstance(amount, Decimal):
        return str(amount)
    return str(amount) if amount is not None else ""


def _account_type(account: str) -> str:
    return account.split(":", 1)[0] if account else ""


def _account_detail(account: str) -> str:
    return account.rsplit(":", 1)[-1] if account else ""

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class SanityCheck:
    """One sanity-check result."""

    check: str  # machine-readable name
    status: str  # "pass" | "warn" | "fail"
    detail: str  # plain-English description


@dataclass
class SanityResult:
    """Result of the full sanity-check suite."""

    checks: list[SanityCheck] = field(default_factory=list)

    @property
    def has_failures(self) -> bool:
        return any(c.status == "fail" for c in self.checks)

    @property
    def has_warnings(self) -> bool:
        return any(c.status == "warn" for c in self.checks)

    def to_dict(self) -> dict:
        return {
            "checks": [
                {"check": c.check, "status": c.status, "detail": c.detail}
                for c in self.checks
            ],
            "has_failures": self.has_failures,
            "has_warnings": self.has_warnings,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


@dataclass
class PackageResult:
    """Result of a generate_accountant_package() call."""

    output_dir: Path
    csv_files: list[Path] = field(default_factory=list)
    xlsx_file: Optional[Path] = None
    xlsx_available: bool = False
    sanity: Optional[SanityResult] = None
    override_used: bool = False
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class XlsxSheet:
    """Worksheet rows plus Excel-specific rendering metadata."""

    name: str
    rows: list[list[str]]
    table: bool = False
    tab_color: str = ""
    formula_columns: frozenset[int] = frozenset()
    numeric_columns: frozenset[int] = frozenset()
    total_label_column: Optional[int] = None
    total_label: str = "Account Total"


# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------


def _count_open_queue_items(entity_path: Path) -> int:
    """Count open review-queue items by scanning review-queue/*.json directly.

    This is a lightweight file-system coupling used because queue.py is being
    built in a parallel unit.  Replace with a queue.py import when available.
    """
    queue_dir = entity_path / "review-queue"
    if not queue_dir.is_dir():
        return 0
    count = 0
    for jf in queue_dir.glob("*.json"):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("status") == "open":
                count += 1
        except (json.JSONDecodeError, OSError):
            pass
    return count


def _run_equity_check(entity_path: Path, to_date: date) -> SanityCheck:
    """Check: assets == liabilities + equity on the balance sheet."""
    try:
        bs = balance_sheet(entity_path, to_date)
        total_assets = bs.totals.get("total_assets", Decimal("0.00"))
        total_le = bs.totals.get("total_liabilities_and_equity", Decimal("0.00"))
        diff = abs(total_assets - total_le)
        if diff < Decimal("0.01"):
            return SanityCheck(
                check="equity_reconciliation",
                status="pass",
                detail=(
                    f"The balance sheet is in balance as of {to_date}: "
                    f"assets equal liabilities plus equity ({total_assets:,.2f})."
                ),
            )
        else:
            return SanityCheck(
                check="equity_reconciliation",
                status="fail",
                detail=(
                    f"The balance sheet does not balance as of {to_date}: "
                    f"assets are {total_assets:,.2f} but liabilities plus equity are "
                    f"{total_le:,.2f} (difference: {diff:,.2f})."
                ),
            )
    except Exception as exc:
        return SanityCheck(
            check="equity_reconciliation",
            status="fail",
            detail=f"Could not compute balance sheet: {exc}",
        )


def _run_queue_empty_check(entity_path: Path) -> SanityCheck:
    """Check: no open review-queue items."""
    count = _count_open_queue_items(entity_path)
    if count == 0:
        return SanityCheck(
            check="queue_empty",
            status="pass",
            detail="There are no items waiting for review in the queue.",
        )
    else:
        return SanityCheck(
            check="queue_empty",
            status="fail",
            detail=(
                f"There are {count} item(s) waiting for review in the queue. "
                "Review and confirm all items before generating the final export, "
                "or use --override to proceed anyway."
            ),
        )


def _run_reconciliation_clean_check(entity_path: Path) -> SanityCheck:
    """Check: no open reconciliation discrepancies."""
    try:
        open_discs = list_discrepancies(entity_path, status="open")
        if not open_discs:
            return SanityCheck(
                check="reconciliation_clean",
                status="pass",
                detail="All reconciliation records are clean or resolved.",
            )
        else:
            accts = [r.get("account", "?") for r in open_discs[:3]]
            more = f" (and {len(open_discs) - 3} more)" if len(open_discs) > 3 else ""
            return SanityCheck(
                check="reconciliation_clean",
                status="warn",
                detail=(
                    f"There are {len(open_discs)} open reconciliation discrepancy/discrepancies: "
                    + ", ".join(accts) + more + ". "
                    "Review these before delivering the final export."
                ),
            )
    except Exception as exc:
        return SanityCheck(
            check="reconciliation_clean",
            status="warn",
            detail=f"Could not check reconciliation records: {exc}",
        )


def _load_entity_config(entity_path: Path) -> dict[str, Any]:
    entity_json = entity_path / "entity.json"
    try:
        data = json.loads(entity_json.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _run_entity_metadata_check(entity_path: Path) -> SanityCheck:
    """Check: jurisdiction context exists for accountant review."""
    config = _load_entity_config(entity_path)
    missing = [
        label
        for key, label in (
            ("country", "country"),
            ("tax_jurisdiction", "tax jurisdiction"),
            ("operating_currency", "operating currency"),
        )
        if not str(config.get(key) or "").strip()
    ]
    if missing:
        return SanityCheck(
            check="entity_metadata",
            status="warn",
            detail=(
                "Entity setup is missing "
                + ", ".join(missing)
                + ". Add this context before relying on the accountant export."
            ),
        )
    return SanityCheck(
        check="entity_metadata",
        status="pass",
        detail=(
            "Entity jurisdiction context is present: "
            f"{config.get('country')} / {config.get('tax_jurisdiction')}, "
            f"currency {config.get('operating_currency')}."
        ),
    )


def _run_indirect_tax_scope_check(entity_path: Path) -> SanityCheck:
    """Check: VAT/GST/sales-tax applicability is only flagged, not calculated."""
    config = _load_entity_config(entity_path)
    indirect_tax = config.get("indirect_tax")
    tax_type = ""
    registered = False
    if isinstance(indirect_tax, dict):
        registered = bool(indirect_tax.get("registered"))
        tax_type = str(indirect_tax.get("type") or "").strip().upper()
    if registered or tax_type:
        label = tax_type or "indirect tax"
        return SanityCheck(
            check="indirect_tax_scope",
            status="warn",
            detail=(
                f"This entity is marked as {label}-registered/applicable. "
                "/books does not calculate, file, or advise on VAT, GST, sales tax, "
                "reverse charge, recoverability, or invoice requirements. Have a local "
                "accountant review the treatment."
            ),
        )
    return SanityCheck(
        check="indirect_tax_scope",
        status="pass",
        detail="No VAT/GST/sales-tax registration is marked in entity.json.",
    )


def _run_currency_scope_check(entity_path: Path) -> SanityCheck:
    """Check: flag transaction currencies outside the operating currency."""
    config = _load_entity_config(entity_path)
    operating_currency = str(config.get("operating_currency") or "").strip().upper()
    if not operating_currency:
        return SanityCheck(
            check="currency_scope",
            status="warn",
            detail="No operating currency is set in entity.json.",
        )

    books_path = entity_path / "books.beancount"
    try:
        parsed = parse_ledger(books_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return SanityCheck(
            check="currency_scope",
            status="warn",
            detail=f"Could not inspect ledger currencies: {exc}",
        )

    currencies = sorted({
        posting.currency
        for entry in parsed.get("entries", [])
        for posting in entry.postings
        if posting.currency
    })
    extra = [currency for currency in currencies if currency != operating_currency]
    if extra:
        return SanityCheck(
            check="currency_scope",
            status="warn",
            detail=(
                f"The ledger contains currencies outside the operating currency "
                f"{operating_currency}: {', '.join(extra)}. /books preserves these "
                "amounts but does not calculate foreign-exchange gains/losses or "
                "multi-currency reporting."
            ),
        )
    return SanityCheck(
        check="currency_scope",
        status="pass",
        detail=f"All inspected ledger postings use the operating currency {operating_currency}.",
    )


def _run_payroll_reports_check(entity_path: Path, from_date: date, to_date: date) -> SanityCheck:
    """Check: payroll provider reports are present when payroll is enabled."""
    config = _load_entity_config(entity_path)
    payroll = config.get("payroll")
    if not isinstance(payroll, dict) or not payroll.get("enabled"):
        return SanityCheck(
            check="payroll_reports",
            status="pass",
            detail="Payroll is not marked as enabled in entity.json.",
        )

    spec = provider_spec(str(payroll.get("provider") or "other"))
    payroll_dir = entity_path / "ingestion" / "payroll"
    candidates = []
    if payroll_dir.is_dir():
        candidates = [
            path for path in payroll_dir.iterdir()
            if path.is_file()
            and not path.name.startswith(".")
            and path.suffix.lower() in {".csv", ".xlsx", ".xls", ".pdf", ".json"}
        ]

    if not candidates:
        return SanityCheck(
            check="payroll_reports",
            status="warn",
            detail=(
                f"Payroll is enabled with {spec.display_name}, but no payroll report files "
                f"were found in ingestion/payroll for {from_date} to {to_date}. "
                f"{spec.report_hint} Payroll journal entries should be draft/accountant-confirmed; "
                "/books does not calculate wages, withholdings, benefits, taxes, or filings."
            ),
        )

    return SanityCheck(
        check="payroll_reports",
        status="warn",
        detail=(
            f"Found {len(candidates)} payroll report file(s) in ingestion/payroll for "
            f"{spec.display_name}. Review them before confirming any draft payroll journal entries; "
            "/books does not calculate payroll or compliance obligations."
        ),
    )


def _run_yoy_pnl_check(
    entity_path: Path,
    from_date: date,
    to_date: date,
) -> SanityCheck:
    """Check year-over-year P&L variance when prior-period data exists."""
    no_prior_detail = (
        "No prior-year P&L transaction history is available in /books for "
        "year-over-year comparison. QuickBooks reference exports or opening "
        "balance snapshots may still exist, but this check only compares "
        "/books ledger activity."
    )
    period_days = (to_date - from_date).days + 1
    prior_to = date(from_date.year - 1, from_date.month, from_date.day) - __import__("datetime").timedelta(days=1)
    prior_from = date(
        prior_to.year,
        prior_to.month - (period_days // 30) if period_days // 30 < prior_to.month else 1,
        1,
    )
    # Simplify: use same calendar span one year earlier
    try:
        import calendar
        prior_from = date(from_date.year - 1, from_date.month, from_date.day)
        prior_to = date(to_date.year - 1, to_date.month, to_date.day)
    except ValueError:
        # Edge case: Feb 29 in a leap year
        prior_from = from_date.replace(year=from_date.year - 1)
        prior_to = to_date.replace(year=to_date.year - 1)

    try:
        current_pnl = profit_and_loss(entity_path, from_date, to_date)
        prior_pnl = profit_and_loss(entity_path, prior_from, prior_to)
    except Exception:
        return SanityCheck(
            check="yoy_pnl_variance",
            status="warn",
            detail=no_prior_detail,
        )

    # Check if prior period has any data
    prior_income = prior_pnl.sections[0]["total"] if prior_pnl.sections else Decimal("0.00")
    prior_expense = prior_pnl.sections[1]["total"] if len(prior_pnl.sections) > 1 else Decimal("0.00")

    if prior_income == Decimal("0.00") and prior_expense == Decimal("0.00"):
        return SanityCheck(
            check="yoy_pnl_variance",
            status="warn",
            detail=no_prior_detail,
        )

    # Compare category-level moves
    current_by_cat: dict[str, Decimal] = {}
    for section in current_pnl.sections:
        for row in section.get("rows", []):
            current_by_cat[row["label"]] = row.get("amount", Decimal("0.00"))

    prior_by_cat: dict[str, Decimal] = {}
    for section in prior_pnl.sections:
        for row in section.get("rows", []):
            prior_by_cat[row["label"]] = row.get("amount", Decimal("0.00"))

    flagged: list[str] = []
    for cat, cur_amt in current_by_cat.items():
        prior_amt = prior_by_cat.get(cat, Decimal("0.00"))
        if prior_amt != Decimal("0.00"):
            pct_change = abs(cur_amt - prior_amt) / abs(prior_amt) * 100
            if pct_change > Decimal("25"):
                direction = "up" if cur_amt > prior_amt else "down"
                flagged.append(f"{cat} ({direction} {pct_change:.0f}%)")

    if flagged:
        return SanityCheck(
            check="yoy_pnl_variance",
            status="warn",
            detail=(
                "The following categories moved more than 25% compared to the same period "
                f"last year: {'; '.join(flagged[:5])}."
                + (" (and more)" if len(flagged) > 5 else "")
            ),
        )
    return SanityCheck(
        check="yoy_pnl_variance",
        status="pass",
        detail="No category moved more than 25% compared to the same period last year.",
    )


def run_sanity_checks(
    entity_path: Path | str,
    from_date: date,
    to_date: date,
) -> SanityResult:
    """Run the full sanity-check suite.

    Returns a :class:`SanityResult` whose ``has_failures`` attribute is True
    when any check has status "fail".  Exit code 0 only when no failures.
    """
    entity_path = Path(entity_path)
    checks: list[SanityCheck] = [
        _run_entity_metadata_check(entity_path),
        _run_indirect_tax_scope_check(entity_path),
        _run_currency_scope_check(entity_path),
        _run_payroll_reports_check(entity_path, from_date, to_date),
        _run_yoy_pnl_check(entity_path, from_date, to_date),
        _run_equity_check(entity_path, to_date),
        _run_queue_empty_check(entity_path),
        _run_reconciliation_clean_check(entity_path),
    ]
    return SanityResult(checks=checks)


# ---------------------------------------------------------------------------
# Sheet data builders
# ---------------------------------------------------------------------------


def _build_cover_rows(
    entity_path: Path,
    entity_name: str,
    from_date: date,
    to_date: date,
    sanity: SanityResult,
    override_used: bool,
) -> list[list[str]]:
    """Build cover/README sheet rows."""
    rows: list[list[str]] = [
        ["Field", "Value"],
        ["Business", entity_name],
        ["Period start", str(from_date)],
        ["Period end", str(to_date)],
        ["Generated", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")],
        ["Source", str(entity_path)],
        [],
        ["Sheet index", ""],
        ["1. Cover", "This sheet"],
        ["2. P&L", "Revenue and expenses for the period"],
        ["3. Balance Sheet", "Assets, liabilities, and equity as of the period end"],
        ["4. Trial Balance", "All account balances as of the period end"],
        ["5. General Ledger", "Every posted transaction in the period"],
        ["6. Reconciliations", "Account reconciliation status"],
        ["7. Vendor 1099", "Vendor totals and potential 1099 candidates"],
        ["8. Adjustment Log", "Corrected and reversed entries"],
        ["9. Open Questions", "Structured questions and owner responses for accountant review"],
        [],
        ["Sanity checks", ""],
    ]
    for c in sanity.checks:
        rows.append([c.check, f"[{c.status.upper()}] {c.detail}"])
    if override_used:
        rows.append([])
        rows.append([
            "Note",
            "This export was generated with --override. One or more sanity checks "
            "did not pass. Review the checks above before delivering.",
        ])
    return rows


def _statement_to_rows(result: StatementResult) -> list[list[str]]:
    """Flatten a StatementResult into CSV-compatible rows."""
    rows: list[list[str]] = []

    if result.kind == "trial_balance":
        # TB has debit/credit split in meta
        rows.append(["Account", "Debit", "Credit"])
        raw_rows = result.meta.get("raw_rows", [])
        for r in raw_rows:
            debit = str(r["debit"]) if r["debit"] > 0 else ""
            credit = str(r["credit"]) if r["credit"] > 0 else ""
            rows.append([r["label"], debit, credit])
        # Totals
        rows.append([])
        rows.append([
            "Totals",
            str(result.totals.get("total_debits", "0.00")),
            str(result.totals.get("total_credits", "0.00")),
        ])
        return rows

    if result.kind in ("pnl", "balance_sheet"):
        rows.append(["Category", "Account", "Amount"])
        for section in result.sections:
            rows.append([section.get("label", ""), "", ""])
            for row in section.get("rows", []):
                amt = row.get("amount")
                rows.append(["", row.get("label", ""), str(amt) if amt is not None else ""])
            total = section.get("total")
            if total is not None:
                rows.append(["", section.get("total_label", "Total"), str(total)])
            rows.append([])
        # Overall totals
        if result.totals:
            rows.append(["Summary totals", "", ""])
            for k, v in result.totals.items():
                rows.append(["", k.replace("_", " ").title(), str(v)])
        return rows

    if result.kind == "general_ledger":
        rows.append([
            "Account Type",
            "Account",
            "Account Detail",
            "Date",
            "Counterparty",
            "Memo",
            "Source ID",
            "Entry Type",
            "Amount",
        ])
        for section in result.sections:
            account_label = section.get("label", "")
            for row in section.get("rows", []):
                label = row.get("label", "")
                # label is "date  |  payee  |  narration" — split it
                parts = [p.strip() for p in label.split("  |  ")]
                entry_date = parts[0] if parts else ""
                if len(parts) >= 3:
                    payee = parts[1]
                    memo = "  |  ".join(parts[2:])
                elif len(parts) == 2:
                    payee = ""
                    memo = parts[1]
                else:
                    payee = ""
                    memo = label
                amt = row.get("amount")
                rows.append([
                    _account_type(account_label.replace(" › ", ":")),
                    account_label,
                    _account_detail(account_label.replace(" › ", ":")),
                    entry_date,
                    payee,
                    memo,
                    "",
                    "Posted",
                    str(amt) if amt is not None else "",
                ])
            total = section.get("total")
            if total is not None:
                rows.append(["", account_label, "", "", "", "Account Total", "", "Account Total", str(total)])
            rows.append([])
        return rows

    # Generic fallback
    rows.append(["Section", "Account", "Amount"])
    for section in result.sections:
        rows.append([section.get("label", ""), "", ""])
        for row in section.get("rows", []):
            amt = row.get("amount")
            rows.append(["", row.get("label", ""), str(amt) if amt is not None else ""])
    return rows


def _entry_source_id(entry: Any) -> str:
    for key, value in getattr(entry, "meta", ()):
        if key == "source-id":
            return value
    return ""


def _entry_type(entry: Any) -> str:
    meta = dict(getattr(entry, "meta", ()))
    if meta.get("reverses"):
        return "Reversal"
    if meta.get("correction-of"):
        return "Correction"
    if str(_entry_source_id(entry)).startswith("open"):
        return "Opening"
    return "Posted"


def _parse_entries_for_period(entity_path: Path, from_date: date, to_date: date) -> list[Any]:
    entries = _parse_all_entries(entity_path)
    return [
        entry for entry in entries
        if from_date <= entry.date <= to_date
    ]


def _parse_all_entries(entity_path: Path) -> list[Any]:
    books_path = entity_path / "books.beancount"
    if not books_path.exists():
        return []
    parsed = parse_ledger(books_path.read_text(encoding="utf-8"))
    return list(parsed.get("entries", []))


def _build_general_ledger_rows(
    entity_path: Path,
    from_date: date,
    to_date: date,
    entries: Optional[list[Any]] = None,
) -> list[list[str]]:
    """Build accountant-reviewable GL rows with source metadata."""
    rows: list[list[str]] = [
        [
            "Account Type",
            "Account",
            "Account Detail",
            "Date",
            "Counterparty",
            "Memo",
            "Source ID",
            "Entry Type",
            "Amount",
        ],
    ]
    rows_by_account: dict[str, list[list[str]]] = {}

    try:
        period_entries = entries if entries is not None else _parse_entries_for_period(entity_path, from_date, to_date)
    except Exception:
        return _statement_to_rows(general_ledger(entity_path, from_date, to_date))

    for entry in period_entries:
        source_id = _entry_source_id(entry)
        entry_type = _entry_type(entry)
        payee = entry.payee or ""
        memo = entry.narration or ""
        for posting in entry.postings:
            rendered_account = _render_account_name(posting.account)
            rows_by_account.setdefault(rendered_account, []).append([
                _account_type(posting.account),
                rendered_account,
                _account_detail(posting.account),
                str(entry.date),
                payee,
                memo,
                source_id,
                entry_type,
                _amount_text(posting.amount),
            ])

    for account in sorted(rows_by_account):
        acct_rows = rows_by_account[account]
        rows.extend(acct_rows)
        total = sum((Decimal(r[8]) for r in acct_rows if r[8]), Decimal("0.00"))
        rows.append(["", account, "", "", "", "Account Total", "", "Account Total", str(total)])
        rows.append([])

    return rows


def _reconciliation_source(record: dict[str, Any]) -> str:
    return str(
        record.get("source_file")
        or record.get("source_path")
        or record.get("statement_file")
        or record.get("import_source")
        or ""
    )


def _reconciliation_review_status(status: str, difference: Any) -> str:
    normalized = str(status or "").strip().lower()
    try:
        diff = abs(Decimal(str(difference or "0")))
    except Exception:
        diff = Decimal("0.00")
    if normalized in {"clean", "pass"} and diff < Decimal("0.01"):
        return "PASS"
    if normalized == "resolved":
        return "RESOLVED"
    return "REVIEW"


def _build_reconciliation_rows(entity_path: Path) -> list[list[str]]:
    """Build reconciliation sheet rows from all discrepancy records."""
    records = list_discrepancies(entity_path)
    rows: list[list[str]] = [
        [
            "Account",
            "As Of",
            "Ledger Balance",
            "Source Balance",
            "Difference",
            "Reconciliation Status",
            "Review Status",
            "Source",
            "Notes",
        ],
    ]
    for r in records:
        status = str(r.get("status", ""))
        rows.append([
            r.get("account", ""),
            r.get("as_of", ""),
            r.get("ledger_balance", ""),
            r.get("source_balance", ""),
            r.get("discrepancy", ""),
            status,
            _reconciliation_review_status(status, r.get("discrepancy", "")),
            _reconciliation_source(r),
            "; ".join(r.get("causes", [])),
        ])
    if not records:
        rows.append(["(no reconciliation records)", "", "", "", "", "not provided", "REVIEW", "", ""])
    return rows


def _build_vendor_1099_rows(
    entity_path: Path,
    from_date: date,
    to_date: date,
    threshold: Decimal,
    entries: Optional[list[Any]] = None,
) -> list[list[str]]:
    """Build vendor 1099 sheet rows from GL expense postings."""
    try:
        period_entries = entries if entries is not None else _parse_entries_for_period(entity_path, from_date, to_date)
    except Exception:
        return [
            ["Vendor", "Net Paid", "1099 Candidate", "Gross Paid", "Adjustments", "Review Notes"],
            ["(could not compute general ledger)", "", ""],
        ]

    vendor_totals: dict[str, dict[str, Any]] = {}
    entries_by_source_id = {
        source_id: entry
        for entry in period_entries
        if (source_id := _entry_source_id(entry))
    }

    def _vendor_bucket(vendor: str) -> dict[str, Any]:
        return vendor_totals.setdefault(
            vendor,
            {"gross": Decimal("0.00"), "adjustments": Decimal("0.00"), "accounts": set()},
        )

    def _add_expense_accounts(bucket: dict[str, Any], entry: Any) -> None:
        accounts = bucket["accounts"]
        if isinstance(accounts, set):
            for posting in entry.postings:
                if posting.account.startswith("Expenses"):
                    accounts.add(_render_account_name(posting.account))

    for entry in period_entries:
        entry_type = _entry_type(entry)
        if entry_type == "Reversal":
            original_id = dict(getattr(entry, "meta", ())).get("reverses", "")
            original = entries_by_source_id.get(original_id)
            vendor = (getattr(original, "payee", None) or "").strip()
            if vendor:
                expense_amount = sum(
                    (posting.amount for posting in entry.postings if posting.account.startswith("Expenses")),
                    Decimal("0.00"),
                )
                bucket = _vendor_bucket(vendor)
                bucket["adjustments"] = bucket["adjustments"] + expense_amount
                _add_expense_accounts(bucket, entry)
            continue
        vendor = (entry.payee or "").strip()
        if not vendor:
            continue

        expense_amount = sum(
            (posting.amount for posting in entry.postings if posting.account.startswith("Expenses")),
            Decimal("0.00"),
        )
        if expense_amount == Decimal("0.00"):
            continue

        bucket = _vendor_bucket(vendor)
        if entry_type == "Correction":
            bucket["adjustments"] = bucket["adjustments"] + expense_amount
        else:
            bucket["gross"] = bucket["gross"] + expense_amount
        _add_expense_accounts(bucket, entry)

    rows: list[list[str]] = [
        ["Vendor", "Net Paid", "1099 Candidate (if >= " + str(threshold) + ")", "Gross Paid", "Adjustments", "Review Notes"],
    ]
    if not vendor_totals:
        rows.append(["(no vendor payments in expense accounts for this period)", "", "", "", "", ""])
        return rows

    def _net_paid(item: tuple[str, dict[str, Any]]) -> Decimal:
        values = item[1]
        return values["gross"] + values["adjustments"]

    for vendor, values in sorted(vendor_totals.items(), key=_net_paid, reverse=True):
        gross = values["gross"]
        adjustments = values["adjustments"]
        net = gross + adjustments
        accounts = values.get("accounts")
        account_note = ""
        if isinstance(accounts, set) and accounts:
            account_note = "Accounts: " + "; ".join(sorted(accounts))
        candidate = "Yes" if net >= threshold else "No"
        rows.append([vendor, str(net), candidate, str(gross), str(adjustments), account_note])

    rows.append([])
    rows.append([f"Note: 1099-candidate flag is informational only. Forms are NOT generated.", "", "", "", "", ""])
    return rows


def _entry_review_amount(entry: Any) -> Decimal:
    for prefix in ("Expenses", "Income"):
        amount = sum(
            (posting.amount for posting in entry.postings if posting.account.startswith(prefix)),
            Decimal("0.00"),
        )
        if amount != Decimal("0.00"):
            return amount
    return sum((posting.amount for posting in entry.postings), Decimal("0.00"))


def _entry_primary_category(entry: Any) -> str:
    for posting in entry.postings:
        if posting.account.startswith(("Expenses", "Income")):
            return _render_account_name(posting.account)
    return ""


def _build_adjustment_log_rows(entity_path: Path, entries: Optional[list[Any]] = None) -> list[list[str]]:
    """Build adjustment log sheet rows by reconstructing reversal pairs.

    Uses reverses:/correction-of: metadata from parse_ledger to find pairs.
    Each pair is emitted exactly once (keyed on original entry source-id).
    """
    books_path = entity_path / "books.beancount"
    rows: list[list[str]] = [
        [
            "Original ID",
            "Original Date",
            "Original Counterparty",
            "Original Amount",
            "Reversal ID",
            "Reversal Date",
            "Corrected ID",
            "Corrected Category",
            "Corrected Amount",
            "Trace Status",
            "Note",
        ],
    ]

    if not books_path.exists():
        rows.append(["(ledger not found)", "", "", "", "", "", "", "", "", "REVIEW", ""])
        return rows

    if entries is None:
        try:
            entries = _parse_all_entries(entity_path)
        except Exception as exc:
            rows.append([f"(could not parse ledger: {exc})", "", "", "", "", "", "", "", "", "REVIEW", ""])
            return rows

    # Build lookup: source-id -> entry
    by_source_id: dict[str, Any] = {}
    for entry in entries:
        for key, val in entry.meta:
            if key == "source-id":
                by_source_id[val] = entry
                break

    # Find reversal entries (have "reverses:" metadata)
    # and corrected entries (have "correction-of:" metadata)
    # Each reversal pair: original + reversing entry + (optional) corrected entry
    # We emit one row per original ID, deduplicating by original ID.
    seen_originals: set[str] = set()
    pair_rows: list[list[str]] = []

    for entry in entries:
        meta_dict: dict[str, str] = dict(entry.meta)
        reverses = meta_dict.get("reverses", "")
        correction_of = meta_dict.get("correction-of", "")

        if reverses:
            # This is a reversing entry; reverses = original source-id
            original_id = reverses
            if original_id in seen_originals:
                continue
            seen_originals.add(original_id)

            original = by_source_id.get(original_id)
            corrected = None
            for e2 in entries:
                m2 = dict(e2.meta)
                if m2.get("correction-of") == original_id:
                    corrected = e2
                    break

            reversal_date = str(entry.date)
            reversal_id = _entry_source_id(entry)
            corrected_id = _entry_source_id(corrected) if corrected is not None else ""
            corrected_category = _entry_primary_category(corrected) if corrected is not None else ""
            corrected_amount = _entry_review_amount(corrected) if corrected is not None else Decimal("0.00")
            trace_status = "COMPLETE" if original is not None and corrected is not None else "REVIEW"

            pair_rows.append([
                original_id,
                str(original.date) if original is not None else "",
                original.payee if original is not None and original.payee else "",
                str(_entry_review_amount(original)) if original is not None else "",
                reversal_id,
                reversal_date,
                corrected_id,
                corrected_category,
                str(corrected_amount) if corrected is not None else "",
                trace_status,
                corrected.narration if corrected is not None else entry.narration,
            ])

        elif correction_of:
            # This is a corrected entry; make sure we haven't already emitted via the reversing entry
            original_id = correction_of
            if original_id in seen_originals:
                continue
            seen_originals.add(original_id)

            original = by_source_id.get(original_id)
            reversal_date = ""
            reversal_id = ""
            # Find the reversing entry
            for e3 in entries:
                m3 = dict(e3.meta)
                if m3.get("reverses") == original_id:
                    reversal_date = str(e3.date)
                    reversal_id = _entry_source_id(e3)
                    break

            trace_status = "COMPLETE" if original is not None and reversal_id else "REVIEW"

            pair_rows.append([
                original_id,
                str(original.date) if original is not None else "",
                original.payee if original is not None and original.payee else "",
                str(_entry_review_amount(original)) if original is not None else "",
                reversal_id,
                reversal_date,
                _entry_source_id(entry),
                _entry_primary_category(entry),
                str(_entry_review_amount(entry)),
                trace_status,
                entry.narration,
            ])

    if pair_rows:
        rows.extend(pair_rows)
    else:
        rows.append(["(no reversals or corrections found in the ledger)", "", "", "", "", "", "", "", "", "PASS", ""])

    return rows


def _build_open_questions_rows(entity_path: Path) -> list[list[str]]:
    """Build owner/accountant questions from reports/open-questions.json when present."""
    oq_path = entity_path / "reports" / "open-questions.json"
    rows: list[list[str]] = [
        [
            "#",
            "Area",
            "Related Sheet",
            "Related Account",
            "Source ID",
            "Amount",
            "Question / Note",
            "Owner Response",
            "Status",
        ],
    ]
    if not oq_path.exists():
        rows.append(["", "General", "", "", "", "", "(no open questions file found)", "", "closed"])
        return rows
    try:
        data = json.loads(oq_path.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not data:
            rows.append(["", "General", "", "", "", "", "(open questions file is empty)", "", "closed"])
            return rows
        for i, item in enumerate(data, 1):
            if isinstance(item, dict):
                rows.append([
                    str(i),
                    str(item.get("area", "General")),
                    str(item.get("related_sheet", "")),
                    str(item.get("account", item.get("related_account", ""))),
                    str(item.get("source_id", "")),
                    str(item.get("amount", "")),
                    str(item.get("question", "")),
                    str(item.get("owner_response", "")),
                    str(item.get("status", "")),
                ])
            else:
                rows.append([str(i), "General", "", "", "", "", str(item), "", "open"])
    except (json.JSONDecodeError, OSError) as exc:
        rows.append(["", "General", "", "", "", "", f"(could not read open questions: {exc})", "", "open"])
    return rows


def _find_row(rows: list[list[str]], *, col: int, value: str) -> int:
    for idx, row in enumerate(rows, start=1):
        if len(row) > col and row[col] == value:
            return idx
    return 0


def _find_amount(rows: list[list[str]], *, label_col: int, amount_col: int, label: str) -> Decimal:
    for row in rows:
        if len(row) > max(label_col, amount_col) and row[label_col] == label:
            try:
                return Decimal(str(row[amount_col]))
            except Exception:
                return Decimal("0.00")
    return Decimal("0.00")


def _last_data_row(rows: list[list[str]]) -> int:
    last = 1
    for idx, row in enumerate(rows, start=1):
        if any(str(cell).strip() for cell in row):
            last = idx
    return last


def _build_summary_rows(
    entity_name: str,
    entity_config: dict[str, Any],
    from_date: date,
    to_date: date,
    sanity: SanityResult,
    override_used: bool,
    pnl_rows: list[list[str]],
    bs_rows: list[list[str]],
    opening_cumulative_net_income: Decimal,
    gl_rows: Optional[list[list[str]]] = None,
    recon_rows: Optional[list[list[str]]] = None,
    vendor_rows: Optional[list[list[str]]] = None,
    open_question_rows: Optional[list[list[str]]] = None,
    formulas: bool = True,
) -> list[list[str]]:
    """Build accountant summary rows.

    XLSX uses formulas for reviewability; CSV uses static values for portability.
    """
    status = "PASS"
    if sanity.has_failures:
        status = "OVERRIDE" if override_used else "FAIL"
    elif sanity.has_warnings:
        status = "WARN"

    pnl_net_row = _find_row(pnl_rows, col=1, value="Net Income")
    net_income = _find_amount(pnl_rows, label_col=1, amount_col=2, label="Net Income")
    ending_cash = sum(
        (
            Decimal(row[2])
            for row in bs_rows
            if len(row) > 2
            and row[1].startswith(("Assets › Bank", "Assets › Cash"))
            and row[2]
        ),
        Decimal("0.00"),
    )
    gl_posting_count = sum(
        1
        for row in gl_rows or []
        if len(row) > 8 and row[8] and row[7] not in {"Account Total", "Entry Type"}
    )
    open_question_count = sum(
        1
        for row in open_question_rows or []
        if len(row) > 8 and row[8].strip().lower() == "open"
    )
    recon_review_count = sum(
        1
        for row in recon_rows or []
        if len(row) > 6 and row[6] == "REVIEW"
    )
    vendor_candidate_count = sum(
        1
        for row in vendor_rows or []
        if len(row) > 2 and row[2] == "Yes"
    )
    rows: list[list[str]] = [
        ["Metric", "Value"],
        ["Business", entity_name],
        ["Produced by", "/books"],
        ["Project link", "https://github.com/giltotherescue/slashbooks"],
        ["Accounting basis", "Cash basis"],
        ["Bookkeeping scope", "Simple cash-basis bookkeeping export; not a tax return or assurance report"],
        ["Tax prep scope", "Handoff support only; tax treatment and filing positions remain preparer-reviewed"],
        ["Legal structure", str(entity_config.get("legal_structure", ""))],
        ["Country", str(entity_config.get("country", ""))],
        ["Tax jurisdiction", str(entity_config.get("tax_jurisdiction", ""))],
        ["Operating currency", str(entity_config.get("operating_currency", ""))],
        ["Period start", str(from_date)],
        ["Period end", str(to_date)],
        ["Package status", status],
        ["Warnings", str(sum(1 for c in sanity.checks if c.status == "warn"))],
        ["Failures", str(sum(1 for c in sanity.checks if c.status == "fail"))],
        ["Net income", f"='P&L'!C{pnl_net_row}" if formulas and pnl_net_row else str(net_income)],
        [
            "Ending cash",
            (
                '=SUMIF(\'Balance Sheet\'!B:B,"Assets › Bank*",\'Balance Sheet\'!C:C)+'
                'SUMIF(\'Balance Sheet\'!B:B,"Assets › Cash*",\'Balance Sheet\'!C:C)'
                if formulas else str(ending_cash)
            ),
        ],
        ["Opening cumulative net income", str(opening_cumulative_net_income)],
        [
            "GL posting rows",
            '=COUNTIFS(\'General Ledger\'!I:I,"<>",\'General Ledger\'!H:H,"<>Account Total",\'General Ledger\'!H:H,"<>Entry Type")'
            if formulas else str(gl_posting_count),
        ],
        [
            "Open questions",
            '=COUNTIF(\'Open Questions\'!I:I,"open")'
            if formulas else str(open_question_count),
        ],
        [
            "Reconciliation items needing review",
            '=COUNTIF(Reconciliations!G:G,"REVIEW")'
            if formulas else str(recon_review_count),
        ],
        [
            "1099 candidates",
            '=COUNTIF(\'Vendor 1099\'!C:C,"Yes")'
            if formulas else str(vendor_candidate_count),
        ],
    ]
    return rows


def _build_checks_rows(
    pnl_rows: list[list[str]],
    bs_rows: list[list[str]],
    tb_rows: list[list[str]],
    summary_rows: list[list[str]],
    formulas: bool = True,
) -> list[list[str]]:
    """Build export tie-out checks.

    XLSX uses formulas; CSV uses static plain-value results.
    """
    tb_total_row = _find_row(tb_rows, col=0, value="Totals")
    bs_assets_row = _find_row(bs_rows, col=1, value="Total Assets")
    bs_le_row = _find_row(bs_rows, col=1, value="Total Liabilities And Equity")
    bs_net_row = _find_row(bs_rows, col=1, value="Net Income (current period)")
    pnl_net_row = _find_row(pnl_rows, col=1, value="Net Income")
    opening_net_row = _find_row(summary_rows, col=0, value="Opening cumulative net income")

    if not formulas:
        tb_diff = _find_amount(tb_rows, label_col=0, amount_col=1, label="Totals") - _find_amount(
            tb_rows,
            label_col=0,
            amount_col=2,
            label="Totals",
        )
        bs_diff = _find_amount(bs_rows, label_col=1, amount_col=2, label="Total Assets") - _find_amount(
            bs_rows,
            label_col=1,
            amount_col=2,
            label="Total Liabilities And Equity",
        )
        pnl_bs_diff = _find_amount(pnl_rows, label_col=1, amount_col=2, label="Net Income") - (
            _find_amount(bs_rows, label_col=1, amount_col=2, label="Net Income (current period)")
            - _find_amount(summary_rows, label_col=0, amount_col=1, label="Opening cumulative net income")
        )
        open_questions = _find_amount(summary_rows, label_col=0, amount_col=1, label="Open questions")
        open_reconciliations = _find_amount(
            summary_rows,
            label_col=0,
            amount_col=1,
            label="Reconciliation items needing review",
        )
        candidates_1099 = _find_amount(summary_rows, label_col=0, amount_col=1, label="1099 candidates")

        def _status_for_zero(value: Decimal, review_label: str = "FAIL") -> str:
            return "PASS" if round(value, 2) == Decimal("0.00") else review_label

        return [
            ["Check", "Tie-out", "Status"],
            ["Trial balance debits equal credits", str(tb_diff), _status_for_zero(tb_diff)],
            ["Balance sheet balances", str(bs_diff), _status_for_zero(bs_diff)],
            [
                "P&L net income agrees to balance sheet net-income movement",
                str(pnl_bs_diff),
                _status_for_zero(pnl_bs_diff),
            ],
            ["Open question count", str(open_questions), "PASS" if open_questions == 0 else "REVIEW"],
            [
                "Open reconciliation item count",
                str(open_reconciliations),
                "PASS" if open_reconciliations == 0 else "REVIEW",
            ],
            ["1099 candidate count", str(candidates_1099), "PASS" if candidates_1099 == 0 else "REVIEW"],
        ]

    return [
        ["Check", "Tie-out", "Status"],
        [
            "Trial balance debits equal credits",
            f"='Trial Balance'!B{tb_total_row}-'Trial Balance'!C{tb_total_row}" if tb_total_row else "",
            f'=IF(ROUND(B2,2)=0,"PASS","FAIL")',
        ],
        [
            "Balance sheet balances",
            f"='Balance Sheet'!C{bs_assets_row}-'Balance Sheet'!C{bs_le_row}" if bs_assets_row and bs_le_row else "",
            f'=IF(ROUND(B3,2)=0,"PASS","FAIL")',
        ],
        [
            "P&L net income agrees to balance sheet net-income movement",
            (
                f"='P&L'!C{pnl_net_row}-('Balance Sheet'!C{bs_net_row}-Summary!B{opening_net_row})"
                if pnl_net_row and bs_net_row and opening_net_row else ""
            ),
            f'=IF(ROUND(B4,2)=0,"PASS","FAIL")',
        ],
        [
            "Open question count",
            '=COUNTIF(\'Open Questions\'!I:I,"open")',
            '=IF(B5=0,"PASS","REVIEW")',
        ],
        [
            "Open reconciliation item count",
            '=COUNTIF(Reconciliations!G:G,"REVIEW")',
            '=IF(B6=0,"PASS","REVIEW")',
        ],
        [
            "1099 candidate count",
            '=COUNTIF(\'Vendor 1099\'!C:C,"Yes")',
            '=IF(B7=0,"PASS","REVIEW")',
        ],
    ]


def _build_source_index_rows(entity_path: Path) -> list[list[str]]:
    """Build XLSX-only source/reconciliation index rows."""
    rows: list[list[str]] = [
        [
            "Account",
            "As Of",
            "Ledger Balance",
            "Source Balance",
            "Difference",
            "Reconciliation Status",
            "Review Status",
            "Source",
            "Notes",
        ],
    ]
    records = list_discrepancies(entity_path)
    for r in records:
        status = str(r.get("status", ""))
        rows.append([
            r.get("account", ""),
            r.get("as_of", ""),
            r.get("ledger_balance", ""),
            r.get("source_balance", ""),
            r.get("discrepancy", ""),
            status,
            _reconciliation_review_status(status, r.get("discrepancy", "")),
            _reconciliation_source(r),
            "; ".join(r.get("causes", [])),
        ])
    if not records:
        rows.append(["(no reconciliation source records)", "", "", "", "", "not provided", "REVIEW", "", ""])
    return rows


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    """Write *rows* to a CSV file at *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(row)


# ---------------------------------------------------------------------------
# XLSX writer (guarded by find_spec)
# ---------------------------------------------------------------------------


def _xlsxwriter_available() -> bool:
    return importlib.util.find_spec("xlsxwriter") is not None


def _write_xlsx(
    xlsx_path: Path,
    sheet_data: list[XlsxSheet] | list[tuple[str, list[list[str]]]],
    xlsx_only_sheet_data: Optional[list[XlsxSheet]] = None,
) -> None:
    """Write all sheets to an XLSX workbook.

    Numbers are written as Decimal via write_number. Formula strings are
    written as formulas. Headers are bold with frozen panes.
    """
    import xlsxwriter  # type: ignore

    xlsx_path.parent.mkdir(parents=True, exist_ok=True)

    wb = xlsxwriter.Workbook(str(xlsx_path), {"strings_to_urls": False})

    fmt_header = wb.add_format({
        "bold": True,
        "bg_color": _HEADER_BG_COLOR,
        "border": 1,
    })
    fmt_number = wb.add_format({"num_format": _ACCT_NUM_FMT})
    fmt_total = wb.add_format({"bold": True, "top": 1, "num_format": _ACCT_NUM_FMT})
    fmt_pass = wb.add_format({"bold": True, "font_color": "#008000"})
    fmt_warn = wb.add_format({"bold": True, "font_color": "#9C6500"})
    fmt_fail = wb.add_format({"bold": True, "font_color": "#C00000"})

    all_sheet_data: list[XlsxSheet] = [
        item if isinstance(item, XlsxSheet) else XlsxSheet(name=item[0], rows=item[1])
        for item in sheet_data
    ]
    if xlsx_only_sheet_data:
        all_sheet_data.extend(xlsx_only_sheet_data)

    def _col_width(rows: list[list[str]], col_idx: int, default: int = 12) -> int:
        max_w = default
        for row in rows[:50]:  # sample first 50 rows
            if col_idx < len(row):
                max_w = max(max_w, len(str(row[col_idx])) + 2)
        return min(max_w, 60)

    def _is_numeric(val: str) -> Optional[Decimal]:
        """Return Decimal if *val* is a numeric string, else None."""
        if not val:
            return None
        try:
            return Decimal(val.replace(",", ""))
        except Exception:
            return None

    def _table_last_index(rows: list[list[str]]) -> int:
        last = len(rows) - 1
        for idx, row in enumerate(rows[1:], start=1):
            if not any(str(cell).strip() for cell in row):
                return max(1, idx - 1)
        return last

    for spec in all_sheet_data:
        sheet_name = spec.name
        rows = spec.rows
        ws = wb.add_worksheet(sheet_name[:31])  # Excel sheet name max 31 chars
        if spec.tab_color:
            ws.set_tab_color(spec.tab_color)

        if not rows:
            wb_done = ws  # noqa: assigned but unused in constant_memory mode
            continue

        header_row = rows[0]
        # Set column widths based on header + sample data
        max_cols = max((len(row) for row in rows), default=len(header_row))
        for col_idx in range(max_cols):
            width = _col_width(rows, col_idx)
            ws.set_column(col_idx, col_idx, width)

        # Write header row with format
        for col_idx, cell in enumerate(header_row):
            ws.write(0, col_idx, str(cell), fmt_header)

        # Freeze header row
        ws.freeze_panes(1, 0)

        # Write data rows
        for row_idx, row in enumerate(rows[1:], start=1):
            for col_idx, cell in enumerate(row):
                cell_str = str(cell) if cell is not None else ""
                if col_idx in spec.formula_columns and cell_str.startswith("="):
                    ws.write_formula(row_idx, col_idx, cell_str, fmt_number if col_idx in spec.numeric_columns else None)
                    continue

                num_val = _is_numeric(cell_str) if col_idx in spec.numeric_columns else None
                if num_val is not None:
                    row_label = row[spec.total_label_column] if spec.total_label_column is not None and len(row) > spec.total_label_column else ""
                    fmt = fmt_total if row_label == spec.total_label else fmt_number
                    ws.write_number(row_idx, col_idx, num_val, fmt)
                else:
                    if cell_str in {"PASS"} or cell_str.startswith("[PASS]"):
                        ws.write(row_idx, col_idx, cell_str, fmt_pass)
                    elif cell_str in {"WARN", "REVIEW"} or cell_str.startswith("[WARN]"):
                        ws.write(row_idx, col_idx, cell_str, fmt_warn)
                    elif cell_str in {"FAIL", "OVERRIDE"} or cell_str.startswith("[FAIL]"):
                        ws.write(row_idx, col_idx, cell_str, fmt_fail)
                    elif cell_str.startswith(("https://", "http://")):
                        ws.write_url(row_idx, col_idx, cell_str, string=cell_str)
                    else:
                        ws.write(row_idx, col_idx, cell_str)

        table_added = False
        if spec.table and len(rows) > 1 and max_cols > 0:
            table_last = _table_last_index(rows)
            if table_last >= 1:
                columns = [{"header": str(header_row[i]) if i < len(header_row) else f"Column {i + 1}"} for i in range(max_cols)]
                ws.add_table(0, 0, table_last, max_cols - 1, {
                    "columns": columns,
                    "style": "Table Style Medium 2",
                })
                table_added = True

        last_row = _last_data_row(rows) - 1
        if not table_added and last_row >= 1 and max_cols > 0:
            ws.autofilter(0, 0, last_row, max_cols - 1)

        ws.set_landscape()
        ws.repeat_rows(0)
        ws.fit_to_pages(1, 0)

    wb.close()


# ---------------------------------------------------------------------------
# Main generation function
# ---------------------------------------------------------------------------


def generate_accountant_package(
    entity_path: Path | str,
    from_date: date,
    to_date: date,
    output_dir: Optional[Path | str] = None,
    override: bool = False,
) -> PackageResult:
    """Generate the full accountant export (CSV exports + optional XLSX).

    Returns a :class:`PackageResult`.  When sanity checks have failures and
    ``override=False``, returns early with an error message.

    The ``override`` flag is recorded in the cover sheet when used.
    """
    entity_path = Path(entity_path)

    # --- Load entity config ---
    entity_json_path = entity_path / "entity.json"
    entity_name = "Unknown Entity"
    threshold_1099 = _DEFAULT_1099_THRESHOLD
    if entity_json_path.exists():
        try:
            ecfg = json.loads(entity_json_path.read_text(encoding="utf-8"))
            entity_name = str(ecfg.get("name", entity_name))
            raw_thresh = ecfg.get("vendor_1099_threshold")
            if raw_thresh is not None:
                threshold_1099 = Decimal(str(raw_thresh))
        except Exception:
            pass

    # --- Output directory ---
    period_str = f"{from_date}_{to_date}"
    if output_dir is None:
        output_dir = entity_path / "reports" / "accountant-export" / period_str
    output_dir = Path(output_dir)
    csv_dir = output_dir / "csv"

    # --- Sanity checks ---
    sanity = run_sanity_checks(entity_path, from_date, to_date)
    if sanity.has_failures and not override:
        return PackageResult(
            output_dir=output_dir,
            sanity=sanity,
            error=(
                "Sanity checks have failures. Fix them before generating the export, "
                "or run with --override to proceed anyway.\n"
                + "\n".join(
                    f"  [{c.status.upper()}] {c.check}: {c.detail}"
                    for c in sanity.checks
                    if c.status == "fail"
                )
            ),
        )

    override_used = sanity.has_failures and override

    # --- Build all sheet data ---
    try:
        pnl_result = profit_and_loss(entity_path, from_date, to_date)
        bs_result = balance_sheet(entity_path, to_date)
        tb_result = trial_balance(entity_path, to_date)
        prior_bs_result = balance_sheet(entity_path, from_date - timedelta(days=1))
    except Exception as exc:
        return PackageResult(
            output_dir=output_dir,
            sanity=sanity,
            error=f"Could not compute financial statements: {exc}",
        )
    try:
        all_entries = _parse_all_entries(entity_path)
        period_entries = [
            entry for entry in all_entries
            if from_date <= entry.date <= to_date
        ]
    except Exception:
        all_entries = None
        period_entries = None

    cover_rows = _build_cover_rows(entity_path, entity_name, from_date, to_date, sanity, override_used)
    pnl_rows = _statement_to_rows(pnl_result)
    bs_rows = _statement_to_rows(bs_result)
    prior_bs_rows = _statement_to_rows(prior_bs_result)
    tb_rows = _statement_to_rows(tb_result)
    gl_rows = _build_general_ledger_rows(entity_path, from_date, to_date, entries=period_entries)
    recon_rows = _build_reconciliation_rows(entity_path)
    vendor_rows = _build_vendor_1099_rows(entity_path, from_date, to_date, threshold_1099, entries=period_entries)
    adj_rows = _build_adjustment_log_rows(entity_path, entries=all_entries)
    open_question_rows = _build_open_questions_rows(entity_path)
    opening_cumulative_net_income = _find_amount(
        prior_bs_rows,
        label_col=1,
        amount_col=2,
        label="Net Income (current period)",
    )
    summary_rows = _build_summary_rows(
        entity_name,
        _load_entity_config(entity_path),
        from_date,
        to_date,
        sanity,
        override_used,
        pnl_rows,
        bs_rows,
        opening_cumulative_net_income,
        gl_rows=gl_rows,
        recon_rows=recon_rows,
        vendor_rows=vendor_rows,
        open_question_rows=open_question_rows,
    )
    csv_summary_rows = _build_summary_rows(
        entity_name,
        _load_entity_config(entity_path),
        from_date,
        to_date,
        sanity,
        override_used,
        pnl_rows,
        bs_rows,
        opening_cumulative_net_income,
        gl_rows=gl_rows,
        recon_rows=recon_rows,
        vendor_rows=vendor_rows,
        open_question_rows=open_question_rows,
        formulas=False,
    )
    checks_rows = _build_checks_rows(pnl_rows, bs_rows, tb_rows, summary_rows)
    csv_checks_rows = _build_checks_rows(pnl_rows, bs_rows, tb_rows, csv_summary_rows, formulas=False)
    source_index_rows = _build_source_index_rows(entity_path)

    sheet_data: list[tuple[str, list[list[str]]]] = [
        ("Cover", cover_rows),
        ("P&L", pnl_rows),
        ("Balance Sheet", bs_rows),
        ("Trial Balance", tb_rows),
        ("General Ledger", gl_rows),
        ("Reconciliations", recon_rows),
        ("Vendor 1099", vendor_rows),
        ("Adjustment Log", adj_rows),
        ("Open Questions", open_question_rows),
        ("Summary", csv_summary_rows),
        ("Checks", csv_checks_rows),
        ("Source Index", source_index_rows),
    ]
    xlsx_sheet_data: list[XlsxSheet] = [
        XlsxSheet("Cover", cover_rows, tab_color="#5B9BD5"),
        XlsxSheet("P&L", pnl_rows, tab_color="#A9D18E", numeric_columns=frozenset({2})),
        XlsxSheet("Balance Sheet", bs_rows, tab_color="#A9D18E", numeric_columns=frozenset({2})),
        XlsxSheet("Trial Balance", tb_rows, tab_color="#A9D18E", numeric_columns=frozenset({1, 2})),
        XlsxSheet(
            "General Ledger",
            gl_rows,
            tab_color="#9DC3E6",
            numeric_columns=frozenset({8}),
            total_label_column=5,
        ),
        XlsxSheet("Reconciliations", recon_rows, table=True, numeric_columns=frozenset({2, 3, 4})),
        XlsxSheet("Vendor 1099", vendor_rows, table=True, tab_color="#F4B183", numeric_columns=frozenset({1, 3, 4})),
        XlsxSheet("Adjustment Log", adj_rows, table=True, numeric_columns=frozenset({3, 8})),
        XlsxSheet("Open Questions", open_question_rows, table=True, numeric_columns=frozenset({5})),
    ]
    xlsx_only_sheet_data: list[XlsxSheet] = [
        XlsxSheet(
            "Summary",
            summary_rows,
            table=True,
            tab_color="#70AD47",
            formula_columns=frozenset({1}),
            numeric_columns=frozenset({1}),
        ),
        XlsxSheet(
            "Checks",
            checks_rows,
            table=True,
            tab_color="#FFC000",
            formula_columns=frozenset({1, 2}),
            numeric_columns=frozenset({1}),
        ),
        XlsxSheet("Source Index", source_index_rows, table=True, numeric_columns=frozenset({2, 3, 4})),
    ]

    # --- Write CSVs (always) ---
    csv_files: list[Path] = []
    for sheet_name, rows in sheet_data:
        safe_name = sheet_name.replace("&", "-and-").replace(" ", "-").replace("/", "-").replace("--", "-")
        csv_path = csv_dir / f"{safe_name}.csv"
        _write_csv(csv_path, rows)
        csv_files.append(csv_path)

    # --- Write XLSX (when available) ---
    xlsx_available = _xlsxwriter_available()
    xlsx_file: Optional[Path] = None

    if xlsx_available:
        xlsx_path = output_dir / f"accountant-export-{period_str}.xlsx"
        try:
            _write_xlsx(xlsx_path, xlsx_sheet_data, xlsx_only_sheet_data=xlsx_only_sheet_data)
            xlsx_file = xlsx_path
        except Exception as exc:
            # Don't fail the whole run over XLSX; CSV is the canonical output
            print(f"Warning: could not write XLSX: {exc}", file=sys.stderr)
    else:
        print(XLSX_EXTRA_MESSAGE)

    return PackageResult(
        output_dir=output_dir,
        csv_files=csv_files,
        xlsx_file=xlsx_file,
        xlsx_available=xlsx_available,
        sanity=sanity,
        override_used=override_used,
    )


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def add_parser(subparsers: Any) -> None:
    """Register ``sanity-check`` and ``export`` subcommands."""
    # sanity-check
    sc_p = subparsers.add_parser(
        "sanity-check",
        help="Run pre-export sanity checks (returns machine-readable JSON)",
    )
    sc_p.add_argument("--entity", required=True, help="Path to entity directory")
    sc_p.add_argument("--from", dest="from_date", required=True, help="Period start (YYYY-MM-DD)")
    sc_p.add_argument("--to", dest="to_date", required=True, help="Period end (YYYY-MM-DD)")

    # export
    cp_p = subparsers.add_parser(
        "export",
        help="Export accountant-ready workbook and CSV files",
    )
    cp_p.add_argument("--entity", required=True, help="Path to entity directory")
    cp_p.add_argument("--from", dest="from_date", required=True, help="Period start (YYYY-MM-DD)")
    cp_p.add_argument("--to", dest="to_date", required=True, help="Period end (YYYY-MM-DD)")
    cp_p.add_argument(
        "--override",
        action="store_true",
        help="Proceed even when sanity checks have failures (override is recorded)",
    )
    cp_p.add_argument(
        "--output-dir",
        dest="output_dir",
        default=None,
        help="Output directory (default: <entity>/reports/accountant-export/<period>/)",
    )


def run(args: Any) -> int:
    """Dispatch workbook CLI commands."""
    cmd = getattr(args, "command", None)

    if cmd == "sanity-check":
        entity = Path(args.entity)
        from_date = date.fromisoformat(args.from_date)
        to_date = date.fromisoformat(args.to_date)
        result = run_sanity_checks(entity, from_date, to_date)
        print(result.to_json())
        return 0 if not result.has_failures else 1

    if cmd == "export":
        entity = Path(args.entity)
        from_date = date.fromisoformat(args.from_date)
        to_date = date.fromisoformat(args.to_date)
        output_dir = Path(args.output_dir) if args.output_dir else None
        result = generate_accountant_package(
            entity,
            from_date,
            to_date,
            output_dir=output_dir,
            override=args.override,
        )
        if not result.success:
            print(f"Error: {result.error}", file=sys.stderr)
            return 1
        print(f"Accountant export written to: {result.output_dir}")
        print(f"  CSV exports: {len(result.csv_files)} files in {result.output_dir / 'csv'}")
        if result.xlsx_file:
            print(f"  XLSX workbook: {result.xlsx_file}")
        else:
            print(f"  {XLSX_EXTRA_MESSAGE}")
        if result.override_used:
            print("  Note: --override was used; sanity check failures are recorded in the cover sheet.")
        return 0

    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 2
