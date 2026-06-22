from __future__ import annotations

"""QuickBooks export inventory, classification, and parser.

Public API:
    inventory(folder)               -> ReadinessReport
    parse_chart_of_accounts(path)   -> list[QBAccount]
    parse_trial_balance(path)       -> TrialBalance
    parse_balance_sheet(path)       -> BalanceSheet
    parse_profit_and_loss(path)     -> ProfitAndLoss
    parse_general_ledger(path)      -> GeneralLedger
    map_qb_account(name, qb_type)   -> str  (beancount account)
    import_opening(entity, folder, cutover) -> ImportResult

CLI surface (wired in by orchestrator — never by editing cli.py):
    add_parser(subparsers)
    run(args)
"""

import csv
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional
from zipfile import ZipFile
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# Row-type classifier
# ---------------------------------------------------------------------------

class RowType:
    """Constants for classified CSV row types."""
    REPORT_HEADER = "report-header"
    COLUMN_HEADER = "column-header"
    ACCOUNT_HEADER = "account-header"
    OPENING_BALANCE = "opening-balance"
    TRANSACTION = "transaction"
    SUBTOTAL = "subtotal"
    SECTION = "section"
    BLANK = "blank"
    BASIS_FOOTER = "basis-footer"


def classify_row(row: list[str], report_type: Optional[str] = None) -> str:
    """Classify a parsed CSV row into one of the RowType constants.

    Classification is keyed on column-fill patterns, not on content semantics.

    Rules (applied in priority order):
    1. BLANK           — all cells empty (after strip)
    2. BASIS_FOOTER    — col 0 matches 'Cash Basis ...' or 'Accrual Basis ...'
    3. REPORT_HEADER   — col 0 has content, rest empty, looks like company/title/date header
    4. COLUMN_HEADER   — recognized header row patterns
    5. ACCOUNT_HEADER  — col 0 has content, no numeric cols, NOT starting with 'Total'
                         (GL/TxDetail 9-col: first col has name, cols 1-8 empty)
    6. OPENING_BALANCE — col 0 empty, only the last col (balance) has content, no date col
    7. TRANSACTION     — col 0 empty, col 1 has a date (MM/DD/YYYY)
    8. SUBTOTAL        — col 0 starts with 'Total for ' or 'TOTAL'
    9. SECTION         — col 0 has content, no amount in col 1 (BS/PL section headers)
    10. Otherwise      — BLANK
    """
    stripped = [c.strip() for c in row]

    # Extend to at least 9 cols for uniform indexing
    while len(stripped) < 9:
        stripped.append("")

    col0 = stripped[0]
    col1 = stripped[1]

    # 1. Blank
    if all(c == "" for c in stripped):
        return RowType.BLANK

    # 2. Basis footer
    if re.match(r'^(Cash|Accrual) Basis\b', col0, re.IGNORECASE):
        return RowType.BASIS_FOOTER

    # 3/4. Column header — recognized patterns
    # Trial Balance: "Full name,Debit,Credit"
    # BS/PL: ",Total"  (col0 empty, col1 = "Total")
    # GL/TD: ",Transaction date,Transaction type,..."
    if col0 == "Full name" and col1 in ("Debit", "Credit", "Debit"):
        return RowType.COLUMN_HEADER
    if col0 == "" and col1 == "Total" and all(c == "" for c in stripped[2:]):
        return RowType.COLUMN_HEADER
    if col0 == "" and col1 == "Transaction date":
        return RowType.COLUMN_HEADER
    if col0 == "Account name" and col1 == "Account type":
        return RowType.COLUMN_HEADER

    # 5. Subtotal rows — "Total for X" or bare "TOTAL" or "Gross Profit" / "Net Income"
    if col0.startswith("Total for ") or col0 == "TOTAL":
        return RowType.SUBTOTAL
    if col0 in ("Gross Profit", "Net Income", "Net Other Income", "Net Operating Income",
                "Total Income", "Total Expenses", "Total Other Income", "Total Other Expenses"):
        return RowType.SUBTOTAL

    # Determine if this is a 9-column (GL/TD) context by checking if cols 1-8 are
    # mostly empty (account-header row) or col1 is a date (transaction row) or
    # col8-only (opening balance row).

    # 6. Opening balance — GL/TD only: col0 empty, cols 1-7 empty, col8 has value
    if col0 == "" and col1 == "" and stripped[8] != "" and all(c == "" for c in stripped[1:8]):
        return RowType.OPENING_BALANCE

    # 7. Transaction — col0 empty, col1 looks like a date MM/DD/YYYY
    if col0 == "" and re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', col1):
        return RowType.TRANSACTION

    # 8. Account header — 9-col format: col0 has name, all other cols empty
    # This applies in GL/TD context where account names appear as standalone rows
    if col0 != "" and all(c == "" for c in stripped[1:]):
        # Could be report header (company name, title, date range) or account header
        # Distinguish: report headers come at the top; account headers appear mid-file
        # We mark these as REPORT_HEADER / SECTION since we can't distinguish here
        # without context — caller uses positional info; here we return SECTION for safety
        # and rely on the inventory/parse functions to handle them contextually.
        return RowType.SECTION

    # 9. For 2-col BS/PL rows: col0 has name, col1 might have an amount
    # If col1 has an amount-like value, it's a leaf data row (we'll call it TRANSACTION
    # to mean "data row" in this context)
    if col0 != "" and col1 != "":
        # Check if col1 looks like an amount
        if _looks_like_amount(col1):
            return RowType.TRANSACTION

    # Default: blank/unclassifiable
    return RowType.BLANK


def _looks_like_amount(s: str) -> bool:
    """Return True if *s* looks like a numeric amount (possibly with $, commas, parens)."""
    s = s.strip()
    if not s:
        return False
    # Strip $ and commas and parens
    test = re.sub(r'[$,]', '', s)
    test = re.sub(r'^\((.+)\)$', r'-\1', test)
    try:
        float(test)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Amount parser
# ---------------------------------------------------------------------------

def parse_amount(s: str) -> Decimal:
    """Parse a QB CSV amount string to a Decimal quantized to 0.01.

    Handles:
    - Quoted thousands-comma amounts: '"4,298.12"' -> Decimal('4298.12')
    - Leading $: '$1,234.56' -> Decimal('1234.56')
    - Parentheses for negatives: '(500.00)' -> Decimal('-500.00')
    - Plain: '1234.56' -> Decimal('1234.56')
    - Empty / zero: '' -> Decimal('0.00')
    """
    s = s.strip().strip('"')
    if not s:
        return Decimal("0.00")
    # Remove $ sign
    s = s.replace("$", "")
    # Parentheses = negative
    paren_match = re.match(r'^\(([^)]+)\)$', s)
    if paren_match:
        s = "-" + paren_match.group(1)
    # Remove thousands commas (only commas between digits)
    s = re.sub(r'(?<=\d),(?=\d)', '', s)
    s = s.strip()
    if not s:
        return Decimal("0.00")
    try:
        return Decimal(s).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ValueError(f"Cannot parse amount: {s!r}")


# ---------------------------------------------------------------------------
# Report type detection
# ---------------------------------------------------------------------------

_REPORT_TYPE_MAP = {
    "trial balance": "trial_balance",
    "balance sheet": "balance_sheet",
    "profit and loss": "profit_and_loss",
    "general ledger": "general_ledger",
    "transaction detail by account": "transaction_detail",
}

def _normalized_header(row: list[str]) -> list[str]:
    return [c.strip().lower() for c in row]


def detect_report_type(rows: list[list[str]]) -> Optional[str]:
    """Detect the QB report type from the first few rows of an export.

    Returns a report type key or None for unrecognized files.
    Chart of accounts is detected by its column header.
    Other reports have company/title/date as the first three rows.
    """
    if not rows:
        return None

    for row in rows[:10]:
        header = _normalized_header(row)
        if len(header) >= 3 and header[:3] == ["account name", "account type", "detail type"]:
            return "chart_of_accounts"
        if "name" in header and "type" in header and "detail type" in header:
            return "chart_of_accounts"

    # Multi-row header: rows 0=company, 1=title, 2=date, 3=blank
    if len(rows) >= 2:
        title_row = rows[1][0].strip().lower() if rows[1] else ""
        for key, val in _REPORT_TYPE_MAP.items():
            if title_row == key:
                return val

    return None


def detect_basis(rows: list[list[str]]) -> Optional[str]:
    """Detect the accounting basis ('cash' or 'accrual') from the footer row."""
    for row in reversed(rows):
        if row:
            col0 = row[0].strip()
            if re.match(r'^Cash Basis\b', col0, re.IGNORECASE):
                return "cash"
            if re.match(r'^Accrual Basis\b', col0, re.IGNORECASE):
                return "accrual"
    return None


def detect_date_range(rows: list[list[str]]) -> Optional[str]:
    """Extract the date/date-range string from the report header (row 2)."""
    if len(rows) >= 3 and rows[2]:
        date_str = rows[2][0].strip().strip('"')
        if date_str:
            return date_str
    return None


# ---------------------------------------------------------------------------
# Data classes for parsed outputs (public API)
# ---------------------------------------------------------------------------

@dataclass
class QBAccount:
    """A single row from the Chart of Accounts export."""
    name: str
    account_type: str
    detail_type: str
    lock: str = "No"


@dataclass
class TBEntry:
    """A single row from the Trial Balance CSV."""
    full_name: str
    debit: Decimal
    credit: Decimal


@dataclass
class TrialBalance:
    """Parsed Trial Balance."""
    company: str
    as_of: str
    basis: Optional[str]   # 'cash' | 'accrual' | None
    entries: list[TBEntry] = field(default_factory=list)


@dataclass
class BSRow:
    """A leaf or subtotal row from the Balance Sheet."""
    name: str
    amount: Decimal
    row_type: str  # 'leaf' | 'subtotal' | 'section'


@dataclass
class BalanceSheet:
    """Parsed Balance Sheet."""
    company: str
    as_of: str
    basis: Optional[str]
    rows: list[BSRow] = field(default_factory=list)


@dataclass
class PLRow:
    """A leaf or subtotal row from the Profit and Loss."""
    name: str
    amount: Decimal
    row_type: str  # 'leaf' | 'subtotal' | 'section'


@dataclass
class ProfitAndLoss:
    """Parsed Profit and Loss."""
    company: str
    period: str
    basis: Optional[str]
    rows: list[PLRow] = field(default_factory=list)


@dataclass
class GLTransaction:
    """A single transaction row from the General Ledger."""
    account: str
    txn_date: date
    txn_type: str
    num: str
    name: str
    description: str
    split: str
    amount: Decimal
    balance: Decimal


@dataclass
class GeneralLedger:
    """Parsed General Ledger."""
    company: str
    period: str
    basis: Optional[str]
    transactions: list[GLTransaction] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Export readers
# ---------------------------------------------------------------------------

_XLSX_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
_SUPPORTED_EXPORT_SUFFIXES = {".csv", ".xlsx"}


def iter_export_files(folder: Path) -> list[Path]:
    """Return supported QuickBooks export files in deterministic order."""
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in _SUPPORTED_EXPORT_SUFFIXES
    )


def _read_csv_export(path: Path) -> list[list[str]]:
    """Read a CSV file and return rows as lists of strings."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        return list(reader)


def _cell_column_index(ref: str) -> int:
    """Return zero-based column index from an Excel cell reference like 'AB12'."""
    letters = re.match(r"([A-Z]+)", ref.upper())
    if not letters:
        return 0
    index = 0
    for char in letters.group(1):
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _xlsx_text(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        text_el = cell.find("main:is/main:t", _XLSX_NS)
        return text_el.text if text_el is not None and text_el.text is not None else ""

    value_el = cell.find("main:v", _XLSX_NS)
    value = value_el.text if value_el is not None and value_el.text is not None else ""
    if cell_type == "s":
        try:
            return shared_strings[int(value)]
        except (ValueError, IndexError):
            return ""
    if cell_type == "b":
        return "TRUE" if value == "1" else "FALSE"
    return value


def _read_shared_strings(zf: ZipFile) -> list[str]:
    try:
        data = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(data)
    strings = []
    for si in root.findall("main:si", _XLSX_NS):
        parts = [
            t.text or ""
            for t in si.findall(".//main:t", _XLSX_NS)
        ]
        strings.append("".join(parts))
    return strings


def _first_worksheet_path(zf: ZipFile) -> str:
    try:
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    except KeyError as exc:
        raise ValueError("File is not a readable XLSX workbook") from exc

    first_sheet = workbook.find("main:sheets/main:sheet", _XLSX_NS)
    if first_sheet is None:
        raise ValueError("XLSX workbook has no worksheets")
    rel_id = first_sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
    if not rel_id:
        raise ValueError("XLSX worksheet relationship is missing")

    for rel in rels:
        if rel.attrib.get("Id") == rel_id:
            target = rel.attrib.get("Target", "")
            if target.startswith("/"):
                return target.lstrip("/")
            return "xl/" + target.lstrip("/")
    raise ValueError("XLSX worksheet relationship was not found")


def _read_xlsx_export(path: Path) -> list[list[str]]:
    """Read the first worksheet from a simple XLSX export into string rows."""
    with ZipFile(path) as zf:
        shared_strings = _read_shared_strings(zf)
        worksheet_path = _first_worksheet_path(zf)
        root = ET.fromstring(zf.read(worksheet_path))

    rows: list[list[str]] = []
    for row in root.findall(".//main:sheetData/main:row", _XLSX_NS):
        values: list[str] = []
        for cell in row.findall("main:c", _XLSX_NS):
            ref = cell.attrib.get("r", "")
            col_index = _cell_column_index(ref)
            while len(values) <= col_index:
                values.append("")
            values[col_index] = _xlsx_text(cell, shared_strings)
        rows.append(values)
    return rows


def _read_export_rows(path: Path) -> list[list[str]]:
    """Read a QuickBooks CSV or XLSX export as rows of strings."""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _read_csv_export(path)
    if suffix == ".xlsx":
        return _read_xlsx_export(path)
    raise ValueError(f"Unsupported QuickBooks export format: {path.suffix}")


def parse_chart_of_accounts(path: Path) -> list[QBAccount]:
    """Parse a QuickBooks Chart of Accounts export.

    Returns a list of QBAccount dataclasses.
    Raises ValueError if the file does not look like a CoA export.
    """
    rows = _read_export_rows(path)
    if not rows:
        raise ValueError(f"Empty file: {path}")
    if detect_report_type(rows) != "chart_of_accounts":
        raise ValueError(f"File does not appear to be a Chart of Accounts: {path}")

    header_index = None
    header: list[str] = []
    for idx, row in enumerate(rows[:10]):
        normalized = _normalized_header(row)
        if len(normalized) >= 3 and normalized[:3] == ["account name", "account type", "detail type"]:
            header_index = idx
            header = normalized
            break
        if "name" in normalized and "type" in normalized and "detail type" in normalized:
            header_index = idx
            header = normalized
            break
    if header_index is None:
        raise ValueError(f"File does not appear to be a Chart of Accounts: {path}")

    def _index(*names: str) -> Optional[int]:
        for name in names:
            if name in header:
                return header.index(name)
        return None

    name_idx = _index("account name", "name")
    type_idx = _index("account type", "type")
    detail_idx = _index("detail type")
    lock_idx = _index("lock")
    if name_idx is None or type_idx is None or detail_idx is None:
        raise ValueError(f"File does not appear to be a Chart of Accounts: {path}")

    max_idx = max(name_idx, type_idx, detail_idx, lock_idx or 0)

    accounts = []
    for row in rows[header_index + 1:]:
        if not row or all(c.strip() == "" for c in row):
            continue
        while len(row) <= max_idx:
            row.append("")
        name = row[name_idx].strip()
        if not name:
            continue
        acct_type = row[type_idx].strip()
        detail_type = row[detail_idx].strip()
        lock = row[lock_idx].strip() if lock_idx is not None else "No"
        accounts.append(QBAccount(
            name=name,
            account_type=acct_type,
            detail_type=detail_type,
            lock=lock,
        ))
    return accounts


def parse_trial_balance(path: Path) -> TrialBalance:
    """Parse a QuickBooks Trial Balance export.

    Returns a TrialBalance with entries for each non-blank data row.
    Raises ValueError if the file is not a Trial Balance.
    """
    rows = _read_export_rows(path)
    rtype = detect_report_type(rows)
    if rtype != "trial_balance":
        raise ValueError(f"File does not appear to be a Trial Balance: {path}")

    company = rows[0][0].strip().strip('"') if rows else ""
    as_of = detect_date_range(rows) or ""
    basis = detect_basis(rows)

    entries = []
    in_data = False
    for row in rows:
        while len(row) < 3:
            row.append("")
        col0 = row[0].strip()
        col1 = row[1].strip()
        col2 = row[2].strip()

        # Detect the column header row to start data ingestion
        if col0 == "Full name" and col1 in ("Debit",):
            in_data = True
            continue
        if not in_data:
            continue

        # Skip blank rows (all cells empty)
        if not col0 and not col1 and not col2:
            continue

        # Skip basis footer
        if re.match(r'^(Cash|Accrual) Basis\b', col0, re.IGNORECASE):
            continue

        # Skip TOTAL / subtotal rows
        if col0.startswith("Total for ") or col0 == "TOTAL":
            continue

        # Data rows: any row with col0 non-empty is an account entry
        # (debit in col1, credit in col2, either may be empty/zero)
        if col0:
            debit = parse_amount(col1) if col1 else Decimal("0.00")
            credit = parse_amount(col2) if col2 else Decimal("0.00")
            entries.append(TBEntry(
                full_name=col0,
                debit=debit,
                credit=credit,
            ))

    return TrialBalance(company=company, as_of=as_of, basis=basis, entries=entries)


def parse_balance_sheet(path: Path) -> BalanceSheet:
    """Parse a QuickBooks Balance Sheet export.

    Returns a BalanceSheet with classified rows.
    Raises ValueError if the file is not a Balance Sheet.
    """
    rows = _read_export_rows(path)
    rtype = detect_report_type(rows)
    if rtype != "balance_sheet":
        raise ValueError(f"File does not appear to be a Balance Sheet: {path}")

    company = rows[0][0].strip().strip('"') if rows else ""
    as_of = detect_date_range(rows) or ""
    basis = detect_basis(rows)

    bs_rows = []
    in_data = False
    for row in rows:
        while len(row) < 2:
            row.append("")
        col0 = row[0].strip()
        col1 = row[1].strip()

        row_t = classify_row(row)

        if row_t == RowType.COLUMN_HEADER and col1 == "Total":
            in_data = True
            continue
        if not in_data:
            continue
        if row_t in (RowType.BLANK, RowType.BASIS_FOOTER):
            continue

        if row_t == RowType.SUBTOTAL:
            # "Total for X" and bare total rows
            amount = Decimal("0.00")
            if col1:
                try:
                    amount = parse_amount(col1)
                except ValueError:
                    pass
            bs_rows.append(BSRow(name=col0, amount=amount, row_type="subtotal"))
        elif col1 and _looks_like_amount(col1):
            # Leaf row with amount
            try:
                amount = parse_amount(col1)
            except ValueError:
                amount = Decimal("0.00")
            bs_rows.append(BSRow(name=col0, amount=amount, row_type="leaf"))
        elif col0 and not col1:
            # Section header
            bs_rows.append(BSRow(name=col0, amount=Decimal("0.00"), row_type="section"))

    return BalanceSheet(company=company, as_of=as_of, basis=basis, rows=bs_rows)


def parse_profit_and_loss(path: Path) -> ProfitAndLoss:
    """Parse a QuickBooks Profit and Loss export.

    Returns a ProfitAndLoss with classified rows.
    Raises ValueError if the file is not a P&L.
    """
    rows = _read_export_rows(path)
    rtype = detect_report_type(rows)
    if rtype != "profit_and_loss":
        raise ValueError(f"File does not appear to be a Profit and Loss: {path}")

    company = rows[0][0].strip().strip('"') if rows else ""
    period = detect_date_range(rows) or ""
    basis = detect_basis(rows)

    pl_rows = []
    in_data = False
    for row in rows:
        while len(row) < 2:
            row.append("")
        col0 = row[0].strip()
        col1 = row[1].strip()

        row_t = classify_row(row)

        if row_t == RowType.COLUMN_HEADER and col1 == "Total":
            in_data = True
            continue
        if not in_data:
            continue
        if row_t in (RowType.BLANK, RowType.BASIS_FOOTER):
            continue

        if row_t == RowType.SUBTOTAL:
            amount = Decimal("0.00")
            if col1:
                try:
                    amount = parse_amount(col1)
                except ValueError:
                    pass
            pl_rows.append(PLRow(name=col0, amount=amount, row_type="subtotal"))
        elif col1 and _looks_like_amount(col1):
            try:
                amount = parse_amount(col1)
            except ValueError:
                amount = Decimal("0.00")
            pl_rows.append(PLRow(name=col0, amount=amount, row_type="leaf"))
        elif col0 and not col1:
            pl_rows.append(PLRow(name=col0, amount=Decimal("0.00"), row_type="section"))

    return ProfitAndLoss(company=company, period=period, basis=basis, rows=pl_rows)


def parse_general_ledger(path: Path) -> GeneralLedger:
    """Parse a QuickBooks General Ledger export.

    Returns a GeneralLedger with transaction rows only.
    Opening-balance and subtotal rows are classified and skipped.
    """
    rows = _read_export_rows(path)
    rtype = detect_report_type(rows)
    if rtype not in ("general_ledger", "transaction_detail"):
        raise ValueError(
            f"File does not appear to be a General Ledger or Transaction Detail: {path}"
        )

    company = rows[0][0].strip().strip('"') if rows else ""
    period = detect_date_range(rows) or ""
    basis = detect_basis(rows)

    transactions = []
    current_account = ""
    in_data = False

    for row in rows:
        while len(row) < 9:
            row.append("")

        row_t = classify_row(row)

        if row_t == RowType.COLUMN_HEADER and row[1].strip() == "Transaction date":
            in_data = True
            continue
        if not in_data:
            continue
        if row_t in (RowType.BLANK, RowType.BASIS_FOOTER):
            continue

        col0 = row[0].strip()

        if row_t == RowType.SECTION:
            # Account header — update current account context
            current_account = col0
            continue
        if row_t == RowType.OPENING_BALANCE:
            # Opening balance row — skip (not a transaction)
            continue
        if row_t == RowType.SUBTOTAL:
            continue
        if row_t == RowType.TRANSACTION:
            date_str = row[1].strip()
            try:
                txn_date = _parse_us_date(date_str)
            except ValueError:
                continue
            txn_type = row[2].strip()
            num = row[3].strip()
            name = row[4].strip()
            description = row[5].strip()
            split = row[6].strip()
            amt_str = row[7].strip()
            bal_str = row[8].strip()
            try:
                amount = parse_amount(amt_str)
            except ValueError:
                amount = Decimal("0.00")
            try:
                balance = parse_amount(bal_str)
            except ValueError:
                balance = Decimal("0.00")
            transactions.append(GLTransaction(
                account=current_account,
                txn_date=txn_date,
                txn_type=txn_type,
                num=num,
                name=name,
                description=description,
                split=split,
                amount=amount,
                balance=balance,
            ))

    return GeneralLedger(company=company, period=period, basis=basis, transactions=transactions)


def _parse_us_date(s: str) -> date:
    """Parse MM/DD/YYYY date string."""
    from datetime import datetime
    return datetime.strptime(s, "%m/%d/%Y").date()


# ---------------------------------------------------------------------------
# QB account name mapper
# ---------------------------------------------------------------------------

# Root account mapping by QB account type
_QB_TYPE_ROOT: dict[str, str] = {
    "bank": "Assets:Bank:",
    "accounts receivable (a/r)": "Assets:Receivable:",
    "other current assets": "Assets:Other:",
    "fixed assets": "Assets:Fixed:",
    "other assets": "Assets:Other:",
    "accounts payable (a/p)": "Liabilities:Payable:",
    "credit card": "Liabilities:CreditCard:",
    "other current liabilities": "Liabilities:Other:",
    "long-term liabilities": "Liabilities:Other:",
    "equity": "Equity:",
    "income": "Income:",
    "other income": "Income:",
    "cost of goods sold": "Expenses:",
    "expenses": "Expenses:",
    "other expense": "Expenses:",
}

# Regex for valid beancount segment characters: start with capital/digit, then letters/digits/hyphens
_VALID_SEG_RE = re.compile(r'^[A-Z0-9][A-Za-z0-9-]*$')

# Curly and straight apostrophes
_APOSTROPHE_RE = re.compile(r"[’'‘`´]")

# Characters not allowed in beancount segments (keep letters, digits, hyphens)
_INVALID_CHAR_RE = re.compile(r'[^A-Za-z0-9-]')

# Runs of hyphens
_MULTI_HYPHEN_RE = re.compile(r'-{2,}')


def _sanitize_segment(seg: str) -> str:
    """Convert a QB account name segment into a valid beancount segment.

    Steps:
    1. Remove apostrophes (curly and straight)
    2. Replace any invalid character with a hyphen
    3. Collapse multiple hyphens
    4. Strip leading/trailing hyphens
    5. Ensure leading capital letter or digit (prefix 'X-' if needed)
    6. Return at least 'X' if empty after sanitization
    """
    # 1. Remove apostrophes
    seg = _APOSTROPHE_RE.sub("", seg)
    # 2. Replace invalid chars with hyphen
    seg = _INVALID_CHAR_RE.sub("-", seg)
    # 3. Collapse multiple hyphens
    seg = _MULTI_HYPHEN_RE.sub("-", seg)
    # 4. Strip leading/trailing hyphens
    seg = seg.strip("-")
    # 5. Ensure leading capital or digit
    if not seg:
        return "X"
    if not re.match(r'^[A-Z0-9]', seg):
        # Capitalize first letter if it's lowercase
        if seg[0].isalpha():
            seg = seg[0].upper() + seg[1:]
        else:
            seg = "X-" + seg
    # Final check
    if not _VALID_SEG_RE.match(seg):
        # Replace anything remaining that's invalid
        seg = re.sub(r'[^A-Za-z0-9-]', '-', seg)
        seg = _MULTI_HYPHEN_RE.sub("-", seg).strip("-")
        if not seg:
            seg = "X"
        if not re.match(r'^[A-Z0-9]', seg):
            seg = seg[0].upper() + seg[1:]
    return seg


def _qb_name_to_segments(qb_name: str) -> list[str]:
    """Split a QB account name on ':' and sanitize each segment."""
    raw_segments = qb_name.split(":")
    sanitized = []
    for seg in raw_segments:
        seg = seg.strip()
        sanitized.append(_sanitize_segment(seg))
    return sanitized


# Global collision tracker for deterministic suffix assignment
# Maps base beancount path -> list of original QB names that map to it
_collision_registry: dict[str, list[str]] = {}


def _reset_collision_registry() -> None:
    """Reset the collision registry (for testing)."""
    _collision_registry.clear()


def map_qb_account(qb_name: str, qb_type: str) -> str:
    """Map a QB account name and type to a beancount account name.

    Deterministic and injective enough for practical use.
    On collision (two different QB names mapping to the same base path),
    append a numeric suffix deterministically.

    Returns a valid beancount account string.
    """
    type_key = qb_type.strip().lower()
    root_prefix = _QB_TYPE_ROOT.get(type_key, "Expenses:")

    segments = _qb_name_to_segments(qb_name)
    base_path = root_prefix + ":".join(segments)

    # Remove trailing colon if root already ends with ":"
    # (root prefix always ends with ":")
    # base_path is like "Assets:Bank:Checking-4512-1"

    # Collision check
    if base_path not in _collision_registry:
        _collision_registry[base_path] = [qb_name]
        return base_path

    existing = _collision_registry[base_path]
    if qb_name in existing:
        # Same name already registered — return the deterministic path
        idx = existing.index(qb_name)
        if idx == 0:
            return base_path
        return f"{base_path}-{idx}"

    # New collision — assign next suffix
    existing.append(qb_name)
    suffix_idx = len(existing) - 1
    return f"{base_path}-{suffix_idx}"


# ---------------------------------------------------------------------------
# Import-readiness report
# ---------------------------------------------------------------------------

# Expected report slots in a complete QB export
_EXPECTED_REPORTS = [
    "chart_of_accounts",
    "trial_balance",
    "balance_sheet",
    "balance_sheet_comparison",
    "profit_and_loss",
    "general_ledger",
    "transaction_detail",
]

_REPORT_LABELS = {
    "chart_of_accounts": "Chart of Accounts",
    "trial_balance": "Trial Balance (prior-period end)",
    "balance_sheet": "Balance Sheet (prior-period end)",
    "balance_sheet_comparison": "Balance Sheet (comparison period end)",
    "profit_and_loss": "Profit and Loss (comparison period)",
    "general_ledger": "General Ledger",
    "transaction_detail": "Transaction Detail by Account",
}


@dataclass
class FileFingerprint:
    """Metadata fingerprinted from a single QuickBooks export file."""
    path: str
    report_type: Optional[str]
    date_range: Optional[str]
    basis: Optional[str]
    row_count: int
    ambiguous: bool = False


@dataclass
class ReadinessSlot:
    """Status of a single expected report slot."""
    report_key: str
    label: str
    status: str  # 'present' | 'missing' | 'blocked'
    file: Optional[str] = None
    block_reason: Optional[str] = None


@dataclass
class ReadinessReport:
    """Import-readiness report for a QB export folder."""
    folder: str
    slots: list[ReadinessSlot] = field(default_factory=list)
    ambiguous_files: list[str] = field(default_factory=list)
    fingerprints: list[FileFingerprint] = field(default_factory=list)

    def is_ready(self) -> bool:
        """Return True only when all required slots are present and unblocked."""
        return all(s.status == "present" for s in self.slots)

    def blocked_slots(self) -> list[ReadinessSlot]:
        return [s for s in self.slots if s.status == "blocked"]

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict."""
        return {
            "folder": self.folder,
            "ready": self.is_ready(),
            "slots": [
                {
                    "key": s.report_key,
                    "label": s.label,
                    "status": s.status,
                    "file": s.file,
                    "block_reason": s.block_reason,
                }
                for s in self.slots
            ],
            "ambiguous_files": self.ambiguous_files,
            "fingerprints": [
                {
                    "path": fp.path,
                    "report_type": fp.report_type,
                    "date_range": fp.date_range,
                    "basis": fp.basis,
                    "row_count": fp.row_count,
                    "ambiguous": fp.ambiguous,
                }
                for fp in self.fingerprints
            ],
        }


def _fingerprint_file(path: Path) -> FileFingerprint:
    """Read an export and return a FileFingerprint."""
    try:
        rows = _read_export_rows(path)
    except Exception as exc:
        return FileFingerprint(
            path=str(path),
            report_type=None,
            date_range=None,
            basis=None,
            row_count=0,
            ambiguous=True,
        )
    rtype = detect_report_type(rows)
    date_range = detect_date_range(rows)
    basis = detect_basis(rows)
    return FileFingerprint(
        path=str(path),
        report_type=rtype,
        date_range=date_range,
        basis=basis,
        row_count=len(rows),
        ambiguous=(rtype is None),
    )


_MONTHS = {
    name.lower(): num
    for num, name in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"],
        start=1,
    )
}
for _num, _abbr in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    start=1,
):
    _MONTHS.setdefault(_abbr.lower(), _num)


def _last_day_of_month(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def parse_qb_date_range(text: str | None) -> tuple[Optional[date], Optional[date]]:
    """Parse QuickBooks header date strings into (start, end) dates.

    Handles: "As of Dec 31, 2025" -> (None, 2025-12-31);
    "January-May, 2026" -> (2026-01-01, 2026-05-31);
    "January-December, 2025" -> (2025-01-01, 2025-12-31).
    Unrecognized text -> (None, None).
    """
    if not text:
        return None, None
    cleaned = text.strip().strip('"')
    m = re.match(r"As of\s+([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", cleaned)
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            return None, date(int(m.group(3)), month, int(m.group(2)))
    m = re.match(r"([A-Za-z]+)\s*-\s*([A-Za-z]+),\s*(\d{4})", cleaned)
    if m:
        start_month = _MONTHS.get(m.group(1).lower())
        end_month = _MONTHS.get(m.group(2).lower())
        year = int(m.group(3))
        if start_month and end_month:
            return date(year, start_month, 1), _last_day_of_month(year, end_month)
    return None, None


def _fp_end_date(fp: FileFingerprint) -> Optional[date]:
    return parse_qb_date_range(fp.date_range)[1]


def _assign_slots(
    fingerprints: list[FileFingerprint],
) -> tuple[dict[str, FileFingerprint], list[str]]:
    """Assign fingerprints to report slots by period semantics.

    The comparison period is the period of the most recent ranged report
    (general ledger / P&L by latest end date); the prior-period end is the
    day before the comparison period starts. Trial balance and balance
    sheets are assigned by their parsed as-of dates, not filename order —
    QuickBooks downloads commonly produce "(1)" duplicate filenames whose
    sort order has nothing to do with their content.
    """
    by_type: dict[str, list[FileFingerprint]] = {}
    ambiguous: list[str] = []

    for fp in fingerprints:
        if fp.ambiguous:
            ambiguous.append(fp.path)
            continue
        by_type.setdefault(fp.report_type, []).append(fp)

    slot_map: dict[str, FileFingerprint] = {}

    if by_type.get("chart_of_accounts"):
        slot_map["chart_of_accounts"] = by_type["chart_of_accounts"][0]

    def _latest_end(candidates: list[FileFingerprint]) -> FileFingerprint:
        return max(candidates, key=lambda fp: (_fp_end_date(fp) or date.min))

    # Ranged reports: the comparison period is the latest-ending range.
    comparison_start: Optional[date] = None
    for key in ("general_ledger", "transaction_detail", "profit_and_loss"):
        candidates = by_type.get(key, [])
        if candidates:
            chosen = _latest_end(candidates)
            slot_map[key] = chosen
            start, _end = parse_qb_date_range(chosen.date_range)
            if start and (comparison_start is None or start > comparison_start):
                comparison_start = start

    prior_end = comparison_start - timedelta(days=1) if comparison_start else None

    # Trial balance: as-of == prior-period end preferred; else the latest
    # as-of not after prior_end; else the latest overall.
    tb_candidates = by_type.get("trial_balance", [])
    if tb_candidates:
        chosen_tb = None
        if prior_end is not None:
            exact = [fp for fp in tb_candidates if _fp_end_date(fp) == prior_end]
            if exact:
                chosen_tb = exact[0]
            else:
                not_after = [
                    fp for fp in tb_candidates
                    if _fp_end_date(fp) and _fp_end_date(fp) <= prior_end
                ]
                if not_after:
                    chosen_tb = _latest_end(not_after)
        slot_map["trial_balance"] = chosen_tb or _latest_end(tb_candidates)

    # Balance sheets: prior-period end vs comparison end by as-of date.
    bs_candidates = by_type.get("balance_sheet", [])
    if len(bs_candidates) == 1:
        slot_map["balance_sheet"] = bs_candidates[0]
    elif len(bs_candidates) >= 2:
        dated = sorted(bs_candidates, key=lambda fp: (_fp_end_date(fp) or date.min))
        if prior_end is not None:
            prior_matches = [fp for fp in dated if _fp_end_date(fp) == prior_end]
            others = [fp for fp in dated if fp not in prior_matches]
            if prior_matches:
                slot_map["balance_sheet"] = prior_matches[0]
                if others:
                    slot_map["balance_sheet_comparison"] = _latest_end(others)
            else:
                # Earlier as-of -> prior-period end; latest -> comparison.
                slot_map["balance_sheet"] = dated[0]
                slot_map["balance_sheet_comparison"] = dated[-1]
        else:
            slot_map["balance_sheet"] = dated[0]
            slot_map["balance_sheet_comparison"] = dated[-1]

    return slot_map, ambiguous


def inventory(folder: str | Path) -> ReadinessReport:
    """Fingerprint all CSV/XLSX exports in *folder* and produce a readiness report.

    Per-file blocking flags:
    - Trial Balance with accrual basis -> BLOCKED (cash-basis required)
    - Missing expected file -> MISSING (partial onboarding resumable)
    - Unrecognized file -> listed as ambiguous

    Returns a ReadinessReport with deterministic JSON output.
    """
    folder = Path(folder)
    export_files = iter_export_files(folder)

    fingerprints = [_fingerprint_file(f) for f in export_files]
    slot_map, ambiguous_files = _assign_slots(fingerprints)

    slots = []
    for key in _EXPECTED_REPORTS:
        label = _REPORT_LABELS[key]
        if key not in slot_map:
            slots.append(ReadinessSlot(
                report_key=key,
                label=label,
                status="missing",
            ))
            continue

        fp = slot_map[key]

        # Opening balances for /books are cash-basis; an accrual TB would mix bases.
        if key == "trial_balance" and fp.basis == "accrual":
            slots.append(ReadinessSlot(
                report_key=key,
                label=label,
                status="blocked",
                file=fp.path,
                block_reason=(
                    "Trial Balance is Accrual Basis. "
                    "Re-export it from QuickBooks with 'Cash Basis' selected, "
                    "or use the cash-basis Balance Sheet fallback if available."
                ),
            ))
            continue

        slots.append(ReadinessSlot(
            report_key=key,
            label=label,
            status="present",
            file=fp.path,
        ))

    return ReadinessReport(
        folder=str(folder),
        slots=slots,
        ambiguous_files=ambiguous_files,
        fingerprints=fingerprints,
    )


# ---------------------------------------------------------------------------
# Opening balance import
# ---------------------------------------------------------------------------

@dataclass
class ImportResult:
    """Result of import_opening."""
    entries_written: int
    accounts_opened: int
    ledger_path: str
    coa_path: str
    errors: list[str] = field(default_factory=list)
    success: bool = True


def _balance_sheet_opening_rows(bs: BalanceSheet) -> list[TBEntry]:
    """Derive trial-balance-shaped opening rows from a cash-basis balance sheet.

    Cash-basis opening balances only involve balance-sheet accounts (P&L
    accounts carry nothing across a year boundary), so the prior-period
    cash-basis balance sheet is an exact substitute for a cash-basis trial
    balance at cutover: asset leaves are debit balances, liability leaves are
    credit balances, and equity leaves are excluded (the single
    Equity:Opening-Balances offset preserves total equity).
    """
    rows: list[TBEntry] = []
    bucket = None
    for row in bs.rows:
        name_lower = row.name.lower()
        if row.row_type == "section":
            if "asset" in name_lower:
                bucket = "assets"
            elif "liabilit" in name_lower:
                bucket = "liabilities"
            elif "equity" in name_lower:
                bucket = "equity"
            continue
        if row.row_type != "leaf" or row.amount == Decimal("0.00"):
            continue
        if bucket == "assets":
            debit = row.amount if row.amount > 0 else Decimal("0.00")
            credit = -row.amount if row.amount < 0 else Decimal("0.00")
            rows.append(TBEntry(full_name=row.name, debit=debit, credit=credit))
        elif bucket == "liabilities":
            credit = row.amount if row.amount > 0 else Decimal("0.00")
            debit = -row.amount if row.amount < 0 else Decimal("0.00")
            rows.append(TBEntry(full_name=row.name, debit=debit, credit=credit))
        # equity leaves intentionally excluded — consolidated into the offset
    return rows


def import_opening(
    entity,   # Entity from entity.py
    folder: str | Path,
    cutover: date,
    source: str = "trial-balance",
) -> ImportResult:
    """Post opening balances from a cash-basis trial balance.

    Requires the TB in *folder* to be cash-basis; refuses otherwise.
    For each non-zero account in the TB:
      - Builds an opening Entry with postings dated *cutover*
      - Debits positive, credits negative (normal TB convention)
      - Offset to Equity:Opening-Balances
    Emits open directives for every mapped account.
    Validates with the ledger validator.
    Writes chart-of-accounts.beancount additions and appends opening balances to
    the canonical ledger store in one transaction.

    When source == "balance-sheet", opening rows derive from the prior-period
    cash-basis balance sheet instead (see _balance_sheet_opening_rows) — the
    supported fallback when the trial balance export is accrual-basis.

    Returns ImportResult.
    """
    from .ledger.model import Entry, Open, Posting
    from .ledger.projections import render_store_ledger
    from .ledger.store import LedgerStore, default_store_path
    from .ledger.writer import render_ledger
    from .ledger.validator import validate

    folder = Path(folder)
    report = inventory(folder)
    store_path = default_store_path(entity.path)

    if source == "balance-sheet":
        bs_slot = next((s for s in report.slots if s.report_key == "balance_sheet"), None)
        if bs_slot is None or bs_slot.status != "present":
            return ImportResult(
                entries_written=0,
                accounts_opened=0,
                ledger_path=str(store_path),
                coa_path=str(entity.coa_path),
                errors=["Prior-period balance sheet not found; cannot derive opening balances."],
                success=False,
            )
        bs = parse_balance_sheet(Path(bs_slot.file))
        if bs.basis != "cash":
            return ImportResult(
                entries_written=0,
                accounts_opened=0,
                ledger_path=str(store_path),
                coa_path=str(entity.coa_path),
                errors=[
                    f"Prior-period balance sheet is {bs.basis or 'unknown'} basis; "
                    "cash basis required for the balance-sheet opening fallback."
                ],
                success=False,
            )
        tb = TrialBalance(
            company=bs.company,
            as_of=bs.as_of,
            basis=bs.basis,
            entries=_balance_sheet_opening_rows(bs),
        )
    elif source == "trial-balance":
        # Check TB slot
        tb_slot = next((s for s in report.slots if s.report_key == "trial_balance"), None)
        if tb_slot is None or tb_slot.status != "present":
            if tb_slot and tb_slot.status == "blocked":
                return ImportResult(
                    entries_written=0,
                    accounts_opened=0,
                    ledger_path=str(store_path),
                    coa_path=str(entity.coa_path),
                    errors=[
                        (tb_slot.block_reason or "Trial Balance blocked")
                        + " (Alternatively: re-run with --source balance-sheet to derive "
                        "openings from the cash-basis balance sheet.)"
                    ],
                    success=False,
                )
            return ImportResult(
                entries_written=0,
                accounts_opened=0,
                ledger_path=str(store_path),
                coa_path=str(entity.coa_path),
                errors=["Trial Balance not found in folder. Run 'books qb inventory' first."],
                success=False,
            )

        tb_path = Path(tb_slot.file)
        tb = parse_trial_balance(tb_path)

        if tb.basis != "cash":
            return ImportResult(
                entries_written=0,
                accounts_opened=0,
                ledger_path=str(store_path),
                coa_path=str(entity.coa_path),
                errors=[
                    f"Trial Balance is {tb.basis or 'unknown'} basis. "
                    "Re-export it from QuickBooks with 'Cash Basis' selected, "
                    "or use the cash-basis Balance Sheet fallback if available."
                ],
                success=False,
            )
    else:
        return ImportResult(
            entries_written=0,
            accounts_opened=0,
            ledger_path=str(store_path),
            coa_path=str(entity.coa_path),
            errors=[f"Unknown opening source: {source!r} (expected trial-balance or balance-sheet)"],
            success=False,
        )

    # Load chart of accounts for type lookup
    coa_slot = next((s for s in report.slots if s.report_key == "chart_of_accounts"), None)
    qb_type_map: dict[str, str] = {}
    if coa_slot and coa_slot.status == "present":
        try:
            coa_accounts = parse_chart_of_accounts(Path(coa_slot.file))
            qb_type_map = {a.name: a.account_type for a in coa_accounts}
        except Exception:
            pass

    # Reset collision registry for deterministic mapping
    _reset_collision_registry()

    opens: list[Open] = []
    postings: list[Posting] = []
    errors: list[str] = []
    coa_additions: list[str] = []

    # Equity:Opening-Balances account
    opening_equity_account = "Equity:Opening-Balances"
    opens.append(Open(date=cutover, account=opening_equity_account))

    for entry in tb.entries:
        if entry.debit == Decimal("0.00") and entry.credit == Decimal("0.00"):
            continue

        qb_type = qb_type_map.get(entry.full_name, "Expenses")
        try:
            bc_account = map_qb_account(entry.full_name, qb_type)
        except Exception as exc:
            errors.append(f"Cannot map account '{entry.full_name}': {exc}")
            continue

        try:
            from .ledger.model import _validate_account_name
            _validate_account_name(bc_account)
        except ValueError as exc:
            errors.append(f"Invalid beancount account for '{entry.full_name}': {exc}")
            continue

        opens.append(Open(date=cutover, account=bc_account))

        # Net amount: debit positive, credit negative
        net = entry.debit - entry.credit
        if net == Decimal("0.00"):
            continue

        postings.append(Posting(
            account=bc_account,
            amount=net,
            currency="USD",
            meta=(("qb-name", entry.full_name),),
        ))

    if not postings:
        return ImportResult(
            entries_written=0,
            accounts_opened=len(opens),
            ledger_path=str(store_path),
            coa_path=str(entity.coa_path),
            errors=errors or ["No non-zero accounts found in trial balance."],
            success=len(errors) == 0,
        )

    # Balance postings with offset to Equity:Opening-Balances
    total = sum(p.amount for p in postings)
    offset = (-total).quantize(Decimal("0.01"))
    postings.append(Posting(
        account=opening_equity_account,
        amount=offset,
        currency="USD",
    ))

    opening_entry = Entry(
        date=cutover,
        narration=(
            "Opening balances from QuickBooks cash-basis trial balance"
            if source == "trial-balance"
            else "Opening balances derived from QuickBooks cash-basis balance sheet"
        ),
        meta=(
            ("import-source", "quickbooks-opening"),
            ("qb-as-of", tb.as_of),
        ),
        postings=tuple(postings),
    )

    # Build ledger text
    ledger_text = render_ledger(
        opens=opens,
        entries=[opening_entry],
        balances=[],
        title="Opening Balances",
    )

    # Validate
    v_errors = validate(ledger_text)
    if v_errors:
        error_strs = [str(e) for e in v_errors]
        return ImportResult(
            entries_written=0,
            accounts_opened=len(opens),
            ledger_path=str(store_path),
            coa_path=str(entity.coa_path),
            errors=error_strs,
            success=False,
        )

    store = LedgerStore(store_path)
    store.initialize()
    try:
        with store.transaction() as conn:
            store.append_audit_event(
                "intent",
                {
                    "session_id": "quickbooks-opening",
                    "description": f"import QuickBooks opening balances source={source}",
                    "entries": 1,
                },
                conn,
            )
            store.insert_opens(opens, conn)
            store.insert_entries([opening_entry], conn)
            store.set_meta("canonical", "true", conn)
            store.set_meta("title", entity.entity_config.get("name", "Books"), conn)
            projection = render_store_ledger(store_path, conn=conn)
            projection_errors = validate(projection)
            if projection_errors:
                error_strs = [str(e) for e in projection_errors]
                raise ValueError("; ".join(error_strs))
            store.append_audit_event(
                "entry-written",
                {
                    "session_id": "quickbooks-opening",
                    "source_id": "quickbooks-opening",
                },
                conn,
            )
            store.append_audit_event(
                "ledger-store-sealed",
                {
                    "session_id": "quickbooks-opening",
                    "entries": 1,
                    "source_ids": ["quickbooks-opening"],
                },
                conn,
            )
    except Exception as exc:
        return ImportResult(
            entries_written=0,
            accounts_opened=len(opens),
            ledger_path=str(store_path),
            coa_path=str(entity.coa_path),
            errors=[str(exc)],
            success=False,
        )

    # Write CoA additions (open directives for new accounts)
    coa_path = entity.coa_path
    coa_additions_text = "\n".join(
        f"{cutover.strftime('%Y-%m-%d')} open {o.account}"
        for o in opens
        if o.account != opening_equity_account
    )
    if coa_additions_text:
        tmp_coa = coa_path.parent / (coa_path.name + ".tmp")
        existing_coa = ""
        if coa_path.exists():
            existing_coa = coa_path.read_text(encoding="utf-8")
        new_coa = existing_coa.rstrip("\n") + "\n\n; QB opening-balance accounts\n" + coa_additions_text + "\n"
        tmp_coa.write_text(new_coa, encoding="utf-8")
        os.replace(tmp_coa, coa_path)

    return ImportResult(
        entries_written=1,
        accounts_opened=len(opens),
        ledger_path=str(store_path),
        coa_path=str(coa_path),
        errors=errors,
        success=True,
    )


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

def add_parser(subparsers) -> None:
    """Register the ``qb`` subcommand onto *subparsers*."""
    qb_parser = subparsers.add_parser(
        "qb",
        help="QuickBooks export inventory and import",
    )
    qb_sub = qb_parser.add_subparsers(dest="qb_command", required=True)

    # inventory <folder>
    inv_parser = qb_sub.add_parser(
        "inventory",
        help="Fingerprint a folder of QB exports and produce an import-readiness report",
    )
    inv_parser.add_argument(
        "folder",
        type=Path,
        help="Folder containing QuickBooks CSV/XLSX exports",
    )
    inv_parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output as JSON",
    )

    # import-opening <folder> --entity <path> --cutover YYYY-MM-DD
    imp_parser = qb_sub.add_parser(
        "import-opening",
        help="Post opening balances from a cash-basis trial balance",
    )
    imp_parser.add_argument(
        "folder",
        type=Path,
        help="Folder containing QuickBooks CSV/XLSX exports",
    )
    imp_parser.add_argument(
        "--entity",
        type=Path,
        required=True,
        dest="entity_path",
        help="Path to the entity directory",
    )
    imp_parser.add_argument(
        "--cutover",
        required=True,
        help="Cutover date YYYY-MM-DD (opening entries will be dated this day)",
    )
    imp_parser.add_argument(
        "--source",
        choices=["trial-balance", "balance-sheet"],
        default="trial-balance",
        help=(
            "Where opening balances come from. 'balance-sheet' is the supported "
            "fallback when the trial balance export is accrual-basis."
        ),
    )


def run(args) -> int:
    """Execute the qb subcommand described by *args*."""
    from datetime import datetime

    if args.qb_command == "inventory":
        report = inventory(args.folder)
        if args.as_json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print(f"QuickBooks Import Readiness: {args.folder}")
            print()
            for slot in report.slots:
                status_icon = {"present": "[OK]", "missing": "[MISSING]", "blocked": "[BLOCKED]"}.get(
                    slot.status, "[?]"
                )
                print(f"  {status_icon} {slot.label}")
                if slot.file:
                    print(f"        File: {slot.file}")
                if slot.block_reason:
                    print(f"        Reason: {slot.block_reason}")
            if report.ambiguous_files:
                print()
                print("  Unrecognized files (ambiguous):")
                for f in report.ambiguous_files:
                    print(f"    ? {f}")
            print()
            if report.is_ready():
                print("Status: READY for opening-balance import.")
            else:
                blocked = report.blocked_slots()
                missing = [s for s in report.slots if s.status == "missing"]
                print(f"Status: NOT READY ({len(blocked)} blocked, {len(missing)} missing)")
        return 0

    elif args.qb_command == "import-opening":
        from .entity import load_entity
        try:
            entity = load_entity(args.entity_path)
        except FileNotFoundError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        try:
            cutover = datetime.strptime(args.cutover, "%Y-%m-%d").date()
        except ValueError:
            print(f"Error: --cutover must be YYYY-MM-DD, got {args.cutover!r}", file=sys.stderr)
            return 1

        result = import_opening(entity, args.folder, cutover, source=args.source)
        if result.success:
            print(f"Opening balances imported successfully.")
            print(f"  Accounts opened: {result.accounts_opened}")
            print(f"  Entries written: {result.entries_written}")
            print(f"  Ledger: {result.ledger_path}")
            if result.errors:
                print(f"  Warnings:")
                for e in result.errors:
                    print(f"    - {e}")
        else:
            print("Error: Import failed.", file=sys.stderr)
            for e in result.errors:
                print(f"  - {e}", file=sys.stderr)
            return 1
        return 0

    print(f"Unknown qb command: {args.qb_command}", file=sys.stderr)
    return 2
