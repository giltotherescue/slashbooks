from __future__ import annotations

"""Tests for src/bookkeeping/ledger/model.py"""

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping.ledger.model import (  # noqa: E402
    Balance,
    Entry,
    Ledger,
    Open,
    Posting,
    TOLERANCE,
    QUANTIZE,
    _validate_account_name,
)


# ---------------------------------------------------------------------------
# Account name validation
# ---------------------------------------------------------------------------


class TestAccountNameValidation(unittest.TestCase):
    def test_valid_roots(self) -> None:
        for root in ("Assets", "Liabilities", "Equity", "Income", "Expenses"):
            _validate_account_name(root)  # must not raise

    def test_valid_multilevel(self) -> None:
        _validate_account_name("Assets:Checking:Mercury")
        _validate_account_name("Expenses:Software:Subscriptions")
        _validate_account_name("Liabilities:CreditCard:Amex-Canceled")

    def test_invalid_root(self) -> None:
        with self.assertRaises(ValueError):
            _validate_account_name("Bank:Checking")
        with self.assertRaises(ValueError):
            _validate_account_name("assets:Checking")  # lowercase root
        with self.assertRaises(ValueError):
            _validate_account_name("Revenue:Services")

    def test_empty_segment(self) -> None:
        with self.assertRaises(ValueError):
            _validate_account_name("Assets::Checking")

    def test_segment_starting_with_lowercase(self) -> None:
        with self.assertRaises(ValueError):
            _validate_account_name("Assets:checking")

    def test_segment_with_invalid_char(self) -> None:
        with self.assertRaises(ValueError):
            _validate_account_name("Assets:Check_ing")  # underscore not allowed
        with self.assertRaises(ValueError):
            _validate_account_name("Assets:Chec king")  # space not allowed

    def test_digit_start_segment_ok(self) -> None:
        _validate_account_name("Assets:1stBank")

    def test_hyphen_in_segment_ok(self) -> None:
        _validate_account_name("Expenses:Meals-Entertainment")


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


class TestPosting(unittest.TestCase):
    def test_valid_posting(self) -> None:
        p = Posting(account="Assets:Checking", amount=Decimal("100.00"))
        self.assertEqual(p.amount, Decimal("100.00"))
        self.assertEqual(p.currency, "USD")

    def test_invalid_account_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Posting(account="bank:checking", amount=Decimal("10.00"))

    def test_overprecise_amount_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Posting(account="Assets:Checking", amount=Decimal("33.333"))

    def test_overprecise_third_decimal_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Posting(account="Assets:Checking", amount=Decimal("10.001"))

    def test_integer_amount_accepted_as_unquantized(self) -> None:
        # Decimal("100") == Decimal("100.00") as values (both compare equal),
        # so the validator accepts it.  The writer will render it as "100.00".
        # This is the intended behavior: integers are valid, they just emit .00.
        p = Posting(account="Assets:Checking", amount=Decimal("100"))
        self.assertEqual(p.amount, Decimal("100"))

    def test_two_decimal_integer_ok(self) -> None:
        p = Posting(account="Assets:Checking", amount=Decimal("100.00"))
        self.assertEqual(p.amount, Decimal("100.00"))

    def test_posting_is_frozen(self) -> None:
        p = Posting(account="Assets:Checking", amount=Decimal("10.00"))
        with self.assertRaises(Exception):
            p.amount = Decimal("20.00")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


class TestEntry(unittest.TestCase):
    def _make_balanced(self, amt: Decimal = Decimal("100.00")) -> Entry:
        return Entry(
            date=date(2026, 1, 15),
            narration="Test transaction",
            postings=(
                Posting("Assets:Checking", amt),
                Posting("Income:Services", -amt),
            ),
        )

    def test_balanced_entry_ok(self) -> None:
        e = self._make_balanced()
        self.assertEqual(len(e.postings), 2)

    def test_fewer_than_two_postings_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Entry(
                date=date(2026, 1, 15),
                narration="Only one posting",
                postings=(Posting("Assets:Checking", Decimal("100.00")),),
            )

    def test_unbalanced_beyond_tolerance_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Entry(
                date=date(2026, 1, 15),
                narration="Unbalanced",
                postings=(
                    Posting("Assets:Checking", Decimal("100.00")),
                    Posting("Income:Services", Decimal("-99.98")),
                    # sum = 0.02 > 0.005 tolerance
                ),
            )

    def test_overprecise_posting_rejected_before_tolerance_check(self) -> None:
        # Posting with 3 decimal places is rejected at the Posting level,
        # before Entry even checks balance tolerance.
        with self.assertRaises(ValueError):
            Entry(
                date=date(2026, 1, 15),
                narration="Near-balanced with overprecise posting",
                postings=(
                    Posting("Assets:Checking", Decimal("100.00")),
                    Posting("Income:Services", Decimal("-99.997")),
                ),
            )

    def test_within_tolerance_two_decimal(self) -> None:
        # sum = 0.00, within tolerance — exact balance
        e = Entry(
            date=date(2026, 1, 15),
            narration="Exact balance",
            postings=(
                Posting("Assets:Checking", Decimal("100.00")),
                Posting("Income:Services", Decimal("-100.00")),
            ),
        )
        self.assertEqual(len(e.postings), 2)

    def test_source_id_property(self) -> None:
        e = Entry(
            date=date(2026, 1, 15),
            narration="With source",
            meta=(("source-id", "txn_abc123"),),
            postings=(
                Posting("Assets:Checking", Decimal("10.00")),
                Posting("Income:Services", Decimal("-10.00")),
            ),
        )
        self.assertEqual(e.source_id, "txn_abc123")

    def test_source_id_none_when_absent(self) -> None:
        e = self._make_balanced()
        self.assertIsNone(e.source_id)

    def test_entry_is_frozen(self) -> None:
        e = self._make_balanced()
        with self.assertRaises(Exception):
            e.narration = "modified"  # type: ignore[misc]

    def test_overprecise_amount_in_posting_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Entry(
                date=date(2026, 1, 15),
                narration="Bad amount",
                postings=(
                    Posting("Assets:Checking", Decimal("33.333")),
                    Posting("Income:Services", Decimal("-33.333")),
                ),
            )


# ---------------------------------------------------------------------------
# Open and Balance
# ---------------------------------------------------------------------------


class TestOpen(unittest.TestCase):
    def test_valid_open(self) -> None:
        o = Open(date=date(2026, 1, 1), account="Assets:Checking", currencies=("USD",))
        self.assertEqual(o.account, "Assets:Checking")

    def test_invalid_account_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Open(date=date(2026, 1, 1), account="cash")


class TestBalance(unittest.TestCase):
    def test_valid_balance(self) -> None:
        b = Balance(date=date(2026, 2, 1), account="Assets:Checking", amount=Decimal("1234.56"))
        self.assertEqual(b.amount, Decimal("1234.56"))

    def test_overprecise_amount_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Balance(date=date(2026, 2, 1), account="Assets:Checking", amount=Decimal("1234.567"))


# ---------------------------------------------------------------------------
# Ledger container
# ---------------------------------------------------------------------------


class TestLedger(unittest.TestCase):
    def test_empty_ledger(self) -> None:
        l = Ledger()
        self.assertEqual(l.opens, ())
        self.assertEqual(l.entries, ())
        self.assertEqual(l.balances, ())

    def test_ledger_is_frozen(self) -> None:
        l = Ledger()
        with self.assertRaises(Exception):
            l.title = "Modified"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
