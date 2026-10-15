"""Spreadsheet formula-injection protection for every CSV and XLSX the engine writes.

A spreadsheet program treats a text cell that starts with ``=``, ``+``, ``-``,
``@``, a tab or a carriage return as a formula. Vendor names, account names,
descriptions and notes come from bank files, QuickBooks exports and other
untrusted sources, so the engine never writes such text as it is.

- ``neutralize_cell`` returns a value that a spreadsheet shows as text.
  Numbers stay numbers: a number type, or a plain numeric string such as
  ``-1,234.50``, is not changed.
- ``neutralize_row`` and ``neutralize_rows`` apply the same rule to a row.
- ``is_numeric_text`` tells whether a string is a plain number.

XLSX writers must also store a string with ``write_string`` and open the
workbook with ``strings_to_formulas`` off, so that no text becomes a formula.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r", "\n")

_NUMERIC_TEXT = re.compile(r"^[+-]?\$?(?:\d{1,3}(?:,\d{3})+|\d+)?(?:\.\d+)?$")


def is_numeric_text(text: str) -> bool:
    """True for a plain number such as ``12``, ``-1,234.50``, ``+.5`` or ``-$3.00``."""
    return bool(text) and any(ch.isdigit() for ch in text) and _NUMERIC_TEXT.fullmatch(text) is not None


def needs_neutralizing(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if is_numeric_text(value):
        return False
    return value.lstrip(" ").startswith(FORMULA_TRIGGERS)


def neutralize_cell(value: Any) -> Any:
    """Return *value*, with a leading apostrophe when it could run as a formula."""
    return "'" + value if needs_neutralizing(value) else value


def neutralize_row(row: Iterable[Any]) -> list[Any]:
    return [neutralize_cell(cell) for cell in row]


def neutralize_rows(rows: Iterable[Iterable[Any]]) -> list[list[Any]]:
    return [neutralize_row(row) for row in rows]
