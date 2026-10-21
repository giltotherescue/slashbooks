"""Generic bank and card CSV reader with layout detection.

This connector reads statement-style CSV exports from banks and card issuers.
It does two separate jobs:

- ``detect_layout`` looks at a file and proposes a column mapping, a date
  format, a decimal style and a sign convention. Each decision carries its
  evidence. When the evidence does not decide a field, the field is listed in
  ``uncertain`` and the caller must supply it. Detection never guesses
  silently.
- ``parse_with_layout`` reads a file with a confirmed layout and returns rows
  in one sign convention, plus any statement metadata the file prints.

Sign convention of parsed rows
------------------------------
``amount`` is from the account holder's view of the account, which is the
same axis the bank-feed contract and ``books ingest`` use:

- Bank account: a deposit is positive, a withdrawal is negative.
- Card account: a payment or refund is positive, a charge is negative.

Layout ``sign`` values:

- ``standard``: the file already uses that convention.
- ``inverted``: the file prints charges and withdrawals as positive numbers.
- ``debit_credit``: separate debit and credit columns; ``amount = credit - debit``
  using absolute values.
- ``direction_column``: amounts are unsigned and a type column says
  ``debit``/``credit``.

Running balances are converted to the same axis. A card balance that the file
prints as a positive amount owed becomes negative, which matches the ledger's
liability sign.

All CSV content is untrusted data. Values are parsed, never executed.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .. import currency as currency_mod

JsonObject = dict[str, Any]

MONEY = Decimal("0.01")
MAX_PREAMBLE_ROWS = 40
MAX_FILE_BYTES = 25 * 1024 * 1024

DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%d/%m/%Y",
    "%d/%m/%y",
    "%Y/%m/%d",
    "%m-%d-%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%Y%m%d",
    "%d %b %Y",
    "%b %d, %Y",
    "%d-%b-%Y",
    "%d-%b-%y",
)

SIGN_MODES = ("standard", "inverted", "debit_credit", "direction_column")
ROLES = ("date", "description", "amount", "debit", "credit", "balance", "reference",
         "status", "direction", "currency", "memo")

# Header names, compared after lowercasing and collapsing punctuation.
_HEADER_ROLES: dict[str, tuple[str, ...]] = {
    "posted_date": ("posting date", "post date", "posted date", "date posted", "posted"),
    "date": ("date", "transaction date", "trans date", "trans. date", "txn date",
             "booking date", "effective date", "value date"),
    "description": ("description", "transaction description", "payee", "merchant",
                    "merchant name", "name", "narrative", "particulars",
                    "original description"),
    "memo": ("memo", "notes", "extended details", "additional info"),
    "amount": ("amount", "transaction amount", "amount usd", "amt", "net amount"),
    "debit": ("debit", "debits", "debit amount", "withdrawal", "withdrawals",
              "withdrawal amount", "money out", "paid out", "charge", "charges"),
    "credit": ("credit", "credits", "credit amount", "deposit", "deposits",
               "deposit amount", "money in", "paid in", "payment", "payments"),
    "balance": ("balance", "running balance", "running bal", "running bal.",
                "ledger balance", "available balance", "balance usd"),
    "reference": ("reference", "ref", "reference number", "ref number", "check number",
                  "check or slip #", "check or slip", "check #", "check no", "cheque number",
                  "transaction id", "transaction reference", "fitid", "id"),
    "status": ("status", "transaction status", "posting status"),
    "direction": ("type", "transaction type", "details", "debit/credit", "dr/cr",
                  "credit/debit", "cr/dr", "direction"),
    "currency": ("currency", "currency code", "ccy"),
}

_DIRECTION_IN = {"credit", "cr", "c", "deposit", "dslip", "in", "return", "refund"}
_DIRECTION_OUT = {"debit", "dr", "d", "withdrawal", "check", "out", "sale", "purchase", "fee"}

_MONEY_HEADER = re.compile(r"^\s*(amount|debit|credit|balance|running bal|withdrawal|deposit)", re.IGNORECASE)

_BALANCE_LINE = re.compile(
    r"^\s*(?P<kind>beginning|opening|starting|previous|ending|closing|new)\s+balance"
    r"(?:\s+(?:as\s+of|on))?\s*(?P<date>[0-9/.\-]{6,10})?",
    re.IGNORECASE,
)

_INFLOW_WORDS = {
    "any": re.compile(r"\b(deposit|refund|payroll|direct dep|interest paid)\b", re.IGNORECASE),
    "asset": re.compile(r"\b(deposit|refund|payroll|direct dep|interest paid|transfer from)\b",
                        re.IGNORECASE),
    "liability": re.compile(r"\b(payment thank you|autopay payment|payment received|"
                            r"online payment, thank you|refund|return)\b", re.IGNORECASE),
}


class ForeignCurrencyError(ValueError):
    """A money cell that is not in the expected currency."""


class StatementFileError(ValueError):
    """A file that cannot be read safely. ``code`` is stable for callers."""

    def __init__(self, code: str, message: str, rows: list[int] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.rows = rows or []

    def as_dict(self) -> JsonObject:
        payload: JsonObject = {"code": self.code, "message": str(self)}
        if self.rows:
            payload["rows"] = self.rows[:20]
        return payload


@dataclass
class ParsedRow:
    line: int
    date: str
    description: str
    amount: Decimal
    balance: Decimal | None = None
    reference: str = ""
    memo: str = ""
    pending: bool = False


@dataclass
class ParsedCsv:
    rows: list[ParsedRow]
    pending_rows: int = 0
    non_transaction_rows: list[JsonObject] = field(default_factory=list)
    metadata: JsonObject = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def read_text(path: Path) -> str:
    """Return decoded text, refusing binary content and oversized files."""
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise StatementFileError("file_too_large", f"'{path.name}' is larger than 25 MB.")
    if not data.strip():
        raise StatementFileError("empty_file", f"'{path.name}' is empty.")
    if b"\x00" in data[:4096]:
        raise StatementFileError(
            "binary_file", f"'{path.name}' is not a text CSV or OFX file. "
            "Export the statement as CSV, OFX, QFX or QBO.")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def read_rows(text: str) -> tuple[list[list[str]], str]:
    """Split CSV text into stripped rows and return the delimiter used."""
    sample = "\n".join(text.splitlines()[:50])
    counts = {d: sum(line.count(d) for line in sample.splitlines()) for d in (",", ";", "\t", "|")}
    delimiter = max(counts, key=lambda d: counts[d])
    if counts[delimiter] == 0:
        raise StatementFileError(
            "not_delimited", "The file has no comma, semicolon, tab or pipe separators; "
            "it is not a CSV statement.")
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        rows = [[cell.strip() for cell in row] for row in reader]
    except csv.Error as exc:
        raise StatementFileError("malformed_csv", f"The CSV structure is damaged: {exc}.") from exc
    while rows and not any(rows[-1]):
        rows.pop()
    return rows, delimiter


def _norm_header(value: str) -> str:
    value = value.lower().replace("﻿", "")
    value = re.sub(r"\(.*?\)", lambda m: " " + m.group(0)[1:-1] + " ", value)
    value = re.sub(r"[^a-z0-9#/.]+", " ", value)
    return re.sub(r"\s+", " ", value).strip(" .")


def _header_role(cell: str) -> str | None:
    name = _norm_header(cell)
    if not name:
        return None
    for role, names in _HEADER_ROLES.items():
        if name in {_norm_header(n) for n in names}:
            return role
    # "Amount (EUR)" or "Balance GBP": a money header that names its currency.
    head, _, tail = name.rpartition(" ")
    if head and tail.upper() in currency_mod.CURRENCIES:
        role = _header_role(head)
        if role in {"amount", "debit", "credit", "balance"}:
            return role
    return None


# ---------------------------------------------------------------------------
# Cell parsing
# ---------------------------------------------------------------------------

def parse_date(value: str, fmt: str) -> date:
    value = value.strip()
    if " " in value and fmt.count(" ") == 0:
        head, _, tail = value.partition(" ")
        if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?(\s*[AaPp][Mm])?", tail.strip()):
            value = head
    return datetime.strptime(value, fmt).date()


def _foreign_currency(value: str, currency: str = "USD") -> str | None:
    """The first currency mark in *value* that is not *currency* ("$" counts for non-dollar currencies)."""
    others = sorted(currency_mod.marks_in(value) - {currency})
    if others:
        return others[0]
    if "$" in value and not currency_mod.dollar_allowed(currency):
        return "$"
    return None


def _mixed_error(found: set[str], what: str = "The file") -> StatementFileError:
    return StatementFileError(
        "unsupported_currency",
        f"{what} has amounts in more than one currency ({', '.join(sorted(found))}). Each company keeps "
        "its books in one currency and Slashbooks does not convert currencies. Ask for a separate file "
        "for each currency, or a file already converted to the company currency.")


def header_currencies(rows: list[list[str]]) -> set[str]:
    """Currencies named by money column headers, such as "Amount (EUR)"."""
    found: set[str] = set()
    for row in rows[:MAX_PREAMBLE_ROWS]:
        for cell in row:
            if _MONEY_HEADER.search(cell) and len(cell) <= 25:
                found |= currency_mod.marks_in(cell)
    return found


def check_currency_headers(rows: list[list[str]], currency: str | None = None) -> None:
    """Refuse a file whose money columns name more than one currency, or a currency other than *currency*."""
    found = header_currencies(rows)
    if len(found) > 1:
        raise _mixed_error(found)
    if currency is not None and found - {currency}:
        raise StatementFileError(
            "unsupported_currency",
            f"A money column is in {next(iter(found))}, not in {currency}. Slashbooks does not convert currencies.")


def file_currencies(text: str, layout: JsonObject | None = None) -> set[str]:
    """Currencies a CSV file names in money headers, money cells or a currency column.

    A plain "$" is not counted: many currencies use it. Descriptions are not read.
    """
    rows, _ = read_rows(text)
    found = header_currencies(rows)
    if layout and layout.get("columns"):
        columns = {k: int(v) for k, v in layout["columns"].items()}
        try:
            header_index = locate_header(rows, layout)
        except StatementFileError:
            header_index = None
        body = rows[(header_index + 1) if header_index is not None else 0:]
    else:
        header_index, roles = _find_header(rows)
        if header_index is None:
            columns, _ = _infer_headerless(rows)
            body = rows
        else:
            columns = dict(roles)
            body = rows[header_index + 1:]
    for role in ("amount", "debit", "credit", "balance"):
        col = columns.get(role)
        if col is not None:
            for row in body:
                if col < len(row) and row[col]:
                    found |= currency_mod.marks_in(row[col])
    col = columns.get("currency")
    if col is not None:
        found |= {row[col].strip().upper() for row in body if col < len(row) and row[col].strip()}
    return found


def parse_money(value: str, decimal: str = ".", currency: str | None = "USD") -> Decimal | None:
    """Parse one money cell. Empty cells return None; bad cells raise ValueError.

    A mark of another currency than *currency* raises ForeignCurrencyError. With
    ``currency=None`` any one currency mark is accepted (detection only).
    """
    raw = value.strip()
    if not raw:
        return None
    marks = currency_mod.marks_in(raw)
    if currency is None:
        if len(marks) > 1:
            raise ForeignCurrencyError(f"more than one currency in {raw!r}")
        allowed = marks
    else:
        if _foreign_currency(raw, currency):
            raise ForeignCurrencyError(f"value {raw!r} is not in {currency}")
        allowed = {currency}
    text = raw
    for code in allowed:
        symbol = currency_mod.CURRENCIES[code].symbol if code in currency_mod.CURRENCIES else None
        if symbol:
            text = text.replace(symbol, "")
        text = re.sub(rf"(?i)(?<![A-Za-z]){code}(?![A-Za-z])", "", text)
    for mark, code in currency_mod.UNAMBIGUOUS_SYMBOLS.items():
        if code in allowed:
            text = text.replace(mark, "")
    text = text.replace("$", "").replace("USD", "").replace("−", "-").replace(" ", "")
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1]
    if text.endswith("-"):
        negative, text = not negative, text[:-1]
    if text.startswith("-"):
        negative, text = not negative, text[1:]
    elif text.startswith("+"):
        text = text[1:]
    if text.startswith("$"):
        text = text[1:]
    if decimal == ",":
        text = text.replace(".", "").replace("'", "").replace(",", ".")
    else:
        text = text.replace(",", "").replace("'", "")
    if not re.fullmatch(r"\d+(\.\d+)?|\.\d+", text):
        raise ValueError(f"not a money value: {raw!r}")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"not a money value: {raw!r}") from exc
    if amount != amount.quantize(MONEY) and len(text.split(".")[-1]) > 2:
        raise ValueError(f"more than two decimal places: {raw!r}")
    amount = amount.quantize(MONEY)
    return -amount if negative else amount


def _is_money(value: str) -> bool:
    try:
        return parse_money(value, ".", None) is not None
    except ValueError:
        try:
            return parse_money(value, ",", None) is not None
        except ValueError:
            return False


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _find_header(rows: list[list[str]]) -> tuple[int | None, dict[str, int]]:
    for index, row in enumerate(rows[:MAX_PREAMBLE_ROWS]):
        roles: dict[str, int] = {}
        for column, cell in enumerate(row):
            role = _header_role(cell)
            if role and role not in roles:
                roles[role] = column
        has_date = "date" in roles or "posted_date" in roles
        has_money = any(r in roles for r in ("amount", "debit", "credit"))
        if has_date and has_money:
            return index, roles
    return None, {}


def _detect_date_format(values: list[str]) -> tuple[str | None, list[str], list[str]]:
    """Return (format, viable formats, uncertain reasons)."""
    values = [v for v in values if v.strip()]
    if not values:
        return None, [], ["date_format"]
    viable: list[str] = []
    for fmt in DATE_FORMATS:
        try:
            for value in values:
                parse_date(value, fmt)
        except ValueError:
            continue
        viable.append(fmt)
    if not viable:
        return None, [], ["date_format"]
    results = {fmt: tuple(parse_date(v, fmt) for v in values) for fmt in viable}
    distinct = {res for res in results.values()}
    if len(distinct) > 1:
        return None, viable, ["date_format"]
    return viable[0], viable, []


def _detect_decimal(values: list[str]) -> tuple[str | None, list[str]]:
    comma_votes = dot_votes = 0
    ambiguous = False
    for raw in values:
        text = re.sub(r"[^0-9.,]", "", raw)
        if not text:
            continue
        if re.search(r",\d{1,2}$", text) and "." not in text[text.rfind(","):]:
            if re.search(r"\.\d{3}", text) or "." not in text:
                comma_votes += 1
                continue
        if re.search(r"\.\d{1,2}$", text):
            dot_votes += 1
            continue
        if re.search(r"[.,]\d{3}$", text):
            ambiguous = True
    if comma_votes and dot_votes:
        return None, ["decimal"]
    if comma_votes:
        return ",", []
    if dot_votes or not ambiguous:
        return ".", []
    return None, ["decimal"]


def _column_values(rows: list[list[str]], column: int | None) -> list[str]:
    if column is None:
        return []
    return [row[column] if column < len(row) else "" for row in rows]


def _infer_headerless(rows: list[list[str]]) -> tuple[dict[str, int], list[str]]:
    """Infer columns for a file with no header row, from cell content only."""
    data = [row for row in rows if any(row)]
    if not data:
        return {}, ["columns"]
    width = max(len(row) for row in data)
    date_cols, money_cols, text_cols = [], [], []
    for column in range(width):
        values = [v for v in _column_values(data, column) if v]
        if not values:
            continue
        if len(set(values)) == 1 and len(data) > 1 and not _is_money(values[0]):
            continue  # Constant marker column such as "*".
        if _detect_date_format(values)[0] or _detect_date_format(values)[1]:
            date_cols.append(column)
        elif all(_is_money(v) for v in values):
            money_cols.append(column)
        else:
            text_cols.append(column)
    if len(date_cols) != 1 or len(money_cols) != 1 or not text_cols:
        return {}, ["columns"]
    description = max(text_cols, key=lambda c: len(set(_column_values(data, c))))
    return {"date": date_cols[0], "amount": money_cols[0], "description": description}, []


def _direction_of(value: str) -> str | None:
    key = value.strip().lower().replace("_", " ")
    if key in _DIRECTION_IN:
        return "in"
    if key in _DIRECTION_OUT:
        return "out"
    if key.startswith("ach debit") or key.endswith(" debit"):
        return "out"
    if key.startswith("ach credit") or key.endswith(" credit"):
        return "in"
    return None


def _balance_relation(amounts: list[Decimal | None], balances: list[Decimal | None]
                      ) -> tuple[str, str] | None:
    """Return (order, relation) when the running balance proves one pairing.

    ``order`` is ``file`` or ``reversed``; ``relation`` is ``with`` when the
    balance rises with a positive amount, or ``against`` when it falls.
    """
    pairs = [(a, b) for a, b in zip(amounts, balances) if a is not None and b is not None]
    if len(pairs) < 2:
        return None
    matches = []
    for order in ("file", "reversed"):
        seq = pairs if order == "file" else list(reversed(pairs))
        for relation in ("with", "against"):
            factor = 1 if relation == "with" else -1
            if all(seq[i][1] - seq[i - 1][1] == factor * seq[i][0] for i in range(1, len(seq))):
                matches.append((order, relation))
    return matches[0] if len(matches) == 1 else None


def account_kind(ledger_account: str | None) -> str | None:
    if not ledger_account:
        return None
    if ledger_account.startswith("Assets:"):
        return "asset"
    if ledger_account.startswith("Liabilities:"):
        return "liability"
    raise StatementFileError(
        "invalid_ledger_account",
        "A statement account must be an Assets: or Liabilities: ledger account.")


def detect_layout(text: str, *, kind: str | None = None, default_currency: str = "USD") -> JsonObject:
    """Propose a layout for CSV *text*.

    Returns a dict with ``layout`` (the proposal), ``uncertain`` (fields the
    caller must decide), ``evidence``, ``warnings`` and ``preview``. The layout
    ``currency`` is the one currency the file names, or *default_currency* (the
    company currency) when the file names none. A file that names more than one
    currency is refused.
    """
    rows, delimiter = read_rows(text)
    check_currency_headers(rows)
    uncertain: list[str] = []
    warnings: list[str] = []
    evidence: JsonObject = {}

    header_index, header_roles = _find_header(rows)
    if header_index is None:
        data_rows = rows
        columns, missing = _infer_headerless(rows)
        uncertain += missing
        header: list[str] = []
        if columns:
            warnings.append("The file has no header row. Columns were inferred from cell content.")
    else:
        header = rows[header_index]
        data_rows = rows[header_index + 1:]
        columns = {}
        if "posted_date" in header_roles:
            columns["date"] = header_roles["posted_date"]
            if "date" in header_roles:
                evidence["date_column"] = (
                    "The file has a transaction date and a posting date; the posting date "
                    "is used because statements and bank feeds use posted dates.")
        elif "date" in header_roles:
            columns["date"] = header_roles["date"]
        for role in ("description", "amount", "debit", "credit", "balance", "reference",
                     "status", "currency", "memo"):
            if role in header_roles:
                columns[role] = header_roles[role]
        if "direction" in header_roles:
            values = [v for v in _column_values(data_rows, header_roles["direction"]) if v]
            known = [_direction_of(v) for v in values]
            if values and sum(1 for d in known if d) >= max(1, int(len(values) * 0.8)):
                columns["direction"] = header_roles["direction"]
            elif "description" not in columns and _norm_header(header[header_roles["direction"]]) == "details":
                columns["description"] = header_roles["direction"]
        if "description" not in columns and "memo" in columns:
            columns["description"] = columns.pop("memo")
        if "description" not in columns:
            uncertain.append("description_column")
        if "amount" in columns and ("debit" in columns or "credit" in columns):
            # Keep the signed amount column; debit/credit columns are summaries.
            columns.pop("debit", None)
            columns.pop("credit", None)
        if "amount" not in columns and not ("debit" in columns and "credit" in columns):
            uncertain.append("amount_columns")

    # Restrict to rows that carry a date in the date column.
    date_col = columns.get("date")
    body = [row for row in data_rows if any(row)]
    date_values = [row[date_col] for row in body if date_col is not None and date_col < len(row)]
    candidate_dates = [v for v in date_values if v and re.search(r"\d", v)]
    date_format, viable, date_uncertain = _detect_date_format(candidate_dates)
    if not date_format:
        parsed_any = []
        for fmt in DATE_FORMATS:
            ok = [v for v in candidate_dates if _try_date(v, fmt)]
            if len(ok) >= max(1, int(len(candidate_dates) * 0.9)):
                parsed_any.append(fmt)
        if parsed_any:
            subset = [v for v in candidate_dates if _try_date(v, parsed_any[0])]
            date_format, viable, date_uncertain = _detect_date_format(subset)
    uncertain += date_uncertain
    if len(viable) > 1 and date_format is None:
        evidence["date_format"] = (
            "Every date in the file fits more than one format, for example both "
            f"{viable[0]} and {viable[1]}. Choose the format the bank uses.")

    money_cols = [columns[r] for r in ("amount", "debit", "credit", "balance") if r in columns]
    money_values = [row[c] for row in body for c in money_cols if c < len(row) and row[c]]
    named = header_currencies(rows)
    for value in money_values:
        named |= currency_mod.marks_in(value)
    if "currency" in columns:
        named |= {row[columns["currency"]].strip().upper() for row in body
                  if columns["currency"] < len(row) and row[columns["currency"]].strip()}
    if len(named) > 1:
        raise _mixed_error(named)
    file_currency = next(iter(named)) if named else default_currency
    for value in money_values:
        if _foreign_currency(value, file_currency):
            raise StatementFileError(
                "unsupported_currency",
                f"The file contains an amount ({value}) that is not in {file_currency}. "
                "Slashbooks does not convert currencies.")
    decimal, decimal_uncertain = _detect_decimal(money_values)
    uncertain += decimal_uncertain

    sign = None
    if "amount" not in columns and "debit" in columns and "credit" in columns:
        sign = "debit_credit"
        evidence["sign"] = "Separate debit and credit columns: credits are money in, debits are money out."
        for role in ("debit", "credit"):
            values = []
            for row in body:
                try:
                    v = (parse_money(row[columns[role]], decimal or ".", file_currency)
                         if columns[role] < len(row) else None)
                except ValueError:
                    v = None
                if v:
                    values.append(v)
            if any(v > 0 for v in values) and any(v < 0 for v in values):
                uncertain.append("debit_credit_signs")
                evidence["debit_credit_signs"] = (
                    f"The {role} column has both positive and negative values.")
    elif "amount" in columns and decimal:
        sign, sign_evidence, conflict = _detect_sign(body, columns, date_format, decimal, kind, file_currency)
        evidence.update(sign_evidence)
        if sign is None:
            uncertain.append("sign")
            if not conflict:
                evidence.setdefault("sign", (
                    "The file has one amount column and no reliable signal for whether "
                    "positive amounts are money in or money out."))

    layout: JsonObject = {
        "format": "csv",
        "delimiter": delimiter,
        "header": header,
        "header_row": header_index,
        "columns": columns,
        "date_format": date_format,
        "decimal": decimal,
        "sign": sign,
        "currency": file_currency,
    }
    preview = []
    if date_col is not None:
        row_count = len(candidate_dates)
    else:
        # No date column: a leading line with no digits above lines that carry
        # digits is an unrecognized header line, not a data row.
        has_digits = [any(re.search(r"\d", cell) for cell in row) for row in body]
        header_line = header_index is None and body and not has_digits[0] and any(has_digits)
        row_count = len(body) - (1 if header_line else 0)
    if not uncertain:
        try:
            parsed = parse_rows(rows, layout, kind=kind)
            row_count = len(parsed.rows)
            preview = [_preview(r) for r in parsed.rows[:5]]
            warnings += parsed.warnings
        except StatementFileError as exc:
            warnings.append(str(exc))
    return {
        "layout": layout,
        "uncertain": sorted(set(uncertain)),
        "evidence": evidence,
        "warnings": warnings,
        "preview": preview,
        "row_count": row_count,
    }


def _try_date(value: str, fmt: str) -> bool:
    try:
        parse_date(value, fmt)
        return True
    except ValueError:
        return False


def _detect_sign(body: list[list[str]], columns: dict[str, int], date_format: str | None,
                 decimal: str, kind: str | None, currency: str = "USD") -> tuple[str | None, JsonObject, bool]:
    votes: dict[str, str] = {}
    amount_col = columns["amount"]
    amounts: list[Decimal | None] = []
    descriptions: list[str] = []
    for row in body:
        try:
            amounts.append(parse_money(row[amount_col], decimal, currency) if amount_col < len(row) else None)
        except ValueError:
            amounts.append(None)
        desc_col = columns.get("description")
        descriptions.append(row[desc_col] if desc_col is not None and desc_col < len(row) else "")

    if "direction" in columns:
        dirs = [_direction_of(row[columns["direction"]]) if columns["direction"] < len(row) else None
                for row in body]
        known = [(d, a) for d, a in zip(dirs, amounts) if d and a]
        if known and all(a > 0 for _, a in known):
            votes["direction_column"] = "direction_column"
        elif known and all((a > 0) == (d == "in") for d, a in known):
            votes["direction_column"] = "standard"
        elif known and all((a > 0) == (d == "out") for d, a in known):
            votes["direction_column"] = "inverted"

    if "balance" in columns and kind == "asset":
        balances = []
        for row in body:
            try:
                balances.append(parse_money(row[columns["balance"]], decimal, currency)
                                if columns["balance"] < len(row) else None)
            except ValueError:
                balances.append(None)
        relation = _balance_relation(amounts, balances)
        if relation:
            votes["running_balance"] = "standard" if relation[1] == "with" else "inverted"

    pattern = _INFLOW_WORDS.get(kind or "any", _INFLOW_WORDS["any"])
    hits = [a for a, d in zip(amounts, descriptions) if a and pattern.search(d)]
    if len(hits) >= 2:
        if all(a > 0 for a in hits):
            votes["descriptions"] = "standard"
        elif all(a < 0 for a in hits):
            votes["descriptions"] = "inverted"

    evidence: JsonObject = {}
    if votes:
        evidence["sign_votes"] = votes
    choices = set(votes.values())
    if len(choices) == 1:
        choice = choices.pop()
        evidence["sign"] = "Decided by: " + ", ".join(sorted(votes)) + "."
        return choice, evidence, False
    if len(choices) > 1:
        evidence["sign"] = "The signals disagree: " + ", ".join(
            f"{k} says {v}" for k, v in sorted(votes.items())) + "."
        return None, evidence, True
    return None, evidence, False


def _preview(row: ParsedRow) -> JsonObject:
    return {"date": row.date, "description": row.description, "amount": format(row.amount, "f"),
            "balance": format(row.balance, "f") if row.balance is not None else None}


# ---------------------------------------------------------------------------
# Parsing with a confirmed layout
# ---------------------------------------------------------------------------

def locate_header(rows: list[list[str]], layout: JsonObject) -> int | None:
    """Find the saved header in *rows*; raise when the file layout changed."""
    header = list(layout.get("header") or [])
    if not header:
        return None
    for index, row in enumerate(rows[:MAX_PREAMBLE_ROWS]):
        if row[:len(header)] == header or row == header:
            return index
    raise StatementFileError(
        "layout_changed",
        "The file does not have the saved column header for this profile. "
        "Inspect the file again and save a new mapping if the bank changed its export.")


def parse_rows(rows: list[list[str]], layout: JsonObject, *, kind: str | None,
               currency: str | None = None) -> ParsedCsv:
    """Read rows with a confirmed layout. Amounts must be in *currency* (default: the layout currency, else USD)."""
    currency = currency or layout.get("currency") or "USD"
    columns = {k: int(v) for k, v in (layout.get("columns") or {}).items()}
    date_format = layout.get("date_format")
    decimal = layout.get("decimal") or "."
    sign = layout.get("sign")
    if not date_format or sign not in SIGN_MODES or "date" not in columns:
        raise StatementFileError("incomplete_layout", "The saved layout is missing a date column, "
                                 "date format or sign convention.")
    header_index = locate_header(rows, layout)
    preamble = rows[:header_index] if header_index is not None else []
    body_start = (header_index + 1) if header_index is not None else 0

    parsed = ParsedCsv(rows=[])
    errors: list[int] = []
    error_detail: list[str] = []
    balance_markers: list[JsonObject] = []

    def cell(row: list[str], role: str) -> str:
        col = columns.get(role)
        return row[col] if col is not None and col < len(row) else ""

    for offset, row in enumerate(rows[body_start:]):
        line = body_start + offset + 1
        if not any(row):
            continue
        raw_date = cell(row, "date")
        description = cell(row, "description")
        try:
            day = parse_date(raw_date, date_format)
        except ValueError:
            marker = _balance_marker(row, decimal, date_format, currency=currency)
            if marker:
                balance_markers.append(marker)
                parsed.non_transaction_rows.append({"line": line, "reason": "balance summary"})
                continue
            errors.append(line)
            error_detail.append(f"line {line}: date {raw_date!r} does not match {date_format}")
            continue
        try:
            if "currency" in columns and cell(row, "currency") and cell(row, "currency").strip().upper() != currency:
                raise StatementFileError(
                    "unsupported_currency",
                    f"Line {line} is in {cell(row, 'currency')}, not in {currency}. "
                    "Slashbooks does not convert currencies.",
                    [line])
            if sign == "debit_credit":
                debit = parse_money(cell(row, "debit"), decimal, currency)
                credit = parse_money(cell(row, "credit"), decimal, currency)
                if debit and credit:
                    raise ValueError("both debit and credit are filled")
                if debit is None and credit is None:
                    amount = None
                else:
                    amount = abs(credit or Decimal("0")) - abs(debit or Decimal("0"))
            else:
                amount = parse_money(cell(row, "amount"), decimal, currency)
                if amount is not None:
                    if sign == "inverted":
                        amount = -amount
                    elif sign == "direction_column":
                        direction = _direction_of(cell(row, "direction"))
                        if direction is None:
                            raise ValueError(f"unknown direction {cell(row, 'direction')!r}")
                        amount = abs(amount) if direction == "in" else -abs(amount)
            balance = parse_money(cell(row, "balance"), decimal, currency) if "balance" in columns else None
        except StatementFileError:
            raise
        except ForeignCurrencyError as exc:
            raise StatementFileError(
                    "unsupported_currency",
                    f"Line {line} has an amount that is not in {currency}. Slashbooks does not convert currencies.",
                    [line]) from exc
        except ValueError as exc:
            errors.append(line)
            error_detail.append(f"line {line}: {exc}")
            continue
        if amount is None:
            marker = _balance_marker(row, decimal, date_format, day, currency=currency)
            if marker or balance is not None:
                balance_markers.append(marker or {"kind": "balance", "date": day.isoformat(),
                                                  "amount": balance})
                parsed.non_transaction_rows.append({"line": line, "reason": "balance row"})
                continue
            errors.append(line)
            error_detail.append(f"line {line}: no amount")
            continue
        status = cell(row, "status").lower()
        pending = "pending" in status or "authoriz" in status
        if pending:
            parsed.pending_rows += 1
            continue
        parsed.rows.append(ParsedRow(
            line=line, date=day.isoformat(), description=description, amount=amount,
            balance=balance, reference=cell(row, "reference").lstrip("'"),
            memo=cell(row, "memo")))

    if errors:
        raise StatementFileError(
            "unparseable_rows",
            f"{len(errors)} row(s) could not be read with this layout: "
            + "; ".join(error_detail[:5]) + ("" if len(errors) <= 5 else "; ..."),
            errors)
    if not parsed.rows:
        raise StatementFileError("no_transactions", "The file has no posted transactions to import.")

    for line_row in preamble:
        marker = _balance_marker(line_row, decimal, date_format, currency=currency)
        if marker:
            balance_markers.append(marker)

    parsed.metadata = _statement_metadata(parsed, balance_markers, kind)
    parsed.warnings.extend(parsed.metadata.pop("_warnings", []))
    return parsed


def _balance_marker(row: list[str], decimal: str, date_format: str,
                    row_date: date | None = None, *, currency: str = "USD") -> JsonObject | None:
    for index, cell in enumerate(row):
        match = _BALANCE_LINE.match(cell)
        if not match:
            continue
        kind = "opening" if match.group("kind").lower() in {"beginning", "opening", "starting",
                                                             "previous"} else "closing"
        when = row_date
        if match.group("date"):
            for fmt in (date_format, *DATE_FORMATS):
                try:
                    when = parse_date(match.group("date"), fmt)
                    break
                except ValueError:
                    continue
        amount = None
        for other in reversed(row[index + 1:]):
            try:
                amount = parse_money(other, decimal, currency)
            except ValueError:
                continue
            if amount is not None:
                break
        if amount is None:
            return None
        return {"kind": kind, "date": when.isoformat() if when else None, "amount": amount}
    return None


def _statement_metadata(parsed: ParsedCsv, markers: list[JsonObject], kind: str | None) -> JsonObject:
    warnings: list[str] = []
    dates = sorted(r.date for r in parsed.rows)
    meta: JsonObject = {
        "period_start": dates[0],
        "period_end": dates[-1],
        "period_source": "transactions",
        "opening_balance": None,
        "opening_balance_date": None,
        "closing_balance": None,
        "closing_balance_date": None,
        "balance_source": None,
        "first_transaction_date": dates[0],
        "last_transaction_date": dates[-1],
    }
    opening = next((m for m in markers if m["kind"] == "opening"), None)
    closing = next((m for m in markers if m["kind"] == "closing"), None)
    if (opening or closing) and kind == "asset":
        if opening and opening.get("date"):
            meta["period_start"] = opening["date"]
        if closing and closing.get("date"):
            meta["period_end"] = closing["date"]
        meta["period_source"] = "file_summary"
        if opening:
            meta["opening_balance"] = opening["amount"]
            meta["opening_balance_date"] = opening.get("date")
        if closing:
            meta["closing_balance"] = closing["amount"]
            meta["closing_balance_date"] = closing.get("date")
        meta["balance_source"] = "file_summary"
    elif opening or closing:
        warnings.append("The file prints summary balances, but their sign is not reliable for a "
                        "card account; they were not recorded.")

    balances = [r.balance for r in parsed.rows]
    if any(b is not None for b in balances) and meta["balance_source"] is None:
        relation = _balance_relation([r.amount for r in parsed.rows], balances)
        if relation is None:
            warnings.append("The running balance column does not follow the amounts in file "
                            "order or reverse order; statement balances were not recorded.")
        else:
            order, rel = relation
            chronological = [r for r in parsed.rows if r.balance is not None]
            if order == "reversed":
                chronological.reverse()
            factor = 1 if rel == "with" else -1
            first, last = chronological[0], chronological[-1]
            meta["opening_balance"] = factor * first.balance - first.amount
            meta["opening_balance_date"] = first.date
            meta["closing_balance"] = factor * last.balance
            meta["closing_balance_date"] = last.date
            meta["balance_source"] = "running_balance"
            for r in parsed.rows:
                if r.balance is not None:
                    r.balance = factor * r.balance
    meta["_warnings"] = warnings
    return meta
