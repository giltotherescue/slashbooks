from __future__ import annotations

"""Tests for src/bookkeeping/ledger/normalize.py"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping.ledger.normalize import normalize_description  # noqa: E402


class TestNormalizeDescription(unittest.TestCase):
    """Table-driven tests for normalize_description pure function."""

    def _check(self, inp: str, expected: str) -> None:
        result = normalize_description(inp)
        self.assertEqual(result, expected, f"normalize_description({inp!r})")

    # --- Uppercase ---
    def test_lowercase_to_upper(self) -> None:
        self._check("starbucks", "STARBUCKS")

    def test_mixed_case_to_upper(self) -> None:
        self._check("Amazon.Com", "AMAZON.COM")

    # --- Whitespace collapse ---
    def test_internal_spaces_collapsed(self) -> None:
        self._check("whole  foods  market", "WHOLE FOODS MARKET")

    def test_leading_trailing_stripped(self) -> None:
        self._check("  COFFEE SHOP  ", "COFFEE SHOP")

    def test_tab_collapsed(self) -> None:
        self._check("UBER\tEATS", "UBER EATS")

    def test_multiple_spaces_and_tabs(self) -> None:
        self._check("  ACH   DEPOSIT   ", "ACH DEPOSIT")

    # --- Trailing digit-run stripping (≥4 digits) ---
    def test_trailing_4_digit_run_stripped(self) -> None:
        self._check("STARBUCKS 1234", "STARBUCKS")

    def test_trailing_8_digit_run_stripped(self) -> None:
        self._check("COFFEE SHOP 00012345", "COFFEE SHOP")

    def test_trailing_hash_then_digits_stripped(self) -> None:
        self._check("STARBUCKS #1234", "STARBUCKS")

    def test_trailing_asterisk_then_digits_stripped(self) -> None:
        self._check("AMAZON.COM*5678", "AMAZON.COM")

    def test_trailing_hash_space_digits_stripped(self) -> None:
        self._check("TARGET # 9999", "TARGET")

    def test_trailing_3_digit_run_not_stripped(self) -> None:
        # Only strips ≥4 digits
        self._check("SHOP 123", "SHOP 123")

    def test_no_trailing_digits(self) -> None:
        self._check("WHOLE FOODS MARKET", "WHOLE FOODS MARKET")

    # --- Combined ---
    def test_combined_lower_spaces_digits(self) -> None:
        self._check("  amazon prime  *12345678  ", "AMAZON PRIME")

    def test_combined_hash_digits(self) -> None:
        self._check("starbucks store #4567", "STARBUCKS STORE")

    # --- Edge cases ---
    def test_empty_string(self) -> None:
        self._check("", "")

    def test_only_digits(self) -> None:
        # 4+ digits with no preceding text: strips everything
        self._check("12345", "")

    def test_only_3_digits(self) -> None:
        self._check("123", "123")

    def test_digits_in_middle_not_stripped(self) -> None:
        # Stripping is trailing only
        self._check("7ELEVEN STORE 1234", "7ELEVEN STORE")

    def test_digits_not_at_end_preserved(self) -> None:
        self._check("7ELEVEN STORE EXTRA", "7ELEVEN STORE EXTRA")

    def test_hash_without_digits_preserved(self) -> None:
        # '#' alone or with <4 digits should not be stripped
        self._check("SHOP #123", "SHOP #123")

    # --- Pure function: same input always same output ---
    def test_idempotent(self) -> None:
        text = "  Starbucks  #1234  "
        first = normalize_description(text)
        second = normalize_description(text)
        self.assertEqual(first, second)

    def test_repeated_normalization_stable(self) -> None:
        text = "  whole  foods   market  99999  "
        first = normalize_description(text)
        second = normalize_description(first)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
