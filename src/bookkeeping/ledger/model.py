from __future__ import annotations

"""Balanced-postings data model for the beancount ledger engine.

All money values are Decimal quantized to 0.01.  Frozen dataclasses are used
throughout so instances are hashable and immutable.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional, Sequence

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

QUANTIZE = Decimal("0.01")
TOLERANCE = Decimal("0.005")

# Beancount v2 account-name rules:
#   Root segments: exactly Assets | Liabilities | Equity | Income | Expenses
#   Each segment: starts with a capital letter or digit, then letters/digits/hyphens
_VALID_ROOTS = {"Assets", "Liabilities", "Equity", "Income", "Expenses"}
_SEGMENT_RE = re.compile(r"^[A-Z0-9][A-Za-z0-9-]*$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_account_name(account: str) -> None:
    """Raise ValueError if *account* is not a valid beancount account name."""
    if not account:
        raise ValueError("Account name must not be empty")
    segments = account.split(":")
    if segments[0] not in _VALID_ROOTS:
        raise ValueError(
            f"Account root '{segments[0]}' is not valid; must be one of "
            f"{sorted(_VALID_ROOTS)}"
        )
    for seg in segments[1:]:
        if not seg:
            raise ValueError(f"Empty segment in account name '{account}'")
        if not _SEGMENT_RE.match(seg):
            raise ValueError(
                f"Segment '{seg}' in account '{account}' is invalid; "
                "must start with a capital letter or digit, then letters/digits/hyphens only"
            )


def _validate_amount(amount: Decimal, context: str = "") -> None:
    """Raise ValueError if *amount* has finer precision than 0.01."""
    try:
        q = Decimal(amount)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid amount {amount!r}{context}") from exc
    # Check that quantizing to 0.01 doesn't change the value
    if q.quantize(QUANTIZE) != q:
        raise ValueError(
            f"Amount {amount!r} has finer precision than 0.01{context}; "
            "quantize to Decimal('0.01') before constructing"
        )


# ---------------------------------------------------------------------------
# ValidationError (returned by validator, also raised by model)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationError:
    line: int
    message: str

    def __str__(self) -> str:  # noqa: D105
        return f"Line {self.line}: {self.message}"


# ---------------------------------------------------------------------------
# Core directives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Posting:
    """A single leg of a transaction."""

    account: str
    amount: Decimal
    currency: str = "USD"
    meta: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _validate_account_name(self.account)
        _validate_amount(self.amount, f" in posting to '{self.account}'")


@dataclass(frozen=True)
class Entry:
    """A double-entry transaction (≥2 postings, balanced to within tolerance)."""

    date: date
    narration: str
    payee: Optional[str] = None
    flag: str = "*"
    tags: tuple[str, ...] = field(default_factory=tuple)
    links: tuple[str, ...] = field(default_factory=tuple)
    meta: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    postings: tuple[Posting, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if len(self.postings) < 2:
            raise ValueError(
                f"Entry on {self.date} '{self.narration}' must have at least 2 postings, "
                f"got {len(self.postings)}"
            )
        total = sum(p.amount for p in self.postings)
        if abs(total) > TOLERANCE:
            raise ValueError(
                f"Entry on {self.date} '{self.narration}' postings do not balance: "
                f"sum={total} (tolerance={TOLERANCE})"
            )

    @property
    def source_id(self) -> Optional[str]:
        """Return the value of the 'source-id' metadata key, or None."""
        for key, val in self.meta:
            if key == "source-id":
                return val
        return None


@dataclass(frozen=True)
class Open:
    """An ``open`` directive."""

    date: date
    account: str
    currencies: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _validate_account_name(self.account)


@dataclass(frozen=True)
class Balance:
    """A ``balance`` directive (checked at start of day)."""

    date: date
    account: str
    amount: Decimal
    currency: str = "USD"

    def __post_init__(self) -> None:
        _validate_account_name(self.account)
        _validate_amount(self.amount, f" in balance for '{self.account}'")


# ---------------------------------------------------------------------------
# Ledger container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ledger:
    """Container for all directives that make up a set of books."""

    opens: tuple[Open, ...] = field(default_factory=tuple)
    entries: tuple[Entry, ...] = field(default_factory=tuple)
    balances: tuple[Balance, ...] = field(default_factory=tuple)
    title: str = "Books"
